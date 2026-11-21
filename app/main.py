import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from app.alerts import AlertStore
from app.config import settings
from app.kafka_client import LogBroker
from app.notifications import (
    get_default_working_hours,
    is_within_working_hours,
    process_outbox_once,
)
from app.parser import prepare_rule_eval_target
from app.rules import (
    evaluate_and_route,
    load_rules,
    save_rules,
    validate_rules,
)
from app.runbooks import (
    execute_runbook_step,
    load_runbooks,
    resolve_runbook_for_alert,
    save_runbooks,
)
from app.schemas import AlertRecord, AlertStatus, OutboxStatus, RunbookDefinition


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
    alert_store: AlertStore | None = None,
    runbooks_file: str | None = None,
) -> FastAPI:
    active_rules_file = rules_file or settings.rules_file
    active_runbooks_file = runbooks_file or settings.runbooks_file
    active_alert_store = alert_store or AlertStore(settings.alerts_db_path)

    def _enrich_alert(alert: AlertRecord) -> dict[str, Any]:
        runbooks = load_runbooks(active_runbooks_file)
        resolved = resolve_runbook_for_alert(alert, runbooks)
        return alert.model_copy(update={"runbook": resolved}).model_dump()
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
            eval_target = prepare_rule_eval_target(message)
            matches, decisions = evaluate_and_route(eval_target, rules, at=eval_time)
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

    @app.get("/alerts", response_class=JSONResponse)
    async def list_alerts_endpoint(
        status: AlertStatus | None = Query(default=None),
        service: str | None = Query(default=None),
        rule_id: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> JSONResponse:
        alerts = active_alert_store.list_alerts(
            status=status,
            service=service,
            rule_id=rule_id,
            limit=limit,
        )
        return JSONResponse([_enrich_alert(a) for a in alerts])

    @app.get("/alerts/{alert_id}", response_class=JSONResponse)
    async def get_alert_endpoint(alert_id: str) -> JSONResponse:
        alert = active_alert_store.get_alert(alert_id)
        if alert is None:
            raise HTTPException(status_code=404, detail="Alert not found")
        return JSONResponse(_enrich_alert(alert))

    @app.post("/alerts/{alert_id}/ack", response_class=JSONResponse)
    async def ack_alert_endpoint(
        alert_id: str,
        payload: dict[str, Any] = Body(default={}),
    ) -> JSONResponse:
        updated = active_alert_store.transition(
            alert_id,
            action="ack",
            actor=str(payload.get("actor") or "oncall"),
            note=payload.get("note"),
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="Alert not found")
        return JSONResponse(_enrich_alert(updated))

    @app.post("/alerts/{alert_id}/resolve", response_class=JSONResponse)
    async def resolve_alert_endpoint(
        alert_id: str,
        payload: dict[str, Any] = Body(default={}),
    ) -> JSONResponse:
        updated = active_alert_store.transition(
            alert_id,
            action="resolve",
            actor=str(payload.get("actor") or "oncall"),
            note=payload.get("note"),
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="Alert not found")
        return JSONResponse(_enrich_alert(updated))

    @app.post("/alerts/{alert_id}/reopen", response_class=JSONResponse)
    async def reopen_alert_endpoint(
        alert_id: str,
        payload: dict[str, Any] = Body(default={}),
    ) -> JSONResponse:
        updated = active_alert_store.transition(
            alert_id,
            action="reopen",
            actor=str(payload.get("actor") or "oncall"),
            note=payload.get("note"),
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="Alert not found")
        return JSONResponse(_enrich_alert(updated))

    @app.post("/alerts/{alert_id}/steps/{step_index}/execute", response_class=JSONResponse)
    async def execute_alert_step_endpoint(
        alert_id: str,
        step_index: int,
        payload: dict[str, Any] = Body(default={}),
    ) -> JSONResponse:
        alert = active_alert_store.get_alert(alert_id)
        if alert is None:
            raise HTTPException(status_code=404, detail="Alert not found")

        runbooks = load_runbooks(active_runbooks_file)
        resolved = resolve_runbook_for_alert(alert, runbooks)
        if resolved is None:
            raise HTTPException(
                status_code=404, detail="No runbook attached to this alert"
            )

        try:
            execution = execute_runbook_step(
                runbook=resolved,
                step_index=step_index,
                alert=alert,
                actor=str(payload.get("actor") or "oncall"),
                approved=bool(payload.get("approved", False)),
                note=payload.get("note"),
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        updated = active_alert_store.record_step_execution(alert_id, execution)
        if updated is None:
            raise HTTPException(status_code=404, detail="Alert not found")
        return JSONResponse(_enrich_alert(updated))

    @app.get("/runbooks", response_class=JSONResponse)
    async def list_runbooks_endpoint() -> JSONResponse:
        runbooks = load_runbooks(active_runbooks_file)
        return JSONResponse([rb.model_dump() for rb in runbooks])

    @app.post("/runbooks", response_class=JSONResponse)
    async def create_runbook_endpoint(payload: dict[str, Any] = Body(...)) -> JSONResponse:
        try:
            new_rb = RunbookDefinition.model_validate(payload)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        existing = load_runbooks(active_runbooks_file)
        if any(rb.id == new_rb.id for rb in existing):
            raise HTTPException(
                status_code=409, detail=f"Runbook '{new_rb.id}' already exists"
            )
        saved = save_runbooks([*existing, new_rb], active_runbooks_file)
        return JSONResponse([rb.model_dump() for rb in saved], status_code=201)

    @app.put("/runbooks/{runbook_id}", response_class=JSONResponse)
    async def update_runbook_endpoint(
        runbook_id: str,
        payload: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        try:
            updated_rb = RunbookDefinition.model_validate({**payload, "id": runbook_id})
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        existing = load_runbooks(active_runbooks_file)
        found = False
        next_list: list[RunbookDefinition] = []
        for rb in existing:
            if rb.id == runbook_id:
                next_list.append(updated_rb)
                found = True
            else:
                next_list.append(rb)
        if not found:
            next_list.append(updated_rb)
        saved = save_runbooks(next_list, active_runbooks_file)
        return JSONResponse([rb.model_dump() for rb in saved])

    @app.delete("/runbooks/{runbook_id}", response_class=JSONResponse)
    async def delete_runbook_endpoint(runbook_id: str) -> JSONResponse:
        existing = load_runbooks(active_runbooks_file)
        next_list = [rb for rb in existing if rb.id != runbook_id]
        if len(next_list) == len(existing):
            raise HTTPException(status_code=404, detail="Runbook not found")
        saved = save_runbooks(next_list, active_runbooks_file)
        return JSONResponse([rb.model_dump() for rb in saved])

    @app.get("/notifications/outbox", response_class=JSONResponse)
    async def list_outbox_endpoint(
        status: OutboxStatus | None = Query(default=None),
        alert_id: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> JSONResponse:
        items = active_alert_store.list_outbox(
            status=status,
            alert_id=alert_id,
            limit=limit,
        )
        return JSONResponse([item.model_dump() for item in items])

    @app.post("/notifications/outbox/process", response_class=JSONResponse)
    async def process_outbox_endpoint(request: Request) -> JSONResponse:
        stats = await process_outbox_once(
            alert_store=active_alert_store,
            broker=request.app.state.broker,
            dead_letter_topic=settings.notification_dead_letter_topic,
        )
        return JSONResponse(stats)

    @app.post("/notifications/outbox/{outbox_id}/retry", response_class=JSONResponse)
    async def requeue_outbox_endpoint(outbox_id: str) -> JSONResponse:
        requeued = active_alert_store.requeue_outbox(outbox_id)
        if requeued is None:
            raise HTTPException(status_code=404, detail="Outbox record not found")
        return JSONResponse(requeued.model_dump())

    return app


app = create_app()
