"""Запуск: SEPS_INTEGRATION=1 python -m pytest tests/test_integration.py -v.

Нужны запущенный Compose, Docker CLI и свободно работающий Worker.
Тест создаёт одно синтетическое событие и оставляет его для просмотра.
"""
import json
import os
import subprocess
import time
from uuid import uuid4

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("SEPS_INTEGRATION") != "1", reason="requires running Docker Compose"
)


def compose(*args):
    return subprocess.check_output(
        ["docker", "compose", *args], text=True, encoding="utf-8"
    ).strip()


def test_postgres_cache_and_consumer():
    with httpx.Client(base_url=os.getenv("SEPS_API_URL", "http://127.0.0.1:8000"),
                      timeout=40, trust_env=False) as client:
        payload = {"event_type": "failed_login", "source_ip": "192.0.2.10",
                   "username": f"test-{uuid4().hex[:12]}"}
        created = client.post("/events", json=payload)
        assert created.status_code == 201, created.text
        event = created.json()
        event_id = event["id"]
        row = compose("exec", "-T", "event-api", "python", "-c",
                      "import sys; from app.database import EventStore; "
                      "from uuid import UUID; "
                      "print(EventStore().get(UUID(sys.argv[1])).model_dump_json())", event_id)
        assert json.loads(row) == event
        hit = client.get(f"/events/{event_id}")
        assert hit.headers["x-cache"] == "HIT"
        compose("exec", "-T", "redis", "redis-cli", "DEL", f"event:{event_id}")
        miss = client.get(f"/events/{event_id}")
        assert miss.headers["x-cache"] == "MISS"
        assert miss.json() == event
        assert int(compose("exec", "-T", "redis", "redis-cli", "TTL", f"event:{event_id}")) > 0
        assert client.get(f"/events/{event_id}").headers["x-cache"] == "HIT"
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            logs = compose("logs", "--since=2m", "event-worker")
            if any("received_event" in line and event_id in line for line in logs.splitlines()):
                break
            time.sleep(0.5)
        else:
            pytest.fail(f"Worker did not log event {event_id}")
        assert client.get(f"/events/{uuid4()}").status_code == 404
        assert client.post("/events", json={**payload, "source_ip": "bad"}).status_code == 422
        print(f"Verified event: {event_id}")
