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
export type NotificationChannel = "email" | "sms";
export type NotificationStatus = "sent" | "routed" | "suppressed" | "failed";
export type AlertStatus = "open" | "acked" | "resolved";
export type RunbookFormat = "yaml" | "markdown" | "text";
export type ApprovalMode = "none" | "required";

export interface RunbookStep {
  index: number;
  label: string;
  action: string | null;
  params: Record<string, unknown>;
  approval: ApprovalMode;
  condition_note?: string | null;
}

export interface RunbookDefinition {
  id: string;
  name: string;
  format: RunbookFormat;
  rule_ids: string[];
  services: string[];
  description: string;
  content: string;
}

export interface ResolvedRunbook {
  id: string;
  name: string;
  format: RunbookFormat;
  description: string;
  rendered_content: string;
  steps: RunbookStep[];
}

export interface StepExecutionRecord {
  step_index: number;
  runbook_id: string;
  action: string;
  label: string;
  status: "executed" | "failed";
  actor: string;
  approved: boolean;
  executed_at: string;
  output: string;
  note?: string | null;
}

export interface AlertAuditEvent {
  action:
    | "opened"
    | "deduplicated"
    | "acked"
    | "resolved"
    | "reopened"
    | "step_executed";
  actor: string;
  at: string;
  note?: string | null;
}

export interface AlertRecord {
  id: string;
  rule_id: string;
  rule_name: string;
  severity: RuleSeverity;
  status: AlertStatus;
  service: string | null;
  group_key: string;
  summary: string;
  message: string;
  count: number;
  dedup_window_seconds: number;
  first_seen: string;
  last_seen: string;
  acknowledged_by: string | null;
  acknowledged_at: string | null;
  resolved_by: string | null;
  resolved_at: string | null;
  notifications: NotificationDecision[];
  sample_raw: string;
  sample_metadata: Record<string, unknown>;
  history: AlertAuditEvent[];
  runbook?: ResolvedRunbook | null;
  step_executions?: StepExecutionRecord[];
}

export interface NotificationDecision {
  rule_id: string;
  rule_name: string;
  severity: RuleSeverity;
  channel: NotificationChannel;
  target: string;
  working_hours_only: boolean;
  status: NotificationStatus;
  reason: string;
}

export type NotifySpec =
  | "email"
  | "sms"
  | "sms_working_hours"
  | {
      channel: "email" | "sms" | "sms_working_hours";
      to?: string;
      working_hours_only?: boolean;
      working_hours?: {
        start?: string;
        end?: string;
        days?: number[];
        timezone?: string;
      };
    };

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
  notify?: NotifySpec | NotifySpec[];
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
  event?: Record<string, unknown> | null;
  parse_mode?: "fast_json" | "fast_yaml" | "schema_cache" | "llm";
  matched_rules?: RuleMatch[];
  notifications?: NotificationDecision[];
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
