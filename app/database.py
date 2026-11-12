import logging
import os
import time
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from app.models import Event, EventCreate

logger = logging.getLogger(__name__)


class EventStore:
    def __init__(self):
        self.parameters = dict(
            host=os.getenv("POSTGRES_HOST", "postgres"),
            port=int(os.getenv("POSTGRES_PORT", "5432")),
            dbname=os.environ["POSTGRES_DB"],
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            connect_timeout=3,
            options="-c statement_timeout=5000 -c lock_timeout=3000",
            row_factory=dict_row,
        )

    def connect(self):
        # Новое соединение на операцию: после рестарта БД следующий запрос
        # подключится заново. INSERT автоматически не повторяем.
        return psycopg.connect(**self.parameters)

    def initialize(self):
        for attempt in range(10):
            try:
                with self.connect() as connection:
                    connection.execute("""
                        CREATE TABLE IF NOT EXISTS events (
                            id UUID PRIMARY KEY,
                            event_type VARCHAR(64) NOT NULL,
                            source_ip INET NOT NULL,
                            username VARCHAR(128) NOT NULL,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                        )
                    """)
                return
            except psycopg.OperationalError:
                if attempt == 9:
                    raise
                logger.warning("PostgreSQL unavailable during initialization; retrying")
                time.sleep(2)

    def create(self, data: EventCreate) -> Event:
        with self.connect() as connection:
            row = connection.execute(
                """INSERT INTO events (id, event_type, source_ip, username)
                   VALUES (%s, %s, %s, %s) RETURNING *""",
                (uuid4(), data.event_type, str(data.source_ip), data.username),
            ).fetchone()
        # Контекст соединения уже зафиксировал транзакцию.
        return Event.model_validate(row)

    def get(self, event_id: UUID) -> Event | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM events WHERE id = %s", (event_id,)
            ).fetchone()
        return Event.model_validate(row) if row else None
