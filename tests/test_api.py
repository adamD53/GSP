import json
from collections import defaultdict
from collections.abc import AsyncIterator

from fastapi.testclient import TestClient

from app.main import create_app


class FakeLogBroker:
    def __init__(self, default_topic: str = "raw-logs") -> None:
        self.default_topic = default_topic
        self.messages: dict[str, list[bytes]] = defaultdict(list)
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def send_raw(self, payload: bytes, topic: str | None = None) -> None:
        target = topic or self.default_topic
        self.messages[target].append(payload)

    async def read_logs(
        self,
        topic: str | None = None,
        limit: int = 100,
        from_beginning: bool = True,
        timeout_ms: int = 2000,
    ) -> str:
        target = topic or self.default_topic
        items = self.messages.get(target, [])
        selected = items[:limit] if from_beginning else items[-limit:]
        return "\n".join(m.decode("utf-8", errors="replace") for m in selected)

    async def stream_logs(
        self,
        topic: str | None = None,
        from_beginning: bool = False,
    ) -> AsyncIterator[str]:
        target = topic or self.default_topic
        for msg in self.messages.get(target, []):
            yield msg.decode("utf-8", errors="replace") + "\n"


def test_health() -> None:
    broker = FakeLogBroker()
    with TestClient(create_app(broker=broker)) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")
        assert resp.text == "ok"
    assert broker.started and broker.stopped


def test_post_and_get_raw_logs() -> None:
    broker = FakeLogBroker()
    with TestClient(create_app(broker=broker)) as client:
        r1 = client.post("/logs", content="INFO auth-service login ok")
        assert r1.status_code == 200
        assert r1.headers["content-type"].startswith("text/plain")
        assert r1.text == "INFO auth-service login ok"

        r2 = client.post("/logs", content='{"level":"error","msg":"timeout"}')
        assert r2.status_code == 200
        assert r2.text == '{"level":"error","msg":"timeout"}'

        get_resp = client.get("/logs")
        assert get_resp.status_code == 200
        assert get_resp.headers["content-type"].startswith("text/plain")
        assert (
            get_resp.text
            == 'INFO auth-service login ok\n{"level":"error","msg":"timeout"}'
        )


def test_custom_topic_routing() -> None:
    broker = FakeLogBroker()
    with TestClient(create_app(broker=broker)) as client:
        client.post("/logs?topic=billing-logs", content="WARN invoice retry")
        assert client.get("/logs").text == ""
        assert client.get("/logs?topic=billing-logs").text == "WARN invoice retry"


def test_stream_raw_logs() -> None:
    broker = FakeLogBroker()
    with TestClient(create_app(broker=broker)) as client:
        client.post("/logs", content="line-1")
        client.post("/logs", content="line-2")
        with client.stream("GET", "/logs/stream?from_beginning=true") as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/plain")
            body = "".join(resp.iter_text())
            assert body == "line-1\nline-2\n"


def test_get_and_stream_parsed_logs() -> None:
    broker = FakeLogBroker()
    sample = {
        "timestamp": "2026-11-21T03:00:00Z",
        "level": "ERROR",
        "service": "payment-service",
        "message": "charge failed",
        "summary": "Payment charge failed for user 42.",
        "metadata": {"user_id": 42},
        "raw": "2026-11-21T03:00:00Z ERROR payment-service charge failed user_id=42",
    }
    broker.messages["parsed-logs"].append(json.dumps(sample).encode("utf-8"))

    with TestClient(create_app(broker=broker)) as client:
        resp = client.get("/logs/parsed")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        data = resp.json()
        assert data == [sample]

        with client.stream("GET", "/logs/parsed/stream?from_beginning=true") as sresp:
            assert sresp.status_code == 200
            assert sresp.headers["content-type"].startswith("application/x-ndjson")
            lines = [line for line in sresp.iter_lines() if line]
            assert [json.loads(line) for line in lines] == [sample]


def test_get_dead_letter_logs() -> None:
    broker = FakeLogBroker()
    dlq_entry = {"raw": "bad line", "error": "Ollama timeout"}
    broker.messages["dead-letter-logs"].append(json.dumps(dlq_entry).encode("utf-8"))

    with TestClient(create_app(broker=broker)) as client:
        resp = client.get("/logs/dead-letter")
        assert resp.status_code == 200
        assert resp.json() == [dlq_entry]
