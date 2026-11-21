import asyncio
import json
import logging
import signal
from typing import Protocol

from aiokafka import AIOKafkaConsumer

from app.config import settings
from app.kafka_client import LogBroker, _safe_stop_consumer
from app.ollama_client import OllamaLogParser
from app.schemas import ParsedLog

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("log_worker")


class ParserProtocol(Protocol):
    async def parse(self, raw_log: str) -> ParsedLog: ...


class ProducerProtocol(Protocol):
    async def send_raw(self, payload: bytes, topic: str | None = None) -> None: ...


async def process_raw_message(
    raw_text: str,
    parser: ParserProtocol,
    broker: ProducerProtocol,
    parsed_topic: str,
    dead_letter_topic: str,
) -> bool:
    """Parse one raw log string and route to parsed_topic or dead_letter_topic."""
    try:
        parsed = await parser.parse(raw_text)
        await broker.send_raw(
            parsed.model_dump_json().encode("utf-8"),
            topic=parsed_topic,
        )
        return True
    except Exception as exc:
        logger.warning("Failed to parse log line, sending to DLQ: %s", exc)
        dlq_payload = json.dumps({"raw": raw_text, "error": str(exc)}).encode("utf-8")
        await broker.send_raw(dlq_payload, topic=dead_letter_topic)
        return False


async def run_worker() -> None:
    broker = LogBroker(
        bootstrap_servers=settings.kafka_bootstrap_servers,
        default_topic=settings.default_topic,
        startup_retries=settings.startup_retries,
        startup_retry_delay=settings.startup_retry_delay,
    )
    parser = OllamaLogParser(
        base_url=settings.ollama_base_url,
        model=settings.ollama_model,
        timeout_seconds=settings.ollama_timeout_seconds,
    )

    await broker.start()
    await broker.ensure_topic(settings.parsed_topic)
    await broker.ensure_topic(settings.dead_letter_topic)

    consumer = AIOKafkaConsumer(
        settings.default_topic,
        bootstrap_servers=settings.kafka_bootstrap_servers,
        group_id=settings.consumer_group_id,
        auto_offset_reset="earliest",
        enable_auto_commit=False,
    )
    await consumer.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    logger.info(
        "Worker started: consuming '%s' -> Ollama (%s) -> '%s'",
        settings.default_topic,
        settings.ollama_model,
        settings.parsed_topic,
    )

    try:
        while not stop_event.is_set():
            batch = await consumer.getmany(timeout_ms=1000, max_records=10)
            for _tp, messages in batch.items():
                for msg in messages:
                    if stop_event.is_set():
                        break
                    if msg.value is None:
                        continue
                    raw_text = msg.value.decode("utf-8", errors="replace")
                    await process_raw_message(
                        raw_text=raw_text,
                        parser=parser,
                        broker=broker,
                        parsed_topic=settings.parsed_topic,
                        dead_letter_topic=settings.dead_letter_topic,
                    )
                    await consumer.commit()
    finally:
        await _safe_stop_consumer(consumer)
        await broker.stop()
        await parser.close()
        logger.info("Worker stopped.")


if __name__ == "__main__":
    asyncio.run(run_worker())
