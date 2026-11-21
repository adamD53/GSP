export type LogLevel = "DEBUG" | "INFO" | "WARN" | "ERROR" | "FATAL" | "UNKNOWN";

export const LOG_LEVELS: LogLevel[] = [
  "FATAL",
  "ERROR",
  "WARN",
  "INFO",
  "DEBUG",
  "UNKNOWN",
];

export type RuleSeverity = "critical" | "high" | "medium" | "low";

export interface RuleMatch {
  id: string;
  name: string;
  severity: RuleSeverity;
}

export interface RulebookRule {
  id?: string;
  name?: string;
  description?: string;
  severity?: RuleSeverity;
  enabled?: boolean;
  field?: string;
  operator?: string;
  value?: unknown;
  all?: Record<string, unknown>[];
  any?: Record<string, unknown>[];
  not?: Record<string, unknown>;
  condition?: Record<string, unknown>;
}

export interface ParsedLog {
  timestamp: string | null;
  level: LogLevel;
  service: string | null;
  message: string;
  summary: string;
  metadata: Record<string, unknown>;
  raw: string;
  matched_rules?: RuleMatch[];
  alert?: boolean;
}

export interface ParsedLogEntry extends ParsedLog {
  _id: string;
  _receivedAt: number;
}

export interface DeadLetterLog {
  raw: string;
  error: string;
}

export type ServiceFormat =
  | "json"
  | "yaml"
  | "syslog-rfc5424"
  | "logfmt"
  | "nginx"
  | "plain-text";

export const SERVICE_FORMATS: {
  value: ServiceFormat;
  label: string;
  contentType: string;
}[] = [
  { value: "json", label: "JSON", contentType: "application/json" },
  { value: "yaml", label: "YAML", contentType: "application/x-yaml" },
  { value: "syslog-rfc5424", label: "Syslog RFC5424", contentType: "text/plain" },
  { value: "logfmt", label: "Logfmt", contentType: "text/plain" },
  { value: "nginx", label: "Nginx Combined", contentType: "text/plain" },
  { value: "plain-text", label: "Plain Text", contentType: "text/plain" },
];

export interface RegisteredService {
  id: string;
  name: string;
  description: string;
  topic: string;
  format: ServiceFormat;
  createdAt: string;
}
