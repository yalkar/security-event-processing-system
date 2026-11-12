import asyncio
import logging
from contextlib import asynccontextmanager
from uuid import UUID

import pika
import psycopg
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from app.cache import EventCache
from app.database import EventStore
from app.messaging import EventPublisher
from app.models import Event, EventCreate

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.store = EventStore()
    app.state.cache = EventCache()
    app.state.publisher = EventPublisher()
    try:
        await asyncio.to_thread(app.state.store.initialize)
        yield
    finally:
        app.state.cache.close()


def create_app() -> FastAPI:
    app = FastAPI(title="SEPS Event API", version="0.2.0", lifespan=lifespan)

    @app.exception_handler(psycopg.Error)
    async def database_unavailable(request: Request, error: psycopg.Error):
        logger.error("database_request_failed error_type=%s", type(error).__name__)
        return JSONResponse(status_code=503, content={"detail": "PostgreSQL unavailable"})

    @app.get("/")
    def hello_world() -> dict[str, str]:
        return {"message": "Hello World"}

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/events", response_model=Event, status_code=201)
    def create_event(data: EventCreate, request: Request, response: Response):
        event = request.app.state.store.create(data)
        request.app.state.cache.put(event)
        try:
            request.app.state.publisher.publish(event)
        except (pika.exceptions.AMQPError, OSError):
            logger.error("publication_unconfirmed event_id=%s", event.id)
            raise HTTPException(
                status_code=503,
                detail={
                    "message": "Event saved; RabbitMQ publication not confirmed",
                    "event_id": str(event.id),
                },
                headers={"Location": f"/events/{event.id}"},
            ) from None
        response.headers["Location"] = f"/events/{event.id}"
        return event

    @app.get("/events/{event_id}", response_model=Event)
    def get_event(event_id: UUID, request: Request, response: Response):
        event, cache_status = request.app.state.cache.get(event_id)
        if event is None:
            event = request.app.state.store.get(event_id)
            if event is None:
                raise HTTPException(status_code=404, detail="Event not found")
            # BYPASS уже установил недоступность Redis в этом запросе.
            if cache_status == "MISS":
                request.app.state.cache.put(event)
        logger.info("event_read event_id=%s cache=%s", event_id, cache_status)
        response.headers["X-Cache"] = cache_status
        return event

    return app


app = create_app()
