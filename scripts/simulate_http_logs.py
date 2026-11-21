#!/usr/bin/env python3
"""Simulate sending example logs in popular formats to the HTTP log receiver."""

import argparse
import itertools
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone


def build_sample_logs() -> list[tuple[str, str, str]]:
    """Return (format_name, content_type, payload) tuples with fresh timestamps."""
    now = datetime.now(timezone.utc)
    iso_ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    syslog_ts = now.strftime("%b %d %H:%M:%S")
    nginx_ts = now.strftime("%d/%b/%Y:%H:%M:%S +0000")

    json_log = json.dumps(
        {
            "timestamp": iso_ts,
            "level": "ERROR",
            "service": "checkout-api",
            "message": "payment gateway timeout",
            "order_id": "ord_98421",
            "latency_ms": 5021,
        }
    )

    yaml_log = (
        f"timestamp: {iso_ts}\n"
        "level: WARN\n"
        "service: inventory-sync\n"
        "message: low stock threshold reached\n"
        "sku: GPU-4090\n"
        "remaining: 3"
    )

    syslog_rfc5424 = (
        f"<165>1 {iso_ts} web-01.prod nginx 4812 ID47 - "
        "upstream prematurely closed connection while reading response header"
    )

    syslog_rfc3164 = (
        f"<34>{syslog_ts} auth-02 sshd[23109]: "
        "Failed password for invalid user admin from 203.0.113.42 port 51422 ssh2"
    )

    logfmt_log = (
        f'ts={iso_ts} level=info service=billing-worker '
        'msg="invoice generated" tenant=acme amount_cents=4900 duration_ms=84'
    )

    nginx_access_log = (
        f'198.51.100.14 - - [{nginx_ts}] "POST /v1/orders HTTP/1.1" 502 559 '
        '"https://shop.example.com/cart" "Mozilla/5.0"'
    )

    plain_python_log = (
        f"{iso_ts} [CRITICAL] db-migrator: "
        "psycopg2.OperationalError: connection to server at 'db.internal' port 5432 timed out"
    )

    return [
        ("json", "application/json", json_log),
        ("yaml", "application/x-yaml", yaml_log),
        ("syslog-rfc5424", "text/plain", syslog_rfc5424),
        ("syslog-rfc3164", "text/plain", syslog_rfc3164),
        ("logfmt", "text/plain", logfmt_log),
        ("nginx-combined", "text/plain", nginx_access_log),
        ("plain-text", "text/plain", plain_python_log),
    ]


def send_log(url: str, payload: str, content_type: str, timeout: float = 10.0) -> int:
    req = urllib.request.Request(
        url,
        data=payload.encode("utf-8"),
        headers={"Content-Type": content_type},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read()
        return resp.status


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Send simulated logs (JSON, YAML, Syslog, Logfmt, Nginx, plain) via HTTP."
    )
    parser.add_argument(
        "--url",
        default="http://localhost:8088/logs",
        help="Target POST /logs endpoint (default: http://localhost:8088/logs)",
    )
    parser.add_argument(
        "--topic",
        default=None,
        help="Optional ?topic= query parameter override",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.5,
        help="Seconds to sleep between requests (default: 0.5)",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=0,
        help="Total logs to send (0 = send each sample format once; ignored with --loop)",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Continuously send logs until Ctrl+C",
    )
    args = parser.parse_args()

    target_url = args.url
    if args.topic:
        sep = "&" if "?" in target_url else "?"
        target_url = f"{target_url}{sep}topic={urllib.parse.quote(args.topic)}"

    if args.loop:
        stream = itertools.chain.from_iterable(iter(build_sample_logs, None))
    elif args.count > 0:
        stream = itertools.islice(
            itertools.chain.from_iterable(iter(build_sample_logs, None)),
            args.count,
        )
    else:
        stream = iter(build_sample_logs())

    sent = 0
    try:
        for fmt, content_type, payload in stream:
            try:
                status = send_log(target_url, payload, content_type)
                sent += 1
                preview = payload.replace("\n", " | ")[:80]
                print(f"[{sent}] {fmt:15s} -> HTTP {status} | {preview}")
            except urllib.error.URLError as exc:
                print(f"[ERR] {fmt:15s} -> {exc}")
            if args.interval > 0:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\nStopped after sending {sent} logs.")


if __name__ == "__main__":
    main()
