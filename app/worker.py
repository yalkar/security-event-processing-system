import logging
import signal
import time

from pydantic import ValidationError

from app.messaging import QUEUE_NAME, connect
from app.models import Event

logger = logging.getLogger(__name__)


def handle_message(channel, method, properties, body):
    try:
        event = Event.model_validate_json(body)
    except ValidationError:
        logger.warning("invalid_message rejected; delivery_tag=%s", method.delivery_tag)
        channel.basic_reject(delivery_tag=method.delivery_tag, requeue=False)
        return

    try:
        # JSON сериализация экранирует переводы строк в пользовательских полях.
        logger.info(
            "received_event redelivered=%s event=%s",
            method.redelivered,
            event.model_dump_json(),
        )
    except Exception:
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
        raise
    channel.basic_ack(delivery_tag=method.delivery_tag)


def run():
    while True:
        try:
            with connect() as connection:
                channel = connection.channel()
                channel.queue_declare(queue=QUEUE_NAME, durable=True)
                channel.basic_qos(prefetch_count=1)
                channel.basic_consume(
                    queue=QUEUE_NAME, on_message_callback=handle_message, auto_ack=False
                )
                logger.info("worker_connected queue=%s", QUEUE_NAME)
                channel.start_consuming()
        except Exception as error:
            # Не выводим параметры соединения или тело некорректного сообщения.
            logger.warning("worker_reconnecting error_type=%s", type(error).__name__)
        time.sleep(2)


def stop(signum, frame):
    # Контекст закроет соединение: неподтверждённые сообщения вернутся в очередь.
    raise KeyboardInterrupt


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, stop)
    try:
        run()
    except KeyboardInterrupt:
        logger.info("worker_stopped")
