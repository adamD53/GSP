import json
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from app.config import settings
from app.kafka_client import LogBroker


def _parse_json_lines(raw_text: str) -> list[dict[str, Any]]:
    if not raw_text:
        return []
    items: list[dict[str, Any]] = []
    for line in raw_text.splitlines():
        if not line.strip():
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            items.append({"raw": line})
    return items


def create_app(broker: LogBroker | None = None) -> FastAPI:
    log_broker = broker or LogBroker(
        bootstrap_servers=settings.kafka_bootstrap_servers,
        default_topic=settings.default_topic,
        startup_retries=settings.startup_retries,
        startup_retry_delay=settings.startup_retry_delay,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.broker = log_broker
        await log_broker.start()
        try:
            yield
        finally:
            await log_broker.stop()

    app = FastAPI(title="Redpanda Raw & Ollama Parsed Log Receiver", lifespan=lifespan)

    @app.get("/health", response_class=PlainTextResponse)
    async def health() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.post("/logs", response_class=PlainTextResponse)
    async def ingest_log(
        request: Request,
        topic: str | None = Query(default=None),
    ) -> PlainTextResponse:
        raw_bytes = await request.body()
        await request.app.state.broker.send_raw(raw_bytes, topic=topic)
        return PlainTextResponse(raw_bytes.decode("utf-8", errors="replace"))

    @app.get("/logs", response_class=PlainTextResponse)
    async def get_logs(
        request: Request,
        topic: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=10000),
        from_beginning: bool = Query(default=True),
        timeout_ms: int = Query(default=2000, ge=50, le=10000),
    ) -> PlainTextResponse:
        raw_output = await request.app.state.broker.read_logs(
            topic=topic,
            limit=limit,
            from_beginning=from_beginning,
            timeout_ms=timeout_ms,
        )
        return PlainTextResponse(raw_output)

    @app.get("/logs/stream")
    async def stream_logs(
        request: Request,
        topic: str | None = Query(default=None),
        from_beginning: bool = Query(default=False),
    ) -> StreamingResponse:
        return StreamingResponse(
            request.app.state.broker.stream_logs(
                topic=topic,
                from_beginning=from_beginning,
            ),
            media_type="text/plain; charset=utf-8",
        )

    @app.get("/logs/parsed", response_class=JSONResponse)
    async def get_parsed_logs(
        request: Request,
        topic: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=10000),
        from_beginning: bool = Query(default=True),
        timeout_ms: int = Query(default=2000, ge=50, le=10000),
    ) -> JSONResponse:
        raw_json_lines = await request.app.state.broker.read_logs(
            topic=topic or settings.parsed_topic,
            limit=limit,
            from_beginning=from_beginning,
            timeout_ms=timeout_ms,
        )
        return JSONResponse(_parse_json_lines(raw_json_lines))

    @app.get("/logs/parsed/stream")
    async def stream_parsed_logs(
        request: Request,
        topic: str | None = Query(default=None),
        from_beginning: bool = Query(default=False),
    ) -> StreamingResponse:
        return StreamingResponse(
            request.app.state.broker.stream_logs(
                topic=topic or settings.parsed_topic,
                from_beginning=from_beginning,
            ),
            media_type="application/x-ndjson",
        )

    @app.get("/logs/dead-letter", response_class=JSONResponse)
    async def get_dead_letter_logs(
        request: Request,
        limit: int = Query(default=100, ge=1, le=10000),
        from_beginning: bool = Query(default=True),
        timeout_ms: int = Query(default=2000, ge=50, le=10000),
    ) -> JSONResponse:
        raw_json_lines = await request.app.state.broker.read_logs(
            topic=settings.dead_letter_topic,
            limit=limit,
            from_beginning=from_beginning,
            timeout_ms=timeout_ms,
        )
        return JSONResponse(_parse_json_lines(raw_json_lines))

    return app


app = create_app()
