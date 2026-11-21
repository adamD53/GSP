# Redpanda Raw & Ollama Parsed Log Receiver

Docker-based log receiver powered by **Redpanda**, **FastAPI (`aiokafka`)**, and **Ollama**.
Accepts logs over **HTTP** or **Kafka**, serves raw plain-text strings, and runs an async Ollama worker that converts every raw log into schema-validated JSON returned directly via the API.

## Quickstart

```bash
docker compose up -d --build
```

If ports `8000` or `19092` are already in use on your host, override them:

```bash
API_PORT=8088 KAFKA_EXTERNAL_PORT=29092 docker compose up -d --build
```

> On first startup, `ollama-init` pulls `qwen2.5:1.5b` (~1 GB) into the persistent `ollama_data` volume.

Services started:
- **Next.js Log Observatory UI**: `http://localhost:3000`
- **FastAPI Receiver**: `http://localhost:8000`
- **Redpanda Broker (Kafka API)**: `localhost:19092` (host) / `redpanda:9092` (Docker network)
- **Ollama**: internal `http://ollama:11434` (with `ollama-init` model puller)
- **Async Worker (`app.worker`)**: consumes `raw-logs` → parses via Ollama → publishes to `parsed-logs` (or `dead-letter-logs` on error)

### Running the Frontend Locally

```bash
cd frontend
npm install
API_BASE_URL=http://localhost:8000 npm run dev
```

## Endpoints

| Method | Path | Response Type | Description |
| ------ | ---- | ------------- | ----------- |
| `GET` | `/health` | `text/plain` | Returns `ok` |
| `POST` | `/logs?topic=raw-logs` | `text/plain` | Ingests raw body into Redpanda and echoes raw string |
| `GET` | `/logs?topic=raw-logs&limit=100&from_beginning=true` | `text/plain` | Returns stored raw logs as newline-delimited string |
| `GET` | `/logs/stream?topic=raw-logs&from_beginning=false` | `text/plain` | Streams incoming raw log lines live |
| `GET` | `/logs/parsed?limit=100&from_beginning=true` | `application/json` | Returns Ollama-parsed structured logs as a JSON array |
| `GET` | `/logs/parsed/stream?from_beginning=false` | `application/x-ndjson` | Streams parsed JSON objects live (one per line) |
| `GET` | `/logs/dead-letter?limit=100` | `application/json` | Returns logs that failed parsing with error details |

## Examples

### Send via HTTP

```bash
curl -X POST http://localhost:8000/logs \
  -H "Content-Type: text/plain" \
  -d "2026-11-21T03:00:00Z ERROR payment-service charge failed user_id=42"
```

### Send via Kafka (directly to Redpanda)

```bash
echo "2026-11-21 ERROR worker-service job failed" | docker compose exec -T redpanda rpk topic produce raw-logs
```

### Read raw string or parsed JSON directly into Python variables

```python
import json
import httpx

# 1. Raw string output
raw_logs: str = httpx.get("http://localhost:8000/logs").text

# 2. Ollama-parsed structured data as a Python list[dict]
parsed_logs: list[dict] = httpx.get("http://localhost:8000/logs/parsed").json()
print(parsed_logs)

# 3. Live stream of parsed dicts
with httpx.stream("GET", "http://localhost:8000/logs/parsed/stream") as resp:
    for line in resp.iter_lines():
        if line:
            log_obj: dict = json.loads(line)
            print(log_obj)
```

## Running Unit Tests

```bash
pytest -v
```
