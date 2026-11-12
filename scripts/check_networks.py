"""Read-only TCP/Firewall checks. Run from repository root with the stack running.

Uses only the Python standard library and Docker CLI; no host firewall changes.
"""
import json
import re
import subprocess
import urllib.request

SERVICES = ("event-api", "event-worker", "postgres", "redis", "rabbitmq")
NETWORKS = {
    "event-api": {"api-db", "api-cache", "api-mq"},
    "event-worker": {"worker-mq"},
    "postgres": {"api-db"},
    "redis": {"api-cache"},
    "rabbitmq": {"api-mq", "worker-mq"},
}
TARGETS = (("event-api", 8000), ("postgres", 5432), ("redis", 6379),
           ("rabbitmq", 5672), ("rabbitmq", 15672))
ALLOWED = {("event-api", "postgres", 5432), ("event-api", "redis", 6379),
           ("event-api", "rabbitmq", 5672), ("event-worker", "rabbitmq", 5672)}


def run(*args, check=True):
    return subprocess.run(args, check=check, text=True, encoding="utf-8",
                          capture_output=True, timeout=45)


def compose(*args, check=True):
    return run("docker", "compose", *args, check=check)


def endpoints(service):
    container_id = compose("ps", "-q", service).stdout.strip()
    assert container_id, f"{service} is not running"
    raw = run("docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}",
              container_id).stdout
    return {key: value["IPAddress"] for key, value in json.loads(raw).items()}


def ip_for(source, target):
    shared = set(source) & set(target)
    # Для запрещённых направлений общей сети может не быть. Всё равно проверяем
    # настоящий IP назначения, не пытаясь разрешать недоступное DNS-имя.
    network = sorted(shared or set(target))[0]
    return target[network]


def output_drops(service):
    text = compose("exec", "-T", "--user", "0", service,
                   "iptables", "-nvxL", "OUTPUT").stdout
    return int(re.search(r"policy DROP (\d+) packets", text).group(1))


def main():
    addresses = {service: endpoints(service) for service in SERVICES}
    for service, networks in addresses.items():
        logical = {name.rsplit("_", 1)[-1] for name in networks}
        assert logical == NETWORKS[service], (service, logical)
        for binary in ("iptables", "ip6tables"):
            rules = compose("exec", "-T", "--user", "0", service, binary, "-S").stdout
            for chain in ("INPUT", "OUTPUT", "FORWARD"):
                assert f"-P {chain} DROP" in rules, (service, binary, chain)
        status = compose("exec", "-T", service, "cat", "/proc/1/status").stdout
        fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
        for field in ("CapEff", "CapPrm", "CapBnd"):
            assert not int(fields[field].strip(), 16) & (1 << 12), (service, field)
        if service in ("event-api", "event-worker"):
            assert set(fields["Uid"].split()) == {"10001"}, service
        print(f"PASS policy/capabilities/networks: {service}", flush=True)

    # Подтверждаем живые слушатели; затем отказ не объясняется закрытым портом.
    for source, target, port in sorted(ALLOWED):
        ip = ip_for(addresses[source], addresses[target])
        compose("exec", "-T", "--user", "10001", source,
                "python", "-m", "app.tcp_probe", ip, str(port), "--expect", "allow")
        print(f"PASS ALLOW {source} -> {target} {ip}:{port}", flush=True)
    for url in ("http://127.0.0.1:8000/health", "http://127.0.0.1:15672/"):
        with urllib.request.urlopen(url, timeout=5) as response:
            assert response.status == 200, url

    before = output_drops("event-worker")
    for source in SERVICES:
        for target, port in TARGETS:
            if source == target or (source, target, port) in ALLOWED:
                continue
            ip = ip_for(addresses[source], addresses[target])
            if source in ("event-api", "event-worker"):
                result = compose("exec", "-T", "--user", "10001", source,
                                 "python", "-m", "app.tcp_probe", ip, str(port),
                                 "--expect", "deny", check=False)
                detail = json.loads(result.stdout)
                assert result.returncode == 0, (source, target, detail)
                reason = detail["result"]
            else:
                result = compose("exec", "-T", source, "nc", "-n", "-v", "-z",
                                 "-w", "2", ip, str(port), check=False)
                reason = (result.stdout + result.stderr).strip()
                assert result.returncode != 0 and any(word in reason.lower() for word in (
                    "timed out", "unreachable", "no route", "operation not permitted",
                )), (source, target, reason)
            print(f"PASS DENY {source} -> {target} {ip}:{port}: {reason}", flush=True)

    after = output_drops("event-worker")
    assert after > before, "No OUTPUT DROP counter increase on Worker"
    print(f"PASS explicit firewall: Worker OUTPUT DROP packets {before} -> {after}")
    print("PASS: 4 allowed directions and all other cross-service targets checked by IP")


if __name__ == "__main__":
    main()
