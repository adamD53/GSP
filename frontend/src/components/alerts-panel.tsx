"use client";

import { useMemo, useState } from "react";
import {
  BellRing,
  BookOpen,
  CheckCheck,
  CheckCircle2,
  Clock,
  History,
  Mail,
  MessageSquare,
  Play,
  RotateCcw,
  ShieldCheck,
  User,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { AlertRecord, AlertStatus, RuleSeverity } from "@/lib/types";
import { cn } from "@/lib/utils";

const SEVERITY_STYLE: Record<RuleSeverity, string> = {
  critical:
    "border-red-500/40 bg-red-500/15 text-red-700 dark:text-red-300 font-semibold",
  high: "border-rose-500/35 bg-rose-500/10 text-rose-700 dark:text-rose-300 font-medium",
  medium:
    "border-amber-500/35 bg-amber-500/10 text-amber-700 dark:text-amber-300 font-medium",
  low: "border-sky-500/30 bg-sky-500/10 text-sky-700 dark:text-sky-300",
};

const STATUS_STYLE: Record<AlertStatus, string> = {
  open: "border-rose-500/40 bg-rose-500/15 text-rose-700 dark:text-rose-300",
  acked: "border-amber-500/40 bg-amber-500/15 text-amber-700 dark:text-amber-300",
  resolved:
    "border-emerald-500/40 bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
};

function formatTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

interface AlertsPanelProps {
  alerts: AlertRecord[];
  selectedService: string | null;
  onTransition: (
    alertId: string,
    action: "ack" | "resolve" | "reopen",
    actor: string
  ) => Promise<void>;
  onExecuteStep: (
    alertId: string,
    stepIndex: number,
    actor: string,
    approved: boolean
  ) => Promise<void>;
}

export function AlertsPanel({
  alerts,
  selectedService,
  onTransition,
  onExecuteStep,
}: AlertsPanelProps) {
  const [statusFilter, setStatusFilter] = useState<AlertStatus | "all">("all");
  const [actor, setActor] = useState("oncall");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [busyStepKey, setBusyStepKey] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [openRunbookId, setOpenRunbookId] = useState<string | null>(null);

  const filtered = useMemo(() => {
    return alerts.filter((a) => {
      if (statusFilter !== "all" && a.status !== statusFilter) return false;
      if (
        selectedService &&
        (a.service ?? "").toLowerCase() !== selectedService.toLowerCase()
      ) {
        return false;
      }
      return true;
    });
  }, [alerts, statusFilter, selectedService]);

  const counts = useMemo(() => {
    let open = 0;
    let acked = 0;
    let resolved = 0;
    for (const a of alerts) {
      if (a.status === "open") open += 1;
      else if (a.status === "acked") acked += 1;
      else resolved += 1;
    }
    return { open, acked, resolved };
  }, [alerts]);

  async function handleAction(
    alertId: string,
    action: "ack" | "resolve" | "reopen"
  ) {
    setBusyId(alertId);
    try {
      await onTransition(alertId, action, actor.trim() || "oncall");
    } finally {
      setBusyId(null);
    }
  }

  async function handleStepRun(
    alertId: string,
    stepIndex: number,
    approved: boolean
  ) {
    const key = `${alertId}:${stepIndex}`;
    setBusyStepKey(key);
    try {
      await onExecuteStep(alertId, stepIndex, actor.trim() || "oncall", approved);
    } finally {
      setBusyStepKey(null);
    }
  }

  return (
    <div className="flex min-h-[520px] flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-1.5">
          {(["all", "open", "acked", "resolved"] as const).map((st) => {
            const count =
              st === "all"
                ? alerts.length
                : st === "open"
                  ? counts.open
                  : st === "acked"
                    ? counts.acked
                    : counts.resolved;
            return (
              <Button
                key={st}
                size="xs"
                variant={statusFilter === st ? "default" : "outline"}
                onClick={() => setStatusFilter(st)}
                className="gap-1.5 capitalize"
              >
                {st}
                <Badge
                  variant={statusFilter === st ? "secondary" : "outline"}
                  className="font-mono text-[10px]"
                >
                  {count}
                </Badge>
              </Button>
            );
          })}
        </div>

        <div className="flex items-center gap-1.5">
          <User className="size-3.5 text-muted-foreground" />
          <Input
            value={actor}
            onChange={(e) => setActor(e.target.value)}
            placeholder="Responder name"
            className="h-7 w-32 font-mono text-xs"
            aria-label="Responder actor name"
          />
        </div>
      </div>

      <div className="flex-1 rounded-lg border p-3">
        {filtered.length === 0 ? (
          <div className="flex min-h-[360px] flex-col items-center justify-center p-6 text-center">
            <BellRing className="mb-2 size-7 text-muted-foreground/50" />
            <p className="text-sm font-medium">No alerts in this view</p>
            <p className="mt-1 max-w-sm text-xs text-muted-foreground">
              When incoming logs match your rulebook, deduplicated alerts appear
              here with live counts and Acknowledge / Resolve controls.
            </p>
          </div>
        ) : (
          <div className="space-y-2.5">
            {filtered.map((alert) => {
              const isBusy = busyId === alert.id;
              const isExpanded = expandedId === alert.id;
              return (
                <div
                  key={alert.id}
                  className="rounded-lg border bg-card p-3 transition-colors hover:border-border"
                >
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0 flex-1 space-y-1.5">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <Badge
                          variant="outline"
                          className={cn(
                            "font-mono text-[10px] uppercase",
                            SEVERITY_STYLE[alert.severity]
                          )}
                        >
                          {alert.severity}
                        </Badge>
                        <Badge
                          variant="outline"
                          className={cn(
                            "font-mono text-[10px] uppercase",
                            STATUS_STYLE[alert.status]
                          )}
                        >
                          {alert.status}
                        </Badge>
                        <Badge
                          variant={alert.count > 1 ? "default" : "secondary"}
                          className="font-mono text-[11px] tabular-nums"
                        >
                          ×{alert.count}
                        </Badge>
                        <span className="text-sm font-semibold text-foreground">
                          {alert.rule_name}
                        </span>
                        {alert.service && (
                          <Badge
                            variant="outline"
                            className="font-mono text-[10px]"
                          >
                            {alert.service}
                          </Badge>
                        )}
                      </div>

                      <p className="text-xs leading-relaxed text-foreground">
                        {alert.summary}
                      </p>

                      <div className="flex flex-wrap items-center gap-2 font-mono text-[11px] text-muted-foreground">
                        <span>group: {alert.group_key}</span>
                        <span>•</span>
                        <span>
                          {formatTime(alert.first_seen)} →{" "}
                          {formatTime(alert.last_seen)}
                        </span>
                        {alert.acknowledged_by && (
                          <>
                            <span>•</span>
                            <span className="text-amber-600 dark:text-amber-300">
                              acked by {alert.acknowledged_by}
                            </span>
                          </>
                        )}
                        {alert.resolved_by && (
                          <>
                            <span>•</span>
                            <span className="text-emerald-600 dark:text-emerald-300">
                              resolved by {alert.resolved_by}
                            </span>
                          </>
                        )}
                      </div>

                      {alert.notifications.length > 0 && (
                        <div className="flex flex-wrap gap-1 pt-0.5">
                          {alert.notifications.map((n, idx) => (
                            <Badge
                              key={`${n.channel}-${idx}`}
                              variant="outline"
                              className={
                                n.status === "suppressed"
                                  ? "gap-1 border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300 font-mono text-[10px]"
                                  : "gap-1 border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 font-mono text-[10px]"
                              }
                            >
                              {n.channel === "email" ? (
                                <Mail className="size-2.5" />
                              ) : (
                                <MessageSquare className="size-2.5" />
                              )}
                              {n.channel.toUpperCase()} → {n.target}
                              {n.status === "suppressed" && (
                                <>
                                  <Clock className="size-2.5" />
                                  suppressed
                                </>
                              )}
                            </Badge>
                          ))}
                        </div>
                      )}
                    </div>

                    <div className="flex shrink-0 flex-wrap items-center gap-1.5">
                      {alert.runbook && (
                        <Button
                          size="xs"
                          variant={
                            openRunbookId === alert.id ? "secondary" : "outline"
                          }
                          onClick={() =>
                            setOpenRunbookId(
                              openRunbookId === alert.id ? null : alert.id
                            )
                          }
                          className="gap-1"
                        >
                          <BookOpen className="size-3" />
                          Runbook
                        </Button>
                      )}
                      {alert.status === "open" && (
                        <Button
                          size="xs"
                          variant="outline"
                          disabled={isBusy}
                          onClick={() => void handleAction(alert.id, "ack")}
                          className="gap-1"
                        >
                          <CheckCheck className="size-3" />
                          Ack
                        </Button>
                      )}
                      {alert.status !== "resolved" ? (
                        <Button
                          size="xs"
                          disabled={isBusy}
                          onClick={() => void handleAction(alert.id, "resolve")}
                          className="gap-1"
                        >
                          <CheckCircle2 className="size-3" />
                          Resolve
                        </Button>
                      ) : (
                        <Button
                          size="xs"
                          variant="outline"
                          disabled={isBusy}
                          onClick={() => void handleAction(alert.id, "reopen")}
                          className="gap-1"
                        >
                          <RotateCcw className="size-3" />
                          Reopen
                        </Button>
                      )}
                      <Button
                        size="icon-xs"
                        variant="ghost"
                        onClick={() =>
                          setExpandedId(isExpanded ? null : alert.id)
                        }
                        aria-label="Toggle audit history"
                      >
                        <History className="size-3.5" />
                      </Button>
                    </div>
                  </div>

                  {alert.runbook &&
                    (openRunbookId === alert.id ||
                      (openRunbookId === null && alert.status !== "resolved")) && (
                      <div className="mt-3 rounded-lg border bg-muted/30 p-3 space-y-2">
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <div className="flex items-center gap-1.5">
                            <BookOpen className="size-3.5 text-sky-500" />
                            <span className="text-xs font-semibold">
                              {alert.runbook.name}
                            </span>
                            <Badge
                              variant="outline"
                              className="font-mono text-[10px] uppercase"
                            >
                              {alert.runbook.format}
                            </Badge>
                          </div>
                          <span className="text-[11px] text-muted-foreground">
                            Interpolated with live alert fields
                          </span>
                        </div>

                        <div className="space-y-1.5">
                          {alert.runbook.steps.map((step) => {
                            const exec = (alert.step_executions ?? [])
                              .slice()
                              .reverse()
                              .find((e) => e.step_index === step.index);
                            const stepBusy =
                              busyStepKey === `${alert.id}:${step.index}`;
                            const needsApproval = step.approval === "required";

                            return (
                              <div
                                key={step.index}
                                className="rounded-md border bg-card px-2.5 py-2 text-xs"
                              >
                                <div className="flex flex-wrap items-start justify-between gap-2">
                                  <div className="min-w-0 flex-1 space-y-0.5">
                                    <div className="flex flex-wrap items-center gap-1.5">
                                      <span className="font-mono text-[11px] text-muted-foreground">
                                        #{step.index + 1}
                                      </span>
                                      <span className="font-medium text-foreground">
                                        {step.label}
                                      </span>
                                      {step.action && (
                                        <Badge
                                          variant="secondary"
                                          className="font-mono text-[10px]"
                                        >
                                          {step.action}
                                        </Badge>
                                      )}
                                      {needsApproval && !exec && (
                                        <Badge
                                          variant="outline"
                                          className="border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300 font-mono text-[10px]"
                                        >
                                          Approval Required
                                        </Badge>
                                      )}
                                    </div>
                                    {step.condition_note && (
                                      <p className="font-mono text-[11px] text-muted-foreground">
                                        if: {step.condition_note}
                                      </p>
                                    )}
                                  </div>

                                  <div className="shrink-0">
                                    {exec ? (
                                      <Badge
                                        variant="outline"
                                        className="border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 gap-1 font-mono text-[10px]"
                                      >
                                        <CheckCircle2 className="size-3" />
                                        {exec.approved ? "Approved & Ran" : "Ran"}
                                      </Badge>
                                    ) : needsApproval ? (
                                      <Button
                                        size="xs"
                                        variant="destructive"
                                        disabled={stepBusy}
                                        onClick={() =>
                                          void handleStepRun(
                                            alert.id,
                                            step.index,
                                            true
                                          )
                                        }
                                        className="gap-1"
                                      >
                                        <ShieldCheck className="size-3" />
                                        {stepBusy ? "Running..." : "Approve & Run"}
                                      </Button>
                                    ) : (
                                      <Button
                                        size="xs"
                                        variant="outline"
                                        disabled={stepBusy}
                                        onClick={() =>
                                          void handleStepRun(
                                            alert.id,
                                            step.index,
                                            false
                                          )
                                        }
                                        className="gap-1"
                                      >
                                        <Play className="size-3" />
                                        {stepBusy ? "Running..." : "Run Step"}
                                      </Button>
                                    )}
                                  </div>
                                </div>

                                {exec && (
                                  <div className="mt-1.5 rounded bg-muted/60 px-2 py-1 font-mono text-[11px] text-emerald-700 dark:text-emerald-300">
                                    [{exec.actor} @ {formatTime(exec.executed_at)}]{" "}
                                    {exec.output}
                                  </div>
                                )}
                              </div>
                            );
                          })}
                        </div>
                      </div>
                    )}

                  {isExpanded && alert.history.length > 0 && (
                    <div className="mt-2.5 border-t pt-2 space-y-1">
                      <p className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                        Audit History
                      </p>
                      {alert.history.map((ev, idx) => (
                        <div
                          key={idx}
                          className="flex flex-wrap items-center gap-2 font-mono text-[11px] text-muted-foreground"
                        >
                          <span>{formatTime(ev.at)}</span>
                          <Badge
                            variant="secondary"
                            className="font-mono text-[10px] uppercase"
                          >
                            {ev.action}
                          </Badge>
                          <span className="text-foreground">{ev.actor}</span>
                          {ev.note && <span>— {ev.note}</span>}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
