#!/usr/bin/env python3
"""End-to-end throughput & latency benchmark for the GSP alerting pipeline.

Measures (never estimates):
  - Ingest throughput (msgs/s) and HTTP ingest latency (p50 / p95 / p99)
  - Processed / end-to-end throughput (msgs/s)
  - End-to-end ingest -> delivery latency on /logs/parsed/stream (p50 / p95 / p99)
  - Server-side pipeline latency (ingested_at -> processed_at) when stamped by worker

Uses only the Python 3 standard library so it runs directly on any host or container.

Example:
  python3 scripts/loadtest.py --url http://localhost:8000 --count 500 --concurrency 20
"""

from __future__ import annotations

import argparse
import concurrent.futures
import http.client
import json
import math
import queue
import re
import sys
import threading
import time
import urllib.parse
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any

_BENCH_ID_RE = re.compile(r'"bench_id"\s*:\s*"([^"]+)"')


def percentile(values: list[float], p: float) -> float:
    """Compute percentile p (0..100) using linear interpolation."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (p / 100.0) * (len(ordered) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return ordered[lo]
    frac = rank - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def summarize_latencies(values: list[float]) -> dict[str, float]:
    if not values:
        return {"count": 0, "min": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0, "mean": 0.0}
    return {
        "count": len(values),
        "min": round(min(values), 2),
        "p50": round(percentile(values, 50), 2),
        "p95": round(percentile(values, 95), 2),
        "p99": round(percentile(values, 99), 2),
        "max": round(max(values), 2),
        "mean": round(sum(values) / len(values), 2),
    }


def build_event_payload(seq: int, run_id: str, mix: str = "mixed") -> tuple[str, str]:
    """Generate a realistic JSON event tagged with a unique bench_id."""
    bench_id = f"{run_id}-{seq}"
    now = datetime.now(timezone.utc)
    iso_ts = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    epoch_ms = int(now.timestamp() * 1000)

    if mix == "alerts":
        kind = seq % 2
    elif mix == "appendix":
        kind = seq % 3
    elif mix == "info":
        kind = 4
    else:
        kind = seq % 5

    if kind == 0:
        # High-latency error (triggers default high-latency-error rule)
        doc: dict[str, Any] = {
            "timestamp": iso_ts,
            "level": "ERROR",
            "service": "checkout-api",
            "message": "payment gateway timeout",
            "order_id": f"ord_{seq}",
            "latency_ms": 4200 + (seq % 1500),
            "bench_id": bench_id,
            "bench_run": run_id,
        }
    elif kind == 1:
        # Appendix A: Workflow failure
        doc = {
            "type": "WORKFLOW_FAILED",
            "workflowId": f"WF-2026-{seq:05d}",
            "workflowName": "order-settlement",
            "step": "validate-counterparty",
            "severity": "ERROR",
            "environment": "prod",
            "timestamp": iso_ts,
            "error": {
                "code": "TIMEOUT",
                "message": "Counterparty service did not respond in 5000 ms",
            },
            "bench_id": bench_id,
            "bench_run": run_id,
        }
    elif kind == 2:
        # Appendix A: Metric threshold breach (Alertmanager-style)
        doc = {
            "alertname": "HighErrorRate",
            "labels": {
                "service": "payments-api",
                "region": "eu-west",
                "severity": "critical",
            },
            "annotations": {
                "summary": f"Error rate {10 + (seq % 15)}% over the last 5 minutes",
            },
            "startsAt": iso_ts,
            "status": "firing",
            "bench_id": bench_id,
            "bench_run": run_id,
        }
    elif kind == 3:
        # Appendix A: Business event with nested envelope
        doc = {
            "meta": {
                "schema": "trade.rejected.v2",
                "producer": "trade-gateway",
                "ts": epoch_ms,
            },
            "payload": {
                "tradeId": f"T-{900000 + seq}",
                "desk": "rates",
                "notional": 25000000,
                "currency": "EUR",
                "reason": "LIMIT_BREACH",
            },
            "bench_id": bench_id,
            "bench_run": run_id,
        }
    else:
        # Routine INFO event (non-alerting baseline)
        doc = {
            "timestamp": iso_ts,
            "level": "INFO",
            "service": "pricing-engine",
            "message": "quote cache refreshed",
            "instrument": f"EURUSD-{seq % 8}",
            "latency_ms": 12 + (seq % 40),
            "bench_id": bench_id,
            "bench_run": run_id,
        }

    return bench_id, json.dumps(doc, separators=(",", ":"))


def extract_bench_id(parsed_obj: dict[str, Any]) -> str | None:
    """Extract bench_id from a parsed log object regardless of parser path."""
    for container_key in ("event", "metadata"):
        container = parsed_obj.get(container_key)
        if isinstance(container, dict):
            bid = container.get("bench_id")
            if isinstance(bid, str) and bid:
                return bid
    raw = parsed_obj.get("raw")
    if isinstance(raw, str):
        match = _BENCH_ID_RE.search(raw)
        if match:
            return match.group(1)
    return None


class ParsedStreamSubscriber:
    """Reads /logs/parsed/stream in a background thread and records delivery times."""

    def __init__(
        self,
        base_url: str,
        run_id: str,
        expected_ids: set[str],
        parsed_topic: str | None = None,
    ) -> None:
        self.base_url = base_url
        self.run_id = run_id
        self.expected_ids = expected_ids
        self.parsed_topic = parsed_topic

        self.ready_event = threading.Event()
        self.done_event = threading.Event()
        self.stop_event = threading.Event()
        self.error: str | None = None

        self._lock = threading.Lock()
        self.delivered_ns: dict[str, int] = {}
        self.server_latencies_ms: list[float] = []
        self.parse_modes: Counter[str] = Counter()
        self.alerts_triggered: int = 0
        self.notifications_delivered: int = 0

        self._conn: http.client.HTTPConnection | None = None
        self._thread = threading.Thread(target=self._run, name="parsed-stream", daemon=True)

    def start(self, ready_timeout: float = 10.0) -> None:
        self._thread.start()
        if not self.ready_event.wait(timeout=ready_timeout):
            raise TimeoutError("Timed out waiting for /logs/parsed/stream to connect")
        if self.error:
            raise RuntimeError(f"Parsed stream error: {self.error}")

    def stop(self) -> None:
        self.stop_event.set()
        conn = self._conn
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        self._thread.join(timeout=2.0)

    def delivered_count(self) -> int:
        with self._lock:
            return len(self.delivered_ns)

    def _run(self) -> None:
        parsed = urllib.parse.urlparse(self.base_url)
        host = parsed.hostname or "localhost"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        qs_params = {"from_beginning": "false"}
        if self.parsed_topic:
            qs_params["topic"] = self.parsed_topic
        path = f"{parsed.path.rstrip('/')}/logs/parsed/stream?{urllib.parse.urlencode(qs_params)}"

        try:
            conn_cls = (
                http.client.HTTPSConnection
                if parsed.scheme == "https"
                else http.client.HTTPConnection
            )
            conn = conn_cls(host, port, timeout=15.0)
            self._conn = conn
            conn.request("GET", path, headers={"Accept": "application/x-ndjson"})
            resp = conn.getresponse()
            if resp.status != 200:
                self.error = f"HTTP {resp.status} from {path}"
                self.ready_event.set()
                return

            # Wait for the initial heartbeat newline flushed by stream_logs()
            # so Kafka seek_to_end() is guaranteed complete before we send traffic.
            resp.readline()
            self.ready_event.set()

            while not self.stop_event.is_set():
                line = resp.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                recv_ns = time.perf_counter_ns()
                try:
                    obj = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue

                bid = extract_bench_id(obj)
                if not bid or not bid.startswith(f"{self.run_id}-"):
                    continue

                with self._lock:
                    if bid in self.expected_ids and bid not in self.delivered_ns:
                        self.delivered_ns[bid] = recv_ns
                        srv_lat = obj.get("latency_ms")
                        if isinstance(srv_lat, (int, float)):
                            self.server_latencies_ms.append(float(srv_lat))
                        mode = obj.get("parse_mode")
                        if isinstance(mode, str):
                            self.parse_modes[mode] += 1
                        if obj.get("alert"):
                            self.alerts_triggered += 1
                        notifs = obj.get("notifications")
                        if isinstance(notifs, list):
                            self.notifications_delivered += sum(
                                1
                                for n in notifs
                                if isinstance(n, dict) and n.get("status") in ("sent", "routed")
                            )
                        if len(self.delivered_ns) >= len(self.expected_ids):
                            self.done_event.set()
                            break
        except Exception as exc:
            if not self.stop_event.is_set():
                self.error = str(exc)
                self.ready_event.set()


class _KeepAlivePoster:
    """Thread-local HTTP/1.1 keep-alive client for high-throughput POST /logs."""

    def __init__(self, base_url: str, topic: str | None = None, timeout: float = 10.0) -> None:
        parsed = urllib.parse.urlparse(base_url)
        self.scheme = parsed.scheme or "http"
        self.host = parsed.hostname or "localhost"
        self.port = parsed.port or (443 if self.scheme == "https" else 80)
        qs = f"?{urllib.parse.urlencode({'topic': topic})}" if topic else ""
        self.path = f"{parsed.path.rstrip('/')}/logs{qs}"
        self.timeout = timeout
        self._local = threading.local()

    def _get_conn(self) -> http.client.HTTPConnection:
        conn: http.client.HTTPConnection | None = getattr(self._local, "conn", None)
        if conn is None:
            cls = (
                http.client.HTTPSConnection
                if self.scheme == "https"
                else http.client.HTTPConnection
            )
            conn = cls(self.host, self.port, timeout=self.timeout)
            self._local.conn = conn
        return conn

    def _reset_conn(self) -> None:
        conn: http.client.HTTPConnection | None = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None

    def post(self, body: bytes) -> int:
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "Connection": "keep-alive",
        }
        for attempt in range(2):
            conn = self._get_conn()
            try:
                conn.request("POST", self.path, body=body, headers=headers)
                resp = conn.getresponse()
                resp.read()
                return resp.status
            except (http.client.HTTPException, OSError):
                self._reset_conn()
                if attempt == 1:
                    raise
        return 0


def check_health(base_url: str) -> None:
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    conn = cls(host, port, timeout=5.0)
    try:
        conn.request("GET", f"{parsed.path.rstrip('/')}/health")
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace").strip()
        if resp.status != 200 or body != "ok":
            raise RuntimeError(f"Unexpected /health response: HTTP {resp.status} {body!r}")
    finally:
        conn.close()


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    check_health(args.url)
    poster = _KeepAlivePoster(args.url, topic=args.topic)

    if args.warmup > 0:
        warmup_run = f"warmup-{uuid.uuid4().hex[:6]}"
        for i in range(args.warmup):
            _, payload = build_event_payload(i, warmup_run, mix=args.mix)
            poster.post(payload.encode("utf-8"))

    run_id = f"bench-{uuid.uuid4().hex[:8]}"
    events = [build_event_payload(i, run_id, mix=args.mix) for i in range(args.count)]
    expected_ids = {bid for bid, _ in events}

    subscriber: ParsedStreamSubscriber | None = None
    if not args.skip_stream:
        subscriber = ParsedStreamSubscriber(
            base_url=args.url,
            run_id=run_id,
            expected_ids=expected_ids,
            parsed_topic=args.parsed_topic,
        )
        subscriber.start()

    send_start_ns: dict[str, int] = {}
    ingest_latencies_ms: list[float] = []
    ingest_errors = 0
    lock = threading.Lock()
    work_q: queue.Queue[tuple[str, bytes]] = queue.Queue()
    for bid, payload in events:
        work_q.put((bid, payload.encode("utf-8")))

    def _worker() -> None:
        nonlocal ingest_errors
        local_latencies: list[float] = []
        local_errors = 0
        while True:
            try:
                bid, body = work_q.get_nowait()
            except queue.Empty:
                break
            t0 = time.perf_counter_ns()
            with lock:
                send_start_ns[bid] = t0
            try:
                status = poster.post(body)
                t1 = time.perf_counter_ns()
                if 200 <= status < 300:
                    local_latencies.append((t1 - t0) / 1_000_000.0)
                else:
                    local_errors += 1
            except Exception:
                local_errors += 1
            finally:
                work_q.task_done()
        with lock:
            ingest_latencies_ms.extend(local_latencies)
            ingest_errors += local_errors

    t_bench_start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futs = [pool.submit(_worker) for _ in range(args.concurrency)]
        for fut in futs:
            fut.result()
    t_ingest_end = time.perf_counter()

    ingest_elapsed_s = max(1e-6, t_ingest_end - t_bench_start)
    ingested_ok = args.count - ingest_errors
    ingest_rps = ingested_ok / ingest_elapsed_s

    e2e_latencies_ms: list[float] = []
    server_latencies_ms: list[float] = []
    delivered_count = 0
    e2e_elapsed_s = ingest_elapsed_s
    e2e_rps = 0.0
    parse_modes: dict[str, int] = {}
    alerts_triggered = 0
    notifications_delivered = 0

    if subscriber is not None:
        subscriber.done_event.wait(timeout=args.drain_timeout)
        t_drain_end = time.perf_counter()
        subscriber.stop()

        with subscriber._lock:
            delivered_map = dict(subscriber.delivered_ns)
            server_latencies_ms = list(subscriber.server_latencies_ms)
            parse_modes = dict(subscriber.parse_modes)
            alerts_triggered = subscriber.alerts_triggered
            notifications_delivered = subscriber.notifications_delivered

        delivered_count = len(delivered_map)
        if delivered_map:
            last_delivery_ns = max(delivered_map.values())
            first_send_ns = min(send_start_ns.values()) if send_start_ns else last_delivery_ns
            e2e_elapsed_s = max(1e-6, (last_delivery_ns - first_send_ns) / 1_000_000_000.0)
            e2e_rps = delivered_count / e2e_elapsed_s
            for bid, recv_ns in delivered_map.items():
                t0 = send_start_ns.get(bid)
                if t0 is not None:
                    e2e_latencies_ms.append(max(0.0, (recv_ns - t0) / 1_000_000.0))
        else:
            e2e_elapsed_s = max(1e-6, t_drain_end - t_bench_start)

    return {
        "run_id": run_id,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "config": {
            "url": args.url,
            "count": args.count,
            "concurrency": args.concurrency,
            "warmup": args.warmup,
            "mix": args.mix,
            "skip_stream": args.skip_stream,
        },
        "throughput": {
            "ingested_ok": ingested_ok,
            "ingest_errors": ingest_errors,
            "ingest_elapsed_s": round(ingest_elapsed_s, 3),
            "ingest_msgs_per_sec": round(ingest_rps, 2),
            "delivered_count": delivered_count,
            "dropped_or_unseen": max(0, ingested_ok - delivered_count) if not args.skip_stream else 0,
            "e2e_elapsed_s": round(e2e_elapsed_s, 3),
            "processed_msgs_per_sec": round(e2e_rps, 2),
        },
        "latency_ms": {
            "http_ingest": summarize_latencies(ingest_latencies_ms),
            "e2e_ingest_to_delivery": summarize_latencies(e2e_latencies_ms),
            "server_pipeline": summarize_latencies(server_latencies_ms),
        },
        "pipeline": {
            "parse_modes": parse_modes,
            "alerts_triggered": alerts_triggered,
            "notifications_delivered": notifications_delivered,
        },
    }


def format_report(report: dict[str, Any]) -> str:
    cfg = report["config"]
    tp = report["throughput"]
    lat = report["latency_ms"]
    pipe = report["pipeline"]

    lines = [
        f"=== GSP Load Test Report ({report['run_id']}) ===",
        f"Target: {cfg['url']} | Messages: {cfg['count']} | Concurrency: {cfg['concurrency']} | Mix: {cfg['mix']}",
        "",
        "Throughput:",
        f"  Ingest    : {tp['ingest_msgs_per_sec']:>8.2f} msgs/s  ({tp['ingested_ok']}/{cfg['count']} ok, {tp['ingest_errors']} err, {tp['ingest_elapsed_s']}s)",
    ]
    if not cfg["skip_stream"]:
        lines.append(
            f"  Processed : {tp['processed_msgs_per_sec']:>8.2f} msgs/s  ({tp['delivered_count']}/{tp['ingested_ok']} delivered, {tp['dropped_or_unseen']} unseen, {tp['e2e_elapsed_s']}s)"
        )

    lines.extend([
        "",
        "Latency (ms):                       min      p50      p95      p99      max",
    ])
    for label, key in (
        ("HTTP Ingest (POST /logs)   ", "http_ingest"),
        ("E2E Ingest -> Delivery     ", "e2e_ingest_to_delivery"),
        ("Server (ingested->processed)", "server_pipeline"),
    ):
        s = lat[key]
        if s["count"] > 0:
            lines.append(
                f"  {label}: {s['min']:7.2f}  {s['p50']:7.2f}  {s['p95']:7.2f}  {s['p99']:7.2f}  {s['max']:7.2f}"
            )

    if not cfg["skip_stream"]:
        modes_str = ", ".join(f"{k}={v}" for k, v in sorted(pipe["parse_modes"].items())) or "none"
        lines.extend([
            "",
            "Pipeline Breakdown:",
            f"  Parse modes             : {modes_str}",
            f"  Alerts triggered        : {pipe['alerts_triggered']}",
            f"  Notifications delivered : {pipe['notifications_delivered']}",
        ])

    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure GSP ingest throughput and end-to-end ingest->delivery p50/p99 latency."
    )
    parser.add_argument("--url", default="http://localhost:8000", help="API base URL (default: http://localhost:8000)")
    parser.add_argument("-n", "--count", type=int, default=500, help="Number of messages to send (default: 500)")
    parser.add_argument("-c", "--concurrency", type=int, default=20, help="Concurrent HTTP sender threads (default: 20)")
    parser.add_argument("--warmup", type=int, default=10, help="Warmup messages before timing (default: 10)")
    parser.add_argument(
        "--mix",
        choices=("mixed", "alerts", "appendix", "info"),
        default="mixed",
        help="Event payload mix (default: mixed)",
    )
    parser.add_argument("--topic", default=None, help="Optional raw topic override")
    parser.add_argument("--parsed-topic", default=None, help="Optional parsed topic override")
    parser.add_argument(
        "--drain-timeout",
        type=float,
        default=30.0,
        help="Seconds to wait for all messages on /logs/parsed/stream (default: 30)",
    )
    parser.add_argument(
        "--skip-stream",
        action="store_true",
        help="Measure HTTP ingest only without subscribing to /logs/parsed/stream",
    )
    parser.add_argument("--json-out", default=None, help="Optional path to write full JSON report")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = run_benchmark(args)
    except Exception as exc:
        print(f"[loadtest] ERROR: {exc}", file=sys.stderr)
        return 1

    print(format_report(report))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
            fh.write("\n")
        print(f"\nSaved JSON report to {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
