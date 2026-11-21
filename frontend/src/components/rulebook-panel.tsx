"use client";

import { useCallback, useEffect, useState } from "react";
import {
  CheckCircle2,
  Code2,
  Play,
  Plus,
  RefreshCw,
  Save,
  ShieldAlert,
  Trash2,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import type { RuleMatch, RuleSeverity, RulebookRule } from "@/lib/types";
import { cn } from "@/lib/utils";

const SEVERITY_BADGE: Record<RuleSeverity, string> = {
  critical:
    "border-red-500/40 bg-red-500/15 text-red-700 dark:text-red-300 font-semibold",
  high: "border-rose-500/35 bg-rose-500/10 text-rose-700 dark:text-rose-300 font-medium",
  medium:
    "border-amber-500/35 bg-amber-500/10 text-amber-700 dark:text-amber-300 font-medium",
  low: "border-sky-500/30 bg-sky-500/10 text-sky-700 dark:text-sky-300",
};

const SAMPLE_TEST_MESSAGE = JSON.stringify(
  {
    timestamp: "2026-11-21T03:00:00Z",
    level: "ERROR",
    service: "checkout-api",
    message: "payment gateway timed out",
    summary: "Checkout request timed out contacting payment gateway.",
    metadata: {
      latency_ms: 5021,
      order_id: "ord_98421",
    },
  },
  null,
  2
);

function summarizeCondition(rule: RulebookRule): string {
  const cond = rule.condition ?? rule;
  if (Array.isArray(cond.all)) {
    return `ALL (${cond.all.length} conditions)`;
  }
  if (Array.isArray(cond.any)) {
    return `ANY (${cond.any.length} conditions)`;
  }
  if (cond.not) {
    return "NOT (...)";
  }
  if (cond.field && cond.operator) {
    return `${cond.field} ${cond.operator} ${JSON.stringify(cond.value)}`;
  }
  return "Custom rule";
}

export function RulebookPanel() {
  const [rules, setRules] = useState<RulebookRule[]>([]);
  const [jsonText, setJsonText] = useState("[]");
  const [editMode, setEditMode] = useState(false);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [statusMsg, setStatusMsg] = useState<{
    kind: "success" | "error";
    text: string;
  } | null>(null);

  const [testPayload, setTestPayload] = useState(SAMPLE_TEST_MESSAGE);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{
    matched: boolean;
    matched_rules: RuleMatch[];
  } | null>(null);
  const [testError, setTestError] = useState<string | null>(null);

  const fetchRules = useCallback(async () => {
    setLoading(true);
    setStatusMsg(null);
    try {
      const res = await fetch("/api/rules", { cache: "no-store" });
      if (!res.ok) {
        const err = await res.text();
        setStatusMsg({ kind: "error", text: `Load failed (${res.status}): ${err}` });
        return;
      }
      const data = await res.json();
      const list = Array.isArray(data) ? (data as RulebookRule[]) : [];
      setRules(list);
      setJsonText(JSON.stringify(list, null, 2));
    } catch (err) {
      setStatusMsg({
        kind: "error",
        text: err instanceof Error ? err.message : "Unable to load rules",
      });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchRules();
  }, [fetchRules]);

  async function persistRules(nextRules: RulebookRule[]) {
    setSaving(true);
    setStatusMsg(null);
    try {
      const res = await fetch("/api/rules", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(nextRules),
      });
      const text = await res.text();
      if (!res.ok) {
        let detail = text;
        try {
          const parsed = JSON.parse(text);
          if (parsed.detail) detail = parsed.detail;
        } catch {
          // keep raw
        }
        setStatusMsg({ kind: "error", text: detail });
        return false;
      }
      const saved = JSON.parse(text) as RulebookRule[];
      setRules(saved);
      setJsonText(JSON.stringify(saved, null, 2));
      setStatusMsg({
        kind: "success",
        text: `Saved ${saved.length} rule${saved.length === 1 ? "" : "s"} to rules.json.`,
      });
      return true;
    } catch (err) {
      setStatusMsg({
        kind: "error",
        text: err instanceof Error ? err.message : "Failed to save rules",
      });
      return false;
    } finally {
      setSaving(false);
    }
  }

  async function handleSaveJson() {
    try {
      const parsed = JSON.parse(jsonText);
      const list = Array.isArray(parsed) ? parsed : [parsed];
      const ok = await persistRules(list);
      if (ok) setEditMode(false);
    } catch (err) {
      setStatusMsg({
        kind: "error",
        text: `Invalid JSON: ${err instanceof Error ? err.message : "parse error"}`,
      });
    }
  }

  function handleAddTemplateRule() {
    const newRule: RulebookRule = {
      id: `custom-rule-${rules.length + 1}`,
      name: `Custom Rule ${rules.length + 1}`,
      description: "Matches ERROR logs for a specific service",
      severity: "high",
      enabled: true,
      all: [
        { field: "/level", operator: "eq", value: "ERROR" },
        { field: "/service", operator: "eq", value: "checkout-api" },
      ],
    };
    const next = [...rules, newRule];
    setRules(next);
    setJsonText(JSON.stringify(next, null, 2));
    setEditMode(true);
    setStatusMsg(null);
  }

  async function handleToggleEnabled(idx: number, enabled: boolean) {
    const next = rules.map((r, i) => (i === idx ? { ...r, enabled } : r));
    await persistRules(next);
  }

  async function handleDeleteRule(idx: number) {
    const next = rules.filter((_, i) => i !== idx);
    await persistRules(next);
  }

  async function handleEvaluate() {
    setTesting(true);
    setTestError(null);
    setTestResult(null);
    try {
      const parsedMsg = JSON.parse(testPayload);
      const body: Record<string, unknown> = { message: parsedMsg };
      if (editMode) {
        body.rules = JSON.parse(jsonText);
      }
      const res = await fetch("/api/rules/evaluate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) {
        setTestError(data.detail || data.error || `HTTP ${res.status}`);
        return;
      }
      setTestResult(data);
    } catch (err) {
      setTestError(
        err instanceof Error ? err.message : "Failed to evaluate test message"
      );
    } finally {
      setTesting(false);
    }
  }

  return (
    <div className="grid h-[564px] grid-cols-1 gap-3 xl:grid-cols-12">
      <div className="flex flex-col overflow-hidden rounded-lg border xl:col-span-7">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b bg-muted/40 px-3 py-2">
          <div className="flex items-center gap-2">
            <ShieldAlert className="size-4 text-amber-500" />
            <span className="text-xs font-semibold">
              Rulebook (`rules.json`)
            </span>
            <Badge variant="secondary" className="font-mono text-[10px]">
              {rules.length}
            </Badge>
          </div>
          <div className="flex items-center gap-1.5">
            <Button
              variant="outline"
              size="xs"
              onClick={handleAddTemplateRule}
              className="gap-1"
            >
              <Plus className="size-3" />
              Add Rule
            </Button>
            <Button
              variant={editMode ? "secondary" : "outline"}
              size="xs"
              onClick={() => {
                setEditMode((prev) => !prev);
                setStatusMsg(null);
              }}
              className="gap-1"
            >
              <Code2 className="size-3" />
              {editMode ? "List View" : "Edit JSON"}
            </Button>
            <Button
              variant="ghost"
              size="icon-xs"
              onClick={() => void fetchRules()}
              disabled={loading}
              aria-label="Reload rules"
            >
              <RefreshCw
                className={`size-3.5 ${loading ? "animate-spin" : ""}`}
              />
            </Button>
          </div>
        </div>

        {statusMsg && (
          <div
            className={`border-b px-3 py-1.5 text-xs ${
              statusMsg.kind === "success"
                ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
                : "bg-rose-500/10 text-rose-700 dark:text-rose-300"
            }`}
          >
            {statusMsg.text}
          </div>
        )}

        {editMode ? (
          <div className="flex flex-1 flex-col gap-2 p-3 overflow-hidden">
            <Textarea
              value={jsonText}
              onChange={(e) => setJsonText(e.target.value)}
              className="flex-1 resize-none font-mono text-xs leading-relaxed"
              spellCheck={false}
            />
            <div className="flex items-center justify-between gap-2">
              <span className="text-[11px] text-muted-foreground">
                Supports JSON Pointer paths (`/level`, `/metadata/latency_ms`), `all`, `any`, `not`, and operators `eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `in`, `not_in`, `contains`, `regex`, `exists`.
              </span>
              <Button
                size="sm"
                onClick={() => void handleSaveJson()}
                disabled={saving}
                className="shrink-0 gap-1.5"
              >
                <Save className="size-3.5" />
                {saving ? "Saving..." : "Save Rulebook"}
              </Button>
            </div>
          </div>
        ) : (
          <ScrollArea className="flex-1 p-3">
            <div className="space-y-2">
              {rules.length === 0 ? (
                <div className="rounded-lg border border-dashed p-6 text-center text-xs text-muted-foreground">
                  No rules configured. Click &ldquo;Add Rule&rdquo; or switch to
                  &ldquo;Edit JSON&rdquo;.
                </div>
              ) : (
                rules.map((rule, idx) => {
                  const sev = (rule.severity ?? "medium") as RuleSeverity;
                  const enabled = rule.enabled !== false;
                  return (
                    <div
                      key={rule.id ?? idx}
                      className={cn(
                        "rounded-lg border p-3 transition-opacity",
                        !enabled && "opacity-55"
                      )}
                    >
                      <div className="flex items-start justify-between gap-2">
                        <div className="min-w-0 space-y-1">
                          <div className="flex flex-wrap items-center gap-1.5">
                            <span className="text-sm font-medium text-foreground">
                              {rule.name || rule.id || `Rule ${idx + 1}`}
                            </span>
                            <Badge
                              variant="outline"
                              className={cn(
                                "font-mono text-[10px] uppercase",
                                SEVERITY_BADGE[sev] ?? SEVERITY_BADGE.medium
                              )}
                            >
                              {sev}
                            </Badge>
                          </div>
                          {rule.description && (
                            <p className="text-xs text-muted-foreground">
                              {rule.description}
                            </p>
                          )}
                          <p className="font-mono text-[11px] text-sky-600 dark:text-sky-400">
                            {summarizeCondition(rule)}
                          </p>
                        </div>

                        <div className="flex shrink-0 items-center gap-2">
                          <Switch
                            size="sm"
                            checked={enabled}
                            onCheckedChange={(checked) =>
                              void handleToggleEnabled(idx, Boolean(checked))
                            }
                            aria-label={`Toggle ${rule.name || rule.id}`}
                          />
                          <Button
                            variant="ghost"
                            size="icon-xs"
                            onClick={() => void handleDeleteRule(idx)}
                            className="text-muted-foreground hover:text-destructive"
                            aria-label={`Delete ${rule.name || rule.id}`}
                          >
                            <Trash2 className="size-3.5" />
                          </Button>
                        </div>
                      </div>
                    </div>
                  );
                })
              )}
            </div>
          </ScrollArea>
        )}
      </div>

      <div className="flex flex-col overflow-hidden rounded-lg border xl:col-span-5">
        <div className="flex items-center justify-between border-b bg-muted/40 px-3 py-2">
          <span className="text-xs font-semibold">Live Rule Tester</span>
          <Button
            size="xs"
            onClick={() => void handleEvaluate()}
            disabled={testing}
            className="gap-1"
          >
            <Play className="size-3" />
            {testing ? "Testing..." : "Evaluate"}
          </Button>
        </div>

        <div className="flex flex-1 flex-col gap-2.5 p-3 overflow-hidden">
          <Textarea
            value={testPayload}
            onChange={(e) => setTestPayload(e.target.value)}
            className="flex-1 resize-none font-mono text-xs leading-relaxed"
            spellCheck={false}
          />

          <Separator />

          <div className="min-h-[84px] rounded-lg border bg-muted/30 p-2.5 text-xs">
            {testError ? (
              <p className="font-mono text-rose-600 dark:text-rose-400">
                {testError}
              </p>
            ) : testResult ? (
              <div className="space-y-1.5">
                <div className="flex items-center gap-1.5 font-medium">
                  {testResult.matched ? (
                    <>
                      <ShieldAlert className="size-4 text-rose-500" />
                      <span className="text-rose-600 dark:text-rose-300">
                        Matched {testResult.matched_rules.length} rule
                        {testResult.matched_rules.length === 1 ? "" : "s"}
                      </span>
                    </>
                  ) : (
                    <>
                      <CheckCircle2 className="size-4 text-emerald-500" />
                      <span className="text-emerald-600 dark:text-emerald-300">
                        No rules triggered
                      </span>
                    </>
                  )}
                </div>
                {testResult.matched_rules.length > 0 && (
                  <div className="flex flex-wrap gap-1.5">
                    {testResult.matched_rules.map((m) => (
                      <Badge
                        key={m.id}
                        variant="outline"
                        className={cn(
                          "font-mono text-[10px]",
                          SEVERITY_BADGE[m.severity] ?? SEVERITY_BADGE.medium
                        )}
                      >
                        {m.name} ({m.severity})
                      </Badge>
                    ))}
                  </div>
                )}
              </div>
            ) : (
              <p className="text-muted-foreground">
                Click &ldquo;Evaluate&rdquo; to test this JSON message against{" "}
                {editMode ? "your unsaved JSON draft" : "the active rulebook"}.
              </p>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
