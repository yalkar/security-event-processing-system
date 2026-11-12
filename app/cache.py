import logging
import os
from uuid import UUID

from pydantic import ValidationError
from redis import Redis, RedisError
from redis.backoff import NoBackoff
from redis.retry import Retry

from app.models import Event

logger = logging.getLogger(__name__)


class EventCache:
    def __init__(self):
        self.ttl = int(os.getenv("CACHE_TTL_SECONDS", "60"))
        if self.ttl < 1:
            raise ValueError("CACHE_TTL_SECONDS must be positive")
        self.client = Redis(
            host=os.getenv("REDIS_HOST", "redis"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
            retry=Retry(NoBackoff(), 0),
        )

    def get(self, event_id: UUID) -> tuple[Event | None, str]:
        try:
            value = self.client.get(f"event:{event_id}")
        except RedisError:
            logger.warning("cache_bypass event_id=%s", event_id)
            return None, "BYPASS"
        if value is not None:
            try:
                event = Event.model_validate_json(value)
                if event.id == event_id:
                    return event, "HIT"
            except ValidationError:
                pass
            logger.warning("invalid_cache_entry event_id=%s", event_id)
        return None, "MISS"

    def put(self, event: Event):
        try:
            self.client.set(f"event:{event.id}", event.model_dump_json(), ex=self.ttl)
        except RedisError:
            logger.warning("cache_write_failed event_id=%s", event.id)

    def close(self):
        self.client.close()
