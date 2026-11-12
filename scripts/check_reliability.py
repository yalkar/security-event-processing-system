"""Controlled fault experiments. Run only on the local educational stack.

python -m scripts.check_reliability --report reports/reliability.json
Creates synthetic events, a backup and a NEW test database; preserves production data.
"""
import argparse
from datetime import datetime, timezone
from http.client import HTTPException
import json
import math
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request
from uuid import UUID, uuid4

from scripts import backup

WORKERS = ("event-worker", "event-worker-standby")
SERVICES = ("event-api", *WORKERS, "postgres", "redis", "rabbitmq")


def compose(*args, check=True):
    return subprocess.run(["docker", "compose", *args], check=check,
                          text=True, encoding="utf-8", capture_output=True, timeout=180)


def state(service):
    container = compose("ps", "--all", "-q", service).stdout.strip()
    result = subprocess.check_output(
        ["docker", "inspect", "--format",
         '{"id":{{json .Id}},"restarts":{{.RestartCount}},"state":{{json .State}}}', container],
        text=True, encoding="utf-8")
    data = json.loads(result)
    return {"id": data["id"], "restarts": data["restarts"], **data["state"]}


def http(base, path, payload=None):
    started = time.monotonic()
    request = urllib.request.Request(base + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5 if payload is None else 40) as response:
            return response.status, json.load(response), {k.lower(): v for k, v in response.headers.items()}, time.monotonic() - started
    except urllib.error.HTTPError as error:
        return error.code, json.load(error), {k.lower(): v for k, v in error.headers.items()}, time.monotonic() - started
    except (urllib.error.URLError, TimeoutError, ConnectionError,
            HTTPException, json.JSONDecodeError):
        return 0, {}, {}, time.monotonic() - started


def sample_summary(samples):
    latencies = sorted(sample["seconds"] for sample in samples if sample["good"])
    good = sum(sample["good"] for sample in samples)
    return {"requests": len(samples), "good": good,
            "availability": good / len(samples) if samples else None,
            "p95_seconds": latencies[math.ceil(.95 * len(latencies)) - 1] if latencies else None}


def wait_for(condition, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(.25)
    raise TimeoutError("Recovery not observed within experiment timeout")


def received(event_id, *workers):
    logs = compose("logs", "--since=10m", *(workers or WORKERS)).stdout
    return any("received_event" in line and event_id in line for line in logs.splitlines())


def queue():
    output = compose("exec", "-T", "rabbitmq", "rabbitmqctl", "-q", "list_queues",
                     "name", "messages_ready", "messages_unacknowledged", "consumers").stdout
    for line in output.splitlines():
        columns = line.split()
        if columns and columns[0] == "security_events":
            return dict(zip(("ready", "unacked", "consumers"), map(int, columns[1:])))
    raise RuntimeError("security_events queue not found")


def read_event(database, event_id):
    event_id = str(UUID(event_id))
    value = backup.query(database, f"SELECT row_to_json(e) FROM events e WHERE id = '{event_id}'::uuid")
    return json.loads(value) if value else None


def application_pid(service):
    process_scan = """from pathlib import Path
import os
import sys
pids = []
for entry in Path('/proc').iterdir():
    if not entry.name.isdecimal() or entry.name == str(os.getpid()):
        continue
    try:
        fields = dict(line.split(':', 1) for line in (entry / 'status').read_text().splitlines() if ':' in line)
        command = (entry / 'cmdline').read_bytes().replace(b'\\0', b' ').decode()
    except OSError:
        continue
    if fields.get('PPid', '').strip() == '1' and sys.argv[1] in command:
        pids.append(entry.name)
assert len(pids) == 1, pids
print(pids[0])
"""
    return compose("exec", "-T", "--user", "10001", service, "python", "-c", process_scan,
                   "uvicorn" if service == "event-api" else "app.worker").stdout.strip()


def signal_process(service, pid, signal_name, check=True):
    return compose("exec", "-T", "--user", "10001", service, "python", "-c",
                   "import os,signal,sys; os.kill(int(sys.argv[1]), getattr(signal,sys.argv[2]))",
                   pid, signal_name, check=check)


def crash(service):
    before = state(service)
    # Let Docker's successful-start monitoring window elapse before fault injection.
    started = datetime.fromisoformat(before["StartedAt"].replace("Z", "+00:00"))
    remaining = 10 - (datetime.now(timezone.utc) - started).total_seconds()
    if remaining > 0:
        time.sleep(remaining)
    before = state(service)
    pid = application_pid(service)
    started_at = time.monotonic()
    # Kill the app child of unprivileged tini, NOT docker stop/kill.
    signal_process(service, pid, "SIGKILL", check=False)
    return before, started_at


def run(base, directory, report):
    for service in SERVICES:
        current = state(service)
        assert current["Running"] and not current["Paused"], f"Start all services first: {service}"
    report.update(started_at=backup.utc_now(), environment="local Docker, controlled faults",
                  polling_seconds=.25, experiments=[])
    suspended = {}

    def suspend(*workers):
        for worker in workers:
            pid = application_pid(worker)
            signal_process(worker, pid, "SIGSTOP")
            suspended[worker] = pid

    def resume(*workers):
        for worker in workers:
            signal_process(worker, suspended[worker], "SIGCONT")
            del suspended[worker]

    def created():
        response = http(base, "/events", {"event_type": "failed_login", "source_ip": "192.0.2.10",
                                        "username": "reliability-" + uuid4().hex[:12]})
        assert response[0] == 201, response[:2]
        return response[1]

    def record(name, started_at, target, **details):
        elapsed = time.monotonic() - started_at
        result = {"name": name, "recovery_seconds": round(elapsed, 3),
                  "target_seconds": target, "met_target": elapsed <= target, **details}
        report["experiments"].append(result)
        print(json.dumps(result), flush=True)

    def get(event_id):
        return http(base, f"/events/{event_id}")

    marker = created()
    marker_id = marker["id"]
    production = backup.source_database()
    original = read_event(production, marker_id)
    try:
        before, began = crash("event-api")
        samples = []

        def api_recovered():
            response = get(marker_id)
            current = state("event-api")
            good = response[0] == 200 and response[1].get("id") == marker_id
            samples.append({"status": response[0], "seconds": response[3], "good": good})
            return current["id"] == before["id"] and current["restarts"] > before["restarts"] and good

        wait_for(api_recovered)
        record("api_crash", began, 30, samples=samples, sli=sample_summary(samples),
               availability_during_fault="temporary outage; single API")

        compose("stop", "event-worker-standby")
        before, began = crash("event-worker")
        wait_for(lambda: state("event-worker")["restarts"] > before["restarts"]
                 and queue()["consumers"] == 1)
        event = created()
        wait_for(lambda: received(event["id"], "event-worker"))
        record("worker_crash", began, 30, event_id=event["id"],
               availability_during_fault="API accepts messages; second worker disabled for this test")
        compose("start", "event-worker-standby")
        wait_for(lambda: queue()["consumers"] == 2)

        suspend("event-worker")
        began = time.monotonic()
        events = [created(), created()]
        wait_for(lambda: any(received(event["id"], "event-worker-standby") for event in events))
        record("worker_redundancy", began, 5, event_ids=[event["id"] for event in events],
               availability_during_fault="API 201; other worker continues; prefetch=1 on suspended worker")
        resume("event-worker")
        wait_for(lambda: all(received(event["id"]) for event in events))

        compose("stop", "redis")
        response = get(marker_id)
        assert response[0] == 200 and response[2]["x-cache"] == "BYPASS", response
        assert created()["id"]
        began = time.monotonic()
        compose("start", "redis")
        wait_for(lambda: get(marker_id)[2].get("x-cache") == "HIT")
        record("redis_return", began, 15, availability_during_fault="GET 200 BYPASS and POST 201")

        compose("exec", "-T", "redis", "redis-cli", "DEL", f"event:{marker_id}")
        compose("stop", "postgres")
        assert get(marker_id)[0] == 503
        assert http(base, "/health")[0] == 200
        began = time.monotonic()
        compose("start", "postgres")
        wait_for(lambda: get(marker_id)[0] == 200)
        assert read_event(production, marker_id) == original
        record("postgres_return", began, 60, event_id=marker_id,
               availability_during_fault="GET miss 503; liveness 200; data survives volume")

        suspend(*WORKERS)
        persistent = created()
        assert not received(persistent["id"])
        pending_before = queue()
        began = time.monotonic()
        compose("restart", "rabbitmq")

        def broker_ready():
            try:
                return queue()["ready"] >= 1
            except (RuntimeError, subprocess.CalledProcessError):
                return False

        wait_for(broker_ready)
        record("rabbitmq_restart", began, 60, event_id=persistent["id"],
               queue_before=pending_before, queue_after=queue(),
               availability_during_fault="reads work; publication unavailable during broker restart")
        began = time.monotonic()
        resume(*WORKERS)
        wait_for(lambda: received(persistent["id"]) and queue()["consumers"] == 2)
        record("consumer_reconnect", began, 30, confirmed_persistent_event=persistent["id"])

        began = time.monotonic()
        archive = backup.create(directory)
        metadata = backup.verify(archive)
        record("backup_create", began, 120, archive=str(archive), bytes=metadata["bytes"])
        after_snapshot = created()
        restore_database = "seps_restore_" + uuid4().hex[:16]
        began = time.monotonic()
        restored = backup.restore(archive, restore_database)
        assert read_event(restore_database, marker_id) == original
        assert read_event(restore_database, after_snapshot["id"]) is None
        assert read_event(production, after_snapshot["id"]) is not None
        record("backup_restore", began, 120, **restored, verified_event=marker_id,
               absent_after_snapshot=after_snapshot["id"], production_preserved=True)
    finally:
        # Preconditions require all services running; restore that initial state.
        for worker, pid in list(suspended.items()):
            signal_process(worker, pid, "SIGCONT", check=False)
        compose("start", *SERVICES)
        compose("up", "-d", "--wait", "--wait-timeout", "180")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--directory", type=Path, default=Path("backups"))
    parser.add_argument("--report", type=Path, default=Path("reports/reliability.json"))
    args = parser.parse_args()
    report = {"success": False}
    try:
        run(args.base_url.rstrip("/"), args.directory, report)
        report["completed_at"] = backup.utc_now()
        report["success"] = True
    finally:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
