import asyncio
import time
from collections.abc import AsyncIterator

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from aiokafka.errors import KafkaConnectionError, TopicAlreadyExistsError


async def _safe_stop_consumer(consumer: AIOKafkaConsumer) -> None:
    try:
        await consumer.stop()
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is not None and task.cancelling() > 0:
            raise


class LogBroker:
    """Thin aiokafka wrapper for producing and reading raw log strings from Redpanda."""

    def __init__(
        self,
        bootstrap_servers: str,
        default_topic: str,
        startup_retries: int = 10,
        startup_retry_delay: float = 1.5,
    ) -> None:
        self.bootstrap_servers = bootstrap_servers
        self.default_topic = default_topic
        self.startup_retries = startup_retries
        self.startup_retry_delay = startup_retry_delay
        self._producer: AIOKafkaProducer | None = None

    async def start(self) -> None:
        last_err: Exception | None = None
        for attempt in range(1, self.startup_retries + 1):
            try:
                await self.ensure_topic(self.default_topic)
                producer = AIOKafkaProducer(bootstrap_servers=self.bootstrap_servers)
                await producer.start()
                self._producer = producer
                return
            except (KafkaConnectionError, OSError) as err:
                last_err = err
                if attempt < self.startup_retries:
                    await asyncio.sleep(self.startup_retry_delay)
        raise RuntimeError(
            f"Could not connect to Redpanda at {self.bootstrap_servers}"
        ) from last_err

    async def stop(self) -> None:
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None

    async def ensure_topic(self, topic: str) -> None:
        admin = AIOKafkaAdminClient(bootstrap_servers=self.bootstrap_servers)
        await admin.start()
        try:
            await admin.create_topics(
                [NewTopic(name=topic, num_partitions=1, replication_factor=1)]
            )
        except TopicAlreadyExistsError:
            pass
        finally:
            await admin.close()

    async def send_raw(self, payload: bytes, topic: str | None = None) -> None:
        if self._producer is None:
            raise RuntimeError("LogBroker has not been started")
        target_topic = topic or self.default_topic
        await self._producer.send_and_wait(
            target_topic,
            value=payload,
            timestamp_ms=int(time.time() * 1000),
        )

    async def read_logs(
        self,
        topic: str | None = None,
        limit: int = 100,
        from_beginning: bool = True,
        timeout_ms: int = 2000,
    ) -> str:
        target_topic = topic or self.default_topic
        consumer = AIOKafkaConsumer(
            bootstrap_servers=self.bootstrap_servers,
            group_id=None,
            enable_auto_commit=False,
        )
        await consumer.start()
        try:
            metadata = await consumer._client.fetch_all_metadata()
            partitions = metadata.partitions_for_topic(target_topic)
            if not partitions:
                return ""

            tps = [TopicPartition(target_topic, p) for p in sorted(partitions)]
            consumer.assign(tps)

            beginning = await consumer.beginning_offsets(tps)
            end = await consumer.end_offsets(tps)

            remaining_targets: dict[TopicPartition, int] = {}
            for tp in tps:
                start_offset = (
                    beginning[tp]
                    if from_beginning
                    else max(beginning[tp], end[tp] - limit)
                )
                if start_offset < end[tp]:
                    consumer.seek(tp, start_offset)
                    remaining_targets[tp] = end[tp]

            if not remaining_targets:
                return ""

            lines: list[str] = []
            while remaining_targets and len(lines) < limit:
                batch = await consumer.getmany(
                    timeout_ms=timeout_ms,
                    max_records=limit - len(lines),
                )
                if not batch:
                    break
                for tp, messages in batch.items():
                    for msg in messages:
                        if msg.value is not None:
                            lines.append(msg.value.decode("utf-8", errors="replace"))
                        if tp in remaining_targets and msg.offset + 1 >= remaining_targets[tp]:
                            remaining_targets.pop(tp, None)
                        if len(lines) >= limit:
                            break

            return "\n".join(lines)
        finally:
            await _safe_stop_consumer(consumer)

    async def stream_logs(
        self,
        topic: str | None = None,
        from_beginning: bool = False,
    ) -> AsyncIterator[str]:
        target_topic = topic or self.default_topic
        await self.ensure_topic(target_topic)

        consumer = AIOKafkaConsumer(
            bootstrap_servers=self.bootstrap_servers,
            group_id=None,
            enable_auto_commit=False,
        )
        await consumer.start()
        try:
            metadata = await consumer._client.fetch_all_metadata()
            partitions = metadata.partitions_for_topic(target_topic) or {0}
            tps = [TopicPartition(target_topic, p) for p in sorted(partitions)]
            consumer.assign(tps)
            if from_beginning:
                await consumer.seek_to_beginning(*tps)
            else:
                await consumer.seek_to_end(*tps)

            # Flush HTTP response headers immediately so proxies/browsers don't stall
            yield "\n"

            while True:
                batch = await consumer.getmany(timeout_ms=5000, max_records=50)
                if not batch:
                    # Periodic heartbeat lets Starlette detect disconnected clients
                    yield "\n"
                    continue
                for _tp, messages in batch.items():
                    for msg in messages:
                        if msg.value is not None:
                            yield msg.value.decode("utf-8", errors="replace") + "\n"
        finally:
            await _safe_stop_consumer(consumer)
