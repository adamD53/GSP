import { SERVICE_FORMATS, type ServiceFormat } from "./types";

export function getContentTypeForFormat(format: ServiceFormat): string {
  return (
    SERVICE_FORMATS.find((item) => item.value === format)?.contentType ??
    "text/plain"
  );
}

export function buildSampleLogPayload(
  serviceName: string,
  format: ServiceFormat
): string {
  const safeService = serviceName.trim() || "app-service";
  const now = new Date();
  const isoTs = now.toISOString().replace(/\.\d{3}Z$/, "Z");
  const months = [
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
  ];
  const day = String(now.getUTCDate()).padStart(2, "0");
  const month = months[now.getUTCMonth()];
  const year = now.getUTCFullYear();
  const hh = String(now.getUTCHours()).padStart(2, "0");
  const mm = String(now.getUTCMinutes()).padStart(2, "0");
  const ss = String(now.getUTCSeconds()).padStart(2, "0");
  const nginxTs = `${day}/${month}/${year}:${hh}:${mm}:${ss} +0000`;

  switch (format) {
    case "json":
      return JSON.stringify(
        {
          timestamp: isoTs,
          level: "ERROR",
          service: safeService,
          message: "upstream request timed out after 5000ms",
          request_id: `req_${Math.floor(10000 + Math.random() * 90000)}`,
          latency_ms: 5021,
        },
        null,
        2
      );
    case "yaml":
      return [
        `timestamp: ${isoTs}`,
        "level: WARN",
        `service: ${safeService}`,
        "message: connection pool utilization above 85%",
        "active_connections: 43",
        "max_connections: 50",
      ].join("\n");
    case "syslog-rfc5424":
      return `<165>1 ${isoTs} prod-01 ${safeService} 4812 ID47 - upstream prematurely closed connection while reading response header`;
    case "logfmt":
      return `ts=${isoTs} level=info service=${safeService} msg="job completed" duration_ms=142 region=us-east-1`;
    case "nginx":
      return `198.51.100.14 - - [${nginxTs}] "POST /v1/${safeService}/events HTTP/1.1" 502 559 "https://app.example.com" "Mozilla/5.0"`;
    case "plain-text":
    default:
      return `${isoTs} [ERROR] ${safeService}: database connection timed out at host=db.internal port=5432`;
  }
}

export function buildCurlSnippet(
  serviceName: string,
  topic: string,
  format: ServiceFormat,
  payload: string
): string {
  const contentType = getContentTypeForFormat(format);
  const query = topic && topic !== "raw-logs" ? `?topic=${encodeURIComponent(topic)}` : "";
  const escaped = payload.replace(/'/g, "'\\''");
  return `curl -X POST "http://localhost:8000/logs${query}" \\\n  -H "Content-Type: ${contentType}" \\\n  -d '${escaped}'`;
}

export function buildKafkaSnippet(topic: string, payload: string): string {
  const targetTopic = topic.trim() || "raw-logs";
  const singleLine = payload.replace(/\n+/g, " ").replace(/'/g, "'\\''");
  return `echo '${singleLine}' | docker compose exec -T redpanda rpk topic produce ${targetTopic}`;
}
