from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pika
import psycopg
import pytest
from fastapi.testclient import TestClient
from redis import ConnectionError as RedisConnectionError

from app.cache import EventCache
from app.main import create_app
from app.models import Event
from app.messaging import EventPublisher, QUEUE_NAME
from app.worker import handle_message

PAYLOAD = {"event_type": "failed_login", "source_ip": "192.0.2.10", "username": "demo"}


@pytest.fixture
def event():
    return Event(**PAYLOAD, id=uuid4(), created_at=datetime.now(timezone.utc))


@pytest.fixture
def api(event):
    app = create_app()
    # Здесь проверяем HTTP-логику без инфраструктуры и без запуска lifespan.
    app.state.store = Mock()
    app.state.store.create.return_value = event
    app.state.store.get.return_value = event
    app.state.cache = Mock()
    app.state.cache.get.return_value = (None, "MISS")
    app.state.publisher = Mock()
    client = TestClient(app)
    yield client, app.state
    client.close()


def test_hello_and_health(api):
    client, _ = api
    assert client.get("/").json() == {"message": "Hello World"}
    assert client.get("/health").json() == {"status": "ok"}


def test_create_saves_then_caches_then_publishes(api, event):
    client, state = api
    calls = Mock()
    calls.attach_mock(state.store.create, "save")
    calls.attach_mock(state.cache.put, "cache")
    calls.attach_mock(state.publisher.publish, "publish")
    response = client.post("/events", json=PAYLOAD)
    assert response.status_code == 201
    assert response.json() == event.model_dump(mode="json")
    assert response.headers["location"] == f"/events/{event.id}"
    assert [call[0] for call in calls.mock_calls] == ["save", "cache", "publish"]


@pytest.mark.parametrize("payload", [
    {**PAYLOAD, "source_ip": "not-an-ip"},
    {**PAYLOAD, "event_type": "   "},
    {**PAYLOAD, "unexpected": "field"},
    {"event_type": "failed_login", "source_ip": "192.0.2.10"},
])
def test_invalid_input_has_no_side_effects(api, payload):
    client, state = api
    assert client.post("/events", json=payload).status_code == 422
    state.store.create.assert_not_called()
    state.cache.put.assert_not_called()
    state.publisher.publish.assert_not_called()


def test_cache_hit_does_not_query_database(api, event):
    client, state = api
    state.cache.get.return_value = (event, "HIT")
    response = client.get(f"/events/{event.id}")
    assert response.status_code == 200
    assert response.headers["x-cache"] == "HIT"
    assert response.json() == event.model_dump(mode="json")
    state.store.get.assert_not_called()


def test_cache_miss_loads_database_and_fills_cache(api, event):
    client, state = api
    response = client.get(f"/events/{event.id}")
    assert response.status_code == 200
    assert response.headers["x-cache"] == "MISS"
    state.store.get.assert_called_once_with(event.id)
    state.cache.put.assert_called_once_with(event)


def test_missing_event_and_invalid_id(api):
    client, state = api
    state.store.get.return_value = None
    assert client.get(f"/events/{uuid4()}").status_code == 404
    assert client.get("/events/not-a-uuid").status_code == 422
    state.cache.put.assert_not_called()


def test_database_failure_does_not_publish(api):
    client, state = api
    state.store.create.side_effect = psycopg.OperationalError("database down")
    assert client.post("/events", json=PAYLOAD).status_code == 503
    state.cache.put.assert_not_called()
    state.publisher.publish.assert_not_called()


def test_failed_publish_reports_already_saved_event(api, event):
    client, state = api
    state.publisher.publish.side_effect = pika.exceptions.AMQPConnectionError()
    response = client.post("/events", json=PAYLOAD)
    assert response.status_code == 503
    assert response.json()["detail"]["event_id"] == str(event.id)
    assert response.headers["location"] == f"/events/{event.id}"
    state.store.create.assert_called_once()


def test_redis_outage_falls_back_to_database(api, event):
    client, state = api
    cache = EventCache()
    cache.client.close()
    cache.client = Mock()
    cache.client.get.side_effect = RedisConnectionError()
    cache.client.set.side_effect = RedisConnectionError()
    state.cache = cache
    response = client.get(f"/events/{event.id}")
    assert response.status_code == 200
    assert response.headers["x-cache"] == "BYPASS"
    assert response.json()["id"] == str(event.id)
    assert client.post("/events", json=PAYLOAD).status_code == 201


@pytest.mark.parametrize("cached", ["not-json", "{}"])
def test_corrupt_cache_is_a_miss(cached, event):
    cache = EventCache()
    cache.client.close()
    cache.client = Mock()
    cache.client.get.return_value = cached
    assert cache.get(event.id) == (None, "MISS")


def test_worker_acknowledges_after_logging(monkeypatch, event):
    calls = []
    channel = Mock()
    channel.basic_ack.side_effect = lambda **kwargs: calls.append("ack")
    monkeypatch.setattr("app.worker.logger.info", lambda *args: calls.append("log"))
    handle_message(channel, SimpleNamespace(delivery_tag=7, redelivered=False), None,
                   event.model_dump_json().encode())
    assert calls == ["log", "ack"]
    channel.basic_ack.assert_called_once_with(delivery_tag=7)
    channel.basic_reject.assert_not_called()


def test_worker_rejects_invalid_message():
    channel = Mock()
    handle_message(channel, SimpleNamespace(delivery_tag=8), None, b"not-json")
    channel.basic_reject.assert_called_once_with(delivery_tag=8, requeue=False)
    channel.basic_ack.assert_not_called()


def test_worker_requeues_on_processing_error(monkeypatch, event):
    channel = Mock()
    monkeypatch.setattr("app.worker.logger.info", Mock(side_effect=RuntimeError()))
    with pytest.raises(RuntimeError):
        handle_message(channel, SimpleNamespace(delivery_tag=9, redelivered=False), None,
                       event.model_dump_json().encode())
    channel.basic_nack.assert_called_once_with(delivery_tag=9, requeue=True)
    channel.basic_ack.assert_not_called()


def test_publisher_uses_confirms_and_persistent_messages(monkeypatch, event):
    connection = Mock()
    context = Mock()
    context.__enter__ = Mock(return_value=connection)
    context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr("app.messaging.connect", Mock(return_value=context))
    EventPublisher().publish(event)
    channel = connection.channel.return_value
    channel.queue_declare.assert_called_once_with(queue=QUEUE_NAME, durable=True)
    channel.confirm_delivery.assert_called_once()
    arguments = channel.basic_publish.call_args.kwargs
    assert arguments["mandatory"] is True
    assert arguments["routing_key"] == QUEUE_NAME
    assert arguments["properties"].delivery_mode == 2
    assert arguments["properties"].message_id == str(event.id)
    assert Event.model_validate_json(arguments["body"]) == event
