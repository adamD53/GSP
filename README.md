# Redpanda Raw Log Receiver

Docker-based log receiver powered by **Redpanda** and **FastAPI (`aiokafka`)**.
Accepts logs over **HTTP** or **Kafka** and returns raw plain-text log strings (`text/plain; charset=utf-8`).

## Quickstart

```bash
docker compose up -d --build
```

If ports `8000` or `19092` are already in use on your host, override them:

```bash
API_PORT=8088 KAFKA_EXTERNAL_PORT=29092 docker compose up -d --build
```

Services started:
- **FastAPI Receiver**: `http://localhost:8000`
- **Redpanda Broker (Kafka API)**: `localhost:19092` (host) / `redpanda:9092` (Docker network)

## Endpoints

| Method | Path | Description |
| ------ | ---- | ----------- |
| `GET` | `/health` | Returns `ok` |
| `POST` | `/logs?topic=raw-logs` | Ingests raw request body into Redpanda and returns the raw string |
| `GET` | `/logs?topic=raw-logs&limit=100&from_beginning=true` | Returns stored logs as newline-delimited raw text |
| `GET` | `/logs/stream?topic=raw-logs&from_beginning=false` | Streams incoming logs live as raw text lines |

## Examples

### Send via HTTP

```bash
curl -X POST http://localhost:8000/logs \
  -H "Content-Type: text/plain" \
  -d "2026-11-21 INFO payment-service charge succeeded"
```

### Send via Kafka (directly to Redpanda)

Using `rpk` inside the Redpanda container:

```bash
echo "2026-11-21 ERROR worker-service job failed" | docker compose exec -T redpanda rpk topic produce raw-logs
```

Or from Python using `aiokafka`:

```python
import asyncio
from aiokafka import AIOKafkaProducer

async def main():
    producer = AIOKafkaProducer(bootstrap_servers="localhost:19092")
    await producer.start()
    try:
        await producer.send_and_wait("raw-logs", b"INFO kafka-service hello")
    finally:
        await producer.stop()

asyncio.run(main())
```

### Read raw string output in Python

```python
import httpx

# Snapshot as a plain Python str
raw_logs: str = httpx.get("http://localhost:8000/logs").text
print(raw_logs)

# Live stream line by line
with httpx.stream("GET", "http://localhost:8000/logs/stream") as resp:
    for line in resp.iter_lines():
        print(line)
```

## Running Unit Tests

```bash
pytest -v
```
