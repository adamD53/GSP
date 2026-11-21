"use client";

import { useCallback, useEffect, useState } from "react";
import { BookOpen, Plus, RefreshCw, Save, Trash2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import type { RunbookDefinition, RunbookFormat } from "@/lib/types";

const TEMPLATE_BY_FORMAT: Record<RunbookFormat, string> = {
  yaml: `runbook: custom-remediation
steps:
  - check: "Verify {{service}} health"
    action: http_get
    url: "http://{{service}}.internal/health"
    approval: none
  - check: "Restart {{service}} deployment"
    action: restart_service
    approval: required`,
  markdown: `# Incident Playbook: {{service}}

1. Open the {{service}} metrics dashboard and inspect error rate.
2. Probe {{service}} health endpoint.
3. Fail traffic over to secondary region (needs approval from team lead).
4. Post an update in #ops-incidents every 15 minutes.`,
  text: `When {{service}} triggers an alert ({{summary}}), verify upstream dependencies, restart the {{service}} instance, wait 2 minutes, and escalate to on-call if the issue persists.`,
};

export function RunbooksPanel() {
  const [runbooks, setRunbooks] = useState<RunbookDefinition[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [statusMsg, setStatusMsg] = useState<{
    kind: "success" | "error";
    text: string;
  } | null>(null);

  const [editingId, setEditingId] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [format, setFormat] = useState<RunbookFormat>("yaml");
  const [ruleIdsInput, setRuleIdsInput] = useState("");
  const [servicesInput, setServicesInput] = useState("");
  const [description, setDescription] = useState("");
  const [content, setContent] = useState(TEMPLATE_BY_FORMAT.yaml);

  const fetchRunbooks = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch("/api/runbooks", { cache: "no-store" });
      if (res.ok) {
        const data = await res.json();
        if (Array.isArray(data)) setRunbooks(data);
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchRunbooks();
  }, [fetchRunbooks]);

  function startNew(nextFmt: RunbookFormat = "yaml") {
    setEditingId(null);
    setName("");
    setFormat(nextFmt);
    setRuleIdsInput("");
    setServicesInput("");
    setDescription("");
    setContent(TEMPLATE_BY_FORMAT[nextFmt]);
    setStatusMsg(null);
  }

  function loadForEdit(rb: RunbookDefinition) {
    setEditingId(rb.id);
    setName(rb.name);
    setFormat(rb.format);
    setRuleIdsInput(rb.rule_ids.join(", "));
    setServicesInput(rb.services.join(", "));
    setDescription(rb.description);
    setContent(rb.content);
    setStatusMsg(null);
  }

  async function handleSave(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim() || !content.trim()) {
      setStatusMsg({ kind: "error", text: "Name and content are required." });
      return;
    }

    setSaving(true);
    setStatusMsg(null);
    const id =
      editingId ??
      `rb-${name
        .trim()
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "-")}-${Date.now().toString(36).slice(-4)}`;

    const payload: RunbookDefinition = {
      id,
      name: name.trim(),
      format,
      rule_ids: ruleIdsInput
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean),
      services: servicesInput
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean),
      description: description.trim(),
      content,
    };

    try {
      const res = await fetch(
        editingId ? `/api/runbooks/${encodeURIComponent(editingId)}` : "/api/runbooks",
        {
          method: editingId ? "PUT" : "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        }
      );
      const text = await res.text();
      if (!res.ok) {
        setStatusMsg({ kind: "error", text: `Save failed (${res.status}): ${text}` });
        return;
      }
      const next = JSON.parse(text) as RunbookDefinition[];
      setRunbooks(next);
      setEditingId(id);
      setStatusMsg({ kind: "success", text: `Saved runbook "${payload.name}".` });
    } catch (err) {
      setStatusMsg({
        kind: "error",
        text: err instanceof Error ? err.message : "Failed to save runbook",
      });
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete(id: string) {
    const res = await fetch(`/api/runbooks/${encodeURIComponent(id)}`, {
      method: "DELETE",
    });
    if (res.ok) {
      const next = (await res.json()) as RunbookDefinition[];
      setRunbooks(next);
      if (editingId === id) startNew();
    }
  }

  return (
    <div className="grid min-h-[540px] grid-cols-1 gap-3 xl:grid-cols-12">
      <div className="flex flex-col rounded-lg border xl:col-span-5">
        <div className="flex items-center justify-between border-b bg-muted/40 px-3 py-2">
          <div className="flex items-center gap-2">
            <BookOpen className="size-4 text-sky-500" />
            <span className="text-xs font-semibold">Attached Runbooks</span>
            <Badge variant="secondary" className="font-mono text-[10px]">
              {runbooks.length}
            </Badge>
          </div>
          <div className="flex items-center gap-1">
            <Button
              size="xs"
              variant="outline"
              onClick={() => startNew("yaml")}
              className="gap-1"
            >
              <Plus className="size-3" />
              New
            </Button>
            <Button
              size="icon-xs"
              variant="ghost"
              onClick={() => void fetchRunbooks()}
              disabled={loading}
              aria-label="Refresh runbooks"
            >
              <RefreshCw className={`size-3.5 ${loading ? "animate-spin" : ""}`} />
            </Button>
          </div>
        </div>

        <div className="flex-1 p-3">
          <div className="space-y-2">
            {runbooks.map((rb) => (
              <div
                key={rb.id}
                onClick={() => loadForEdit(rb)}
                className={`cursor-pointer rounded-lg border p-2.5 transition-colors ${
                  editingId === rb.id
                    ? "border-primary/50 bg-primary/5"
                    : "hover:bg-muted/40"
                }`}
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0 space-y-1">
                    <div className="flex items-center gap-1.5">
                      <span className="truncate text-sm font-medium">
                        {rb.name}
                      </span>
                      <Badge
                        variant="outline"
                        className="font-mono text-[10px] uppercase"
                      >
                        {rb.format}
                      </Badge>
                    </div>
                    {rb.description && (
                      <p className="line-clamp-1 text-xs text-muted-foreground">
                        {rb.description}
                      </p>
                    )}
                    <div className="flex flex-wrap gap-1 pt-0.5">
                      {rb.rule_ids.map((r) => (
                        <Badge
                          key={r}
                          variant="secondary"
                          className="font-mono text-[10px]"
                        >
                          rule:{r}
                        </Badge>
                      ))}
                      {rb.services.map((s) => (
                        <Badge
                          key={s}
                          variant="outline"
                          className="font-mono text-[10px]"
                        >
                          svc:{s}
                        </Badge>
                      ))}
                    </div>
                  </div>
                  <Button
                    size="icon-xs"
                    variant="ghost"
                    onClick={(e) => {
                      e.stopPropagation();
                      void handleDelete(rb.id);
                    }}
                    className="text-muted-foreground hover:text-destructive"
                    aria-label={`Delete ${rb.name}`}
                  >
                    <Trash2 className="size-3.5" />
                  </Button>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>

      <form
        onSubmit={handleSave}
        className="flex flex-col rounded-lg border xl:col-span-7"
      >
        <div className="flex items-center justify-between border-b bg-muted/40 px-3 py-2">
          <span className="text-xs font-semibold">
            {editingId ? `Edit Runbook (${editingId})` : "Create Runbook"}
          </span>
          <Button size="xs" type="submit" disabled={saving} className="gap-1">
            <Save className="size-3" />
            {saving ? "Saving..." : "Save Runbook"}
          </Button>
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

        <div className="flex flex-1 flex-col gap-2.5 overflow-auto p-3">
          <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-3">
            <div className="space-y-1 sm:col-span-2">
              <Label htmlFor="rb-name" className="text-xs">
                Name
              </Label>
              <Input
                id="rb-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g. Settlement Timeout Remediation"
                className="h-8 text-xs"
              />
            </div>

            <div className="space-y-1">
              <Label htmlFor="rb-format" className="text-xs">
                Format
              </Label>
              <Select
                value={format}
                onValueChange={(val) => {
                  if (!val) return;
                  const next = val as RunbookFormat;
                  setFormat(next);
                  if (!editingId) setContent(TEMPLATE_BY_FORMAT[next]);
                }}
              >
                <SelectTrigger id="rb-format" className="h-8 w-full text-xs">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="yaml">YAML (Structured Steps)</SelectItem>
                  <SelectItem value="markdown">Markdown Page</SelectItem>
                  <SelectItem value="text">Free Text</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>

          <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
            <div className="space-y-1">
              <Label htmlFor="rb-rules" className="text-xs">
                Linked Rule IDs (comma-separated)
              </Label>
              <Input
                id="rb-rules"
                value={ruleIdsInput}
                onChange={(e) => setRuleIdsInput(e.target.value)}
                placeholder="high-latency-error, fatal-or-critical"
                className="h-8 font-mono text-xs"
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="rb-services" className="text-xs">
                Linked Services (comma-separated)
              </Label>
              <Input
                id="rb-services"
                value={servicesInput}
                onChange={(e) => setServicesInput(e.target.value)}
                placeholder="checkout-api, payments-api"
                className="h-8 font-mono text-xs"
              />
            </div>
          </div>

          <div className="space-y-1">
            <Label htmlFor="rb-desc" className="text-xs">
              Description
            </Label>
            <Input
              id="rb-desc"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Short summary of when to use this runbook"
              className="h-8 text-xs"
            />
          </div>

          <div className="flex flex-1 flex-col space-y-1">
            <Label htmlFor="rb-content" className="text-xs">
              Runbook Content (supports {"{{service}}"}, {"{{metadata.order_id}}"},
              {" {{workflowId}}"})
            </Label>
            <Textarea
              id="rb-content"
              value={content}
              onChange={(e) => setContent(e.target.value)}
              className="min-h-[260px] flex-1 resize-y font-mono text-xs leading-relaxed"
              spellCheck={false}
            />
          </div>
        </div>
      </form>
    </div>
  );
}
