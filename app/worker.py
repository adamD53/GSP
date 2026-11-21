import asyncio
import json
import logging
import signal
from typing import Protocol

from aiokafka import AIOKafkaConsumer

from app.config import settings
from app.kafka_client import LogBroker, _safe_stop_consumer
from app.notifications import dispatch_notifications
from app.ollama_client import OllamaLogParser
from app.rules import evaluate_and_route_file
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
    rules_file: str | None = None,
) -> bool:
    """Parse one raw log string, evaluate rulebook, and route to parsed or DLQ."""
    try:
        parsed = await parser.parse(raw_text)
        active_rules_file = rules_file if rules_file is not None else settings.rules_file
        try:
            matches, decisions = evaluate_and_route_file(
                parsed.model_dump(),
                active_rules_file,
            )
            if decisions:
                decisions = await dispatch_notifications(
                    decisions,
                    log_summary=parsed.summary or parsed.message,
                    service=parsed.service,
                )
        except Exception as rule_exc:
            logger.warning("Rule evaluation skipped due to error: %s", rule_exc)
            matches, decisions = [], []

        if matches:
            parsed = parsed.model_copy(
                update={
                    "matched_rules": matches,
                    "notifications": decisions,
                    "alert": True,
                }
            )
            logger.info(
                "Rule match (%s) on service=%s level=%s -> notifications=%s",
                ", ".join(m.id for m in matches),
                parsed.service,
                parsed.level,
                ", ".join(f"{d.channel}:{d.status}" for d in decisions) or "none",
            )

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
