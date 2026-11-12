import os

import pika

from app.models import Event

QUEUE_NAME = os.getenv("RABBITMQ_QUEUE", "security_events")


def connect():
    return pika.BlockingConnection(
        pika.ConnectionParameters(
            host=os.getenv("RABBITMQ_HOST", "rabbitmq"),
            port=int(os.getenv("RABBITMQ_PORT", "5672")),
            credentials=pika.PlainCredentials(
                os.environ["RABBITMQ_USER"], os.environ["RABBITMQ_PASSWORD"]
            ),
            heartbeat=30,
            socket_timeout=3,
            stack_timeout=6,
            blocked_connection_timeout=3,
            connection_attempts=3,
            retry_delay=1,
        )
    )


class EventPublisher:
    def publish(self, event: Event):
        # BlockingConnection не разделяется между потоками FastAPI.
        with connect() as connection:
            channel = connection.channel()
            channel.queue_declare(queue=QUEUE_NAME, durable=True)
            channel.confirm_delivery()
            channel.basic_publish(
                exchange="",
                routing_key=QUEUE_NAME,
                body=event.model_dump_json().encode("utf-8"),
                properties=pika.BasicProperties(
                    content_type="application/json",
                    delivery_mode=2,
                    message_id=str(event.id),
                ),
                mandatory=True,
            )
