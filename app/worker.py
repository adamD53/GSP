import asyncio
import json
import logging
import signal
from typing import Protocol

from aiokafka import AIOKafkaConsumer

from app.alerts import AlertStore
from app.config import settings
from app.kafka_client import LogBroker, _safe_stop_consumer
from app.notifications import dispatch_notifications, process_outbox_once
from app.ollama_client import OllamaLogParser
from app.parser import HybridLogParser, build_rule_eval_document
from app.rules import evaluate_per_rule, load_rules
from app.schemas import NotificationDecision, ParsedLog, RuleMatch

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
    alert_store: AlertStore | None = None,
) -> bool:
    """Parse one raw log string, evaluate rulebook + dedup alerts, and route."""
    try:
        parsed = await parser.parse(raw_text)
        active_rules_file = rules_file if rules_file is not None else settings.rules_file
        matches: list[RuleMatch] = []
        all_decisions: list[NotificationDecision] = []

        try:
            rules = load_rules(active_rules_file, strict=False)
        except Exception as rule_exc:
            logger.warning("Failed to load rules file: %s", rule_exc)
            rules = []

        eval_doc = build_rule_eval_document(parsed)
        for single_rule in rules:
            try:
                per_rule = evaluate_per_rule(eval_doc, [single_rule])
                for rule_match, rule_dict, decisions in per_rule:
                    matches.append(rule_match)
                    if alert_store is not None:
                        alert_rec, should_notify = alert_store.record_match(
                            parsed_log=parsed,
                            rule_match=rule_match,
                            rule_dict=rule_dict,
                            notifications=decisions,
                        )
                        if should_notify:
                            dispatched = await dispatch_notifications(
                                decisions,
                                log_summary=parsed.summary or parsed.message,
                                service=parsed.service,
                            )
                            alert_store.update_notifications(alert_rec.id, dispatched)
                            if any(d.status == "failed" for d in dispatched):
                                alert_store.enqueue_failed_notifications(
                                    alert_id=alert_rec.id,
                                    decisions=dispatched,
                                    log_summary=parsed.summary or parsed.message,
                                    service=parsed.service,
                                )
                            all_decisions.extend(dispatched)
                        else:
                            suppressed = [
                                d.model_copy(
                                    update={
                                        "status": "suppressed",
                                        "reason": (
                                            f"Deduplicated into {alert_rec.id} "
                                            f"(occurrence #{alert_rec.count} within "
                                            f"{alert_rec.dedup_window_seconds}s)"
                                        ),
                                    }
                                )
                                for d in decisions
                            ]
                            all_decisions.extend(suppressed)
                    else:
                        dispatched = await dispatch_notifications(
                            decisions,
                            log_summary=parsed.summary or parsed.message,
                            service=parsed.service,
                        )
                        all_decisions.extend(dispatched)
            except Exception as rule_exc:
                logger.warning(
                    "Skipping rule '%s' due to error: %s",
                    single_rule.get("id", "unknown") if isinstance(single_rule, dict) else "unknown",
                    rule_exc,
                )
                continue

        if matches:
            parsed = parsed.model_copy(
                update={
                    "matched_rules": matches,
                    "notifications": all_decisions,
                    "alert": True,
                }
            )
            logger.info(
                "Rule match (%s) on service=%s level=%s -> notifications=%s",
                ", ".join(m.id for m in matches),
                parsed.service,
                parsed.level,
                ", ".join(f"{d.channel}:{d.status}" for d in all_decisions) or "none",
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


async def process_batch(
    raw_messages: list[str],
    parser: ParserProtocol,
    broker: ProducerProtocol,
    parsed_topic: str,
    dead_letter_topic: str,
    concurrency: int = 16,
    rules_file: str | None = None,
    alert_store: AlertStore | None = None,
) -> list[bool]:
    """Process a batch of raw log strings concurrently with bounded parallelism."""
    if not raw_messages:
        return []
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _run_one(raw_text: str) -> bool:
        async with sem:
            return await process_raw_message(
                raw_text=raw_text,
                parser=parser,
                broker=broker,
                parsed_topic=parsed_topic,
                dead_letter_topic=dead_letter_topic,
                rules_file=rules_file,
                alert_store=alert_store,
            )

    return list(await asyncio.gather(*(_run_one(raw) for raw in raw_messages)))


async def run_worker() -> None:
    broker = LogBroker(
        bootstrap_servers=settings.kafka_bootstrap_servers,
        default_topic=settings.default_topic,
        startup_retries=settings.startup_retries,
        startup_retry_delay=settings.startup_retry_delay,
    )
    llm_parser = OllamaLogParser(
        base_url=settings.ollama_base_url,
        model=settings.ollama_model,
        timeout_seconds=settings.ollama_timeout_seconds,
    )
    parser = HybridLogParser(
        llm_parser=llm_parser,
        enable_fast_parser=settings.enable_fast_parser,
        enable_schema_learning=settings.enable_schema_learning,
    )
    alert_store = AlertStore(settings.alerts_db_path)

    await broker.start()
    await broker.ensure_topic(settings.parsed_topic)
    await broker.ensure_topic(settings.dead_letter_topic)
    await broker.ensure_topic(settings.notification_dead_letter_topic)

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

    async def _outbox_retry_loop() -> None:
        while not stop_event.is_set():
            try:
                await process_outbox_once(
                    alert_store=alert_store,
                    broker=broker,
                    dead_letter_topic=settings.notification_dead_letter_topic,
                )
            except Exception as exc:
                logger.warning("Outbox retry pass failed: %s", exc)
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=settings.notification_retry_poll_seconds,
                )
            except asyncio.TimeoutError:
                pass

    retry_task = asyncio.create_task(_outbox_retry_loop())

    logger.info(
        "Worker started: consuming '%s' -> HybridParser (fast=%s, schema_learn=%s, llm=%s, concurrency=%d) -> '%s'",
        settings.default_topic,
        settings.enable_fast_parser,
        settings.enable_schema_learning,
        settings.ollama_model,
        settings.worker_concurrency,
        settings.parsed_topic,
    )

    try:
        while not stop_event.is_set():
            batch = await consumer.getmany(
                timeout_ms=500,
                max_records=settings.worker_batch_size,
            )
            raw_messages = [
                msg.value.decode("utf-8", errors="replace")
                for messages in batch.values()
                for msg in messages
                if msg.value is not None
            ]
            if not raw_messages:
                continue

            await process_batch(
                raw_messages=raw_messages,
                parser=parser,
                broker=broker,
                parsed_topic=settings.parsed_topic,
                dead_letter_topic=settings.dead_letter_topic,
                concurrency=settings.worker_concurrency,
                alert_store=alert_store,
            )
            await consumer.commit()
    finally:
        stop_event.set()
        retry_task.cancel()
        try:
            await retry_task
        except asyncio.CancelledError:
            pass
        await _safe_stop_consumer(consumer)
        await broker.stop()
        await parser.close()
        logger.info("Worker stopped.")


if __name__ == "__main__":
    asyncio.run(run_worker())
