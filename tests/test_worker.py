import asyncio
import json
from collections import defaultdict

import httpx

from app.ollama_client import OllamaLogParser
from app.schemas import ParsedLog
from app.worker import process_raw_message


class FakeProducer:
    def __init__(self) -> None:
        self.sent: dict[str, list[bytes]] = defaultdict(list)

    async def send_raw(self, payload: bytes, topic: str | None = None) -> None:
        self.sent[topic or "default"].append(payload)


class FailingParser:
    async def parse(self, raw_log: str) -> ParsedLog:
        raise RuntimeError("ollama unavailable")


def test_ollama_parser_structured_output() -> None:
    raw_line = "2026-11-21T03:00:00Z ERROR auth-service invalid token user=bob"
    llm_json = json.dumps(
        {
            "timestamp": "2026-11-21T03:00:00Z",
            "level": "ERROR",
            "service": "auth-service",
            "message": "invalid token",
            "summary": "Authentication failed due to invalid token for bob.",
            "metadata": {"user": "bob"},
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert request.url.path == "/api/chat"
        assert body["model"] == "qwen2.5:1.5b"
        assert body["stream"] is False
        assert body["options"]["temperature"] == 0
        assert "properties" in body["format"]
        return httpx.Response(200, json={"message": {"content": llm_json}})

    async def _run() -> ParsedLog:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            parser = OllamaLogParser(
                base_url="http://ollama:11434",
                model="qwen2.5:1.5b",
                client=client,
            )
            return await parser.parse(raw_line)

    parsed = asyncio.run(_run())
    assert parsed.level == "ERROR"
    assert parsed.service == "auth-service"
    assert parsed.metadata == {"user": "bob"}
    assert parsed.raw == raw_line


def test_worker_routes_success_to_parsed_topic() -> None:
    raw_line = "INFO api started"
    llm_json = json.dumps(
        {
            "timestamp": None,
            "level": "INFO",
            "service": "api",
            "message": "started",
            "summary": "API service started.",
            "metadata": {},
        }
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": llm_json}})

    async def _run() -> tuple[bool, FakeProducer]:
        producer = FakeProducer()
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            parser = OllamaLogParser("http://ollama:11434", "qwen2.5:1.5b", client=client)
            ok = await process_raw_message(
                raw_text=raw_line,
                parser=parser,
                broker=producer,
                parsed_topic="parsed-logs",
                dead_letter_topic="dead-letter-logs",
            )
            return ok, producer

    ok, producer = asyncio.run(_run())
    assert ok is True
    assert len(producer.sent["parsed-logs"]) == 1
    assert len(producer.sent["dead-letter-logs"]) == 0
    saved = json.loads(producer.sent["parsed-logs"][0].decode("utf-8"))
    assert saved["raw"] == raw_line
    assert saved["level"] == "INFO"


def test_worker_routes_failure_to_dlq() -> None:
    async def _run() -> tuple[bool, FakeProducer]:
        producer = FakeProducer()
        ok = await process_raw_message(
            raw_text="corrupt input",
            parser=FailingParser(),
            broker=producer,
            parsed_topic="parsed-logs",
            dead_letter_topic="dead-letter-logs",
        )
        return ok, producer

    ok, producer = asyncio.run(_run())
    assert ok is False
    assert len(producer.sent["parsed-logs"]) == 0
    assert len(producer.sent["dead-letter-logs"]) == 1
    dlq = json.loads(producer.sent["dead-letter-logs"][0].decode("utf-8"))
    assert dlq["raw"] == "corrupt input"
    assert "ollama unavailable" in dlq["error"]
