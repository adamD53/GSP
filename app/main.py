import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from app.config import settings
from app.kafka_client import LogBroker
from app.notifications import get_default_working_hours, is_within_working_hours
from app.rules import (
    evaluate_and_route,
    load_rules,
    save_rules,
    validate_rules,
)


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


def create_app(
    broker: LogBroker | None = None,
    rules_file: str | None = None,
) -> FastAPI:
    active_rules_file = rules_file or settings.rules_file
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

    @app.get("/rules", response_class=JSONResponse)
    async def get_rules() -> JSONResponse:
        try:
            rules = load_rules(active_rules_file)
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return JSONResponse(rules)

    @app.put("/rules", response_class=JSONResponse)
    async def update_rules(payload: Any = Body(...)) -> JSONResponse:
        try:
            saved = save_rules(payload, active_rules_file)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(saved)

    @app.get("/rules/working-hours", response_class=JSONResponse)
    async def get_working_hours() -> JSONResponse:
        wh = get_default_working_hours()
        now = datetime.now(timezone.utc)
        return JSONResponse(
            {
                "start": wh.start.strftime("%H:%M"),
                "end": wh.end.strftime("%H:%M"),
                "days": list(wh.days),
                "timezone": wh.tz_name,
                "description": wh.describe(),
                "within_working_hours": is_within_working_hours(at=now, config=wh),
                "now": now.isoformat(),
            }
        )

    @app.post("/rules/evaluate", response_class=JSONResponse)
    async def evaluate_rules_endpoint(payload: dict[str, Any] = Body(...)) -> JSONResponse:
        message = payload.get("message")
        if not isinstance(message, dict):
            raise HTTPException(
                status_code=400,
                detail="Request body must include a 'message' JSON object.",
            )
        override_rules = payload.get("rules")
        raw_at = payload.get("at")
        eval_time = datetime.now(timezone.utc)
        if isinstance(raw_at, str) and raw_at.strip():
            try:
                eval_time = datetime.fromisoformat(raw_at.strip().replace("Z", "+00:00"))
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid ISO timestamp for 'at': {raw_at}",
                ) from exc

        try:
            rules = (
                validate_rules(override_rules)
                if override_rules is not None
                else load_rules(active_rules_file)
            )
            matches, decisions = evaluate_and_route(message, rules, at=eval_time)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        wh = get_default_working_hours()
        return JSONResponse(
            {
                "matched": len(matches) > 0,
                "matched_rules": [m.model_dump() for m in matches],
                "notifications": [d.model_dump() for d in decisions],
                "evaluated_at": eval_time.isoformat(),
                "within_working_hours": is_within_working_hours(at=eval_time, config=wh),
                "working_hours": wh.describe(),
            }
        )

    return app


app = create_app()
