import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from scripts.loadtest import (
    build_event_payload,
    extract_bench_id,
    format_report,
    parse_args,
    percentile,
    run_benchmark,
    summarize_latencies,
)


def test_percentile_and_summary() -> None:
    assert percentile([], 50) == 0.0
    assert percentile([42.0], 99) == 42.0
    values = [float(i) for i in range(1, 101)]
    assert round(percentile(values, 50), 2) == 50.5
    assert round(percentile(values, 99), 2) == 99.01

    summary = summarize_latencies(values)
    assert summary["count"] == 100
    assert summary["min"] == 1.0
    assert summary["max"] == 100.0
    assert summary["p50"] == 50.5
    assert summary["p99"] == 99.01


def test_build_event_payload_and_extract_bench_id() -> None:
    seen_shapes: set[str] = set()
    for seq in range(5):
        bid, raw = build_event_payload(seq, "run-abc", mix="mixed")
        assert bid == f"run-abc-{seq}"
        doc = json.loads(raw)
        assert doc["bench_id"] == bid
        assert doc["bench_run"] == "run-abc"

        # Extraction via event, metadata, and raw fallback
        assert extract_bench_id({"event": doc}) == bid
        assert extract_bench_id({"metadata": {"bench_id": bid}}) == bid
        assert extract_bench_id({"raw": raw}) == bid

        if "type" in doc:
            seen_shapes.add("workflow")
        elif "alertname" in doc:
            seen_shapes.add("alertmanager")
        elif "meta" in doc:
            seen_shapes.add("business")
        elif doc.get("level") == "ERROR":
            seen_shapes.add("checkout_error")
        else:
            seen_shapes.add("info")

    assert seen_shapes == {"workflow", "alertmanager", "business", "checkout_error", "info"}


def test_run_benchmark_end_to_end_against_local_http_server() -> None:
    """Verify full POST /logs -> /logs/parsed/stream measurement loop."""
    published: list[str] = []
    cond = threading.Condition()
    stop_stream = threading.Event()

    class MockHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            if self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")
                return

            if self.path.startswith("/logs/parsed/stream"):
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                self.wfile.write(b"\n")
                self.wfile.flush()

                idx = 0
                while not stop_stream.is_set():
                    with cond:
                        if idx >= len(published):
                            cond.wait(timeout=0.1)
                            continue
                        batch = published[idx:]
                        idx = len(published)
                    for line in batch:
                        try:
                            self.wfile.write((line + "\n").encode("utf-8"))
                            self.wfile.flush()
                        except OSError:
                            return
                return

            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            doc = json.loads(raw)
            parsed_line = json.dumps(
                {
                    "level": doc.get("level", "INFO"),
                    "service": doc.get("service", "mock"),
                    "message": "ok",
                    "summary": "ok",
                    "metadata": {"bench_id": doc.get("bench_id")},
                    "raw": raw,
                    "event": doc,
                    "parse_mode": "fast_json",
                    "alert": doc.get("level") == "ERROR",
                    "notifications": (
                        [{"channel": "email", "status": "routed"}]
                        if doc.get("level") == "ERROR"
                        else []
                    ),
                    "latency_ms": 4.2,
                }
            )
            with cond:
                published.append(parsed_line)
                cond.notify_all()

            body = raw.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    port = server.server_address[1]
    srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
    srv_thread.start()

    try:
        args = parse_args(
            [
                "--url",
                f"http://127.0.0.1:{port}",
                "--count",
                "25",
                "--concurrency",
                "5",
                "--warmup",
                "2",
                "--drain-timeout",
                "5",
            ]
        )
        report = run_benchmark(args)
        assert report["throughput"]["ingested_ok"] == 25
        assert report["throughput"]["ingest_errors"] == 0
        assert report["throughput"]["delivered_count"] == 25
        assert report["throughput"]["dropped_or_unseen"] == 0
        assert report["throughput"]["ingest_msgs_per_sec"] > 0
        assert report["throughput"]["processed_msgs_per_sec"] > 0
        assert report["latency_ms"]["http_ingest"]["count"] == 25
        assert report["latency_ms"]["e2e_ingest_to_delivery"]["count"] == 25
        assert report["latency_ms"]["server_pipeline"]["p50"] == 4.2
        assert report["pipeline"]["parse_modes"] == {"fast_json": 25}
        assert report["pipeline"]["alerts_triggered"] == 5

        rendered = format_report(report)
        assert "GSP Load Test Report" in rendered
        assert "E2E Ingest -> Delivery" in rendered
    finally:
        stop_stream.set()
        with cond:
            cond.notify_all()
        server.shutdown()
        server.server_close()


def test_worker_stamps_latency_and_delivery_timestamps(tmp_path: Path) -> None:
    """Verify worker stamps ingested_at, processed_at, latency_ms, and delivered_at."""
    from collections import defaultdict

    from app.schemas import ParsedLog
    from app.worker import process_raw_message

    rules_path = tmp_path / "rules.json"
    rules_path.write_text(
        json.dumps(
            [
                {
                    "id": "err-rule",
                    "name": "Error Rule",
                    "severity": "high",
                    "notify": [{"channel": "email", "to": "ops@example.com"}],
                    "field": "/level",
                    "operator": "eq",
                    "value": "ERROR",
                }
            ]
        ),
        encoding="utf-8",
    )

    class StaticParser:
        async def parse(self, raw_log: str) -> ParsedLog:
            return ParsedLog(
                timestamp=None,
                level="ERROR",
                service="checkout-api",
                message="timeout",
                summary="timeout",
                metadata={},
                raw=raw_log,
                parse_mode="fast_json",
            )

    class FakeBroker:
        def __init__(self) -> None:
            self.sent: dict[str, list[bytes]] = defaultdict(list)

        async def send_raw(self, payload: bytes, topic: str | None = None) -> None:
            self.sent[topic or "default"].append(payload)

    ingested_ms = int(time.time() * 1000) - 25
    broker = FakeBroker()
    ok = asyncio.run(
        process_raw_message(
            raw_text='{"level":"ERROR"}',
            parser=StaticParser(),
            broker=broker,
            parsed_topic="parsed-logs",
            dead_letter_topic="dead-letter-logs",
            rules_file=str(rules_path),
            ingested_at_ms=ingested_ms,
        )
    )
    assert ok is True
    saved = json.loads(broker.sent["parsed-logs"][0].decode("utf-8"))
    assert saved["ingested_at"] is not None
    assert saved["processed_at"] is not None
    assert isinstance(saved["latency_ms"], float)
    assert saved["latency_ms"] >= 0.0
    assert saved["notifications"][0]["delivered_at"] is not None
