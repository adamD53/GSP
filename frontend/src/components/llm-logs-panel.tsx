"use client";

import { useMemo, useState } from "react";
import {
  AlertOctagon,
  Clock,
  FileText,
  Mail,
  MessageSquare,
  Search,
  ShieldAlert,
  Sparkles,
  Trash2,
  X,
} from "lucide-react";
import { LogDetailSheet } from "@/components/log-detail-sheet";
import { RulebookPanel } from "@/components/rulebook-panel";
import { SeverityBadge } from "@/components/severity-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  LOG_LEVELS,
  type DeadLetterLog,
  type LogLevel,
  type ParsedLogEntry,
} from "@/lib/types";

interface LLMLogsPanelProps {
  logs: ParsedLogEntry[];
  deadLetters: DeadLetterLog[];
  rawLogs: string;
  selectedService: string | null;
  onClearServiceFilter: () => void;
  onClearLogs: () => void;
}

function formatShortTime(ts: string | null): string {
  if (!ts) return "—";
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) {
    return ts.length > 20 ? ts.slice(0, 20) : ts;
  }
  return date.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

export function LLMLogsPanel({
  logs,
  deadLetters,
  rawLogs,
  selectedService,
  onClearServiceFilter,
  onClearLogs,
}: LLMLogsPanelProps) {
  const [search, setSearch] = useState("");
  const [levelFilter, setLevelFilter] = useState<LogLevel | "ALL">("ALL");
  const [alertsOnly, setAlertsOnly] = useState(false);
  const [inspectedLog, setInspectedLog] = useState<ParsedLogEntry | null>(null);

  const alertCount = useMemo(
    () =>
      logs.filter(
        (entry) =>
          entry.alert || (entry.matched_rules && entry.matched_rules.length > 0)
      ).length,
    [logs]
  );

  const filteredLogs = useMemo(() => {
    const q = search.trim().toLowerCase();
    return logs.filter((entry) => {
      if (
        alertsOnly &&
        !(entry.alert || (entry.matched_rules && entry.matched_rules.length > 0))
      ) {
        return false;
      }
      if (
        selectedService &&
        (entry.service ?? "").toLowerCase() !== selectedService.toLowerCase()
      ) {
        return false;
      }
      if (levelFilter !== "ALL" && entry.level !== levelFilter) {
        return false;
      }
      if (!q) return true;
      const haystack = [
        entry.summary,
        entry.message,
        entry.service ?? "",
        entry.level,
        entry.raw,
        JSON.stringify(entry.metadata ?? {}),
        (entry.matched_rules ?? []).map((r) => `${r.id} ${r.name}`).join(" "),
      ]
        .join(" ")
        .toLowerCase();
      return haystack.includes(q);
    });
  }, [logs, selectedService, levelFilter, alertsOnly, search]);

  return (
    <>
      <Card className="flex h-full flex-col">
        <CardHeader className="border-b pb-3">
          <div className="flex items-center gap-2">
            <Sparkles className="size-4 text-sky-500" />
            <CardTitle>LLM Output Log</CardTitle>
          </div>
          <CardDescription>
            Ollama-parsed structured summaries, severity, and extracted metadata
          </CardDescription>
          <CardAction>
            <Button
              variant="ghost"
              size="sm"
              onClick={onClearLogs}
              disabled={logs.length === 0}
              className="gap-1.5 text-xs text-muted-foreground"
            >
              <Trash2 className="size-3.5" />
              Clear View
            </Button>
          </CardAction>
        </CardHeader>

        <CardContent className="flex flex-1 flex-col gap-3 pt-3 overflow-hidden">
          <Tabs defaultValue="parsed" className="flex flex-1 flex-col overflow-hidden">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <TabsList>
                <TabsTrigger value="parsed" className="gap-1.5">
                  <Sparkles className="size-3.5" />
                  LLM Parsed
                  <Badge variant="secondary" className="ml-0.5 font-mono text-[10px]">
                    {filteredLogs.length}
                  </Badge>
                </TabsTrigger>
                <TabsTrigger value="dlq" className="gap-1.5">
                  <AlertOctagon className="size-3.5" />
                  Dead-Letter
                  <Badge
                    variant={deadLetters.length > 0 ? "destructive" : "secondary"}
                    className="ml-0.5 font-mono text-[10px]"
                  >
                    {deadLetters.length}
                  </Badge>
                </TabsTrigger>
                <TabsTrigger value="raw" className="gap-1.5">
                  <FileText className="size-3.5" />
                  Raw Logs
                </TabsTrigger>
                <TabsTrigger value="rules" className="gap-1.5">
                  <ShieldAlert className="size-3.5" />
                  Rulebook
                </TabsTrigger>
              </TabsList>

              {selectedService && (
                <Badge
                  variant="secondary"
                  className="gap-1.5 pl-2.5 pr-1 py-1 font-mono text-xs"
                >
                  service: {selectedService}
                  <button
                    type="button"
                    onClick={onClearServiceFilter}
                    className="rounded-sm p-0.5 hover:bg-muted-foreground/20"
                    aria-label="Clear service filter"
                  >
                    <X className="size-3" />
                  </button>
                </Badge>
              )}
            </div>

            <TabsContent
              value="parsed"
              className="mt-3 flex flex-1 flex-col gap-3 overflow-hidden"
            >
              <div className="flex flex-wrap items-center gap-2">
                <div className="relative flex-1 min-w-[200px]">
                  <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
                  <Input
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                    placeholder="Search LLM summary, message, service, metadata..."
                    className="pl-8 text-xs"
                  />
                </div>

                <Select
                  value={levelFilter}
                  onValueChange={(val) => {
                    if (val) setLevelFilter(val as LogLevel | "ALL");
                  }}
                >
                  <SelectTrigger className="w-[140px]">
                    <SelectValue placeholder="Severity" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="ALL">All Levels</SelectItem>
                    {LOG_LEVELS.map((lvl) => (
                      <SelectItem key={lvl} value={lvl}>
                        {lvl}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>

                <Button
                  type="button"
                  variant={alertsOnly ? "destructive" : "outline"}
                  size="sm"
                  onClick={() => setAlertsOnly((prev) => !prev)}
                  className="gap-1.5 text-xs"
                >
                  <ShieldAlert className="size-3.5" />
                  Alerts
                  <Badge
                    variant={alertsOnly ? "secondary" : "outline"}
                    className="font-mono text-[10px]"
                  >
                    {alertCount}
                  </Badge>
                </Button>
              </div>

              <div className="flex-1 overflow-hidden rounded-lg border">
                <ScrollArea className="h-[520px]">
                  {filteredLogs.length === 0 ? (
                    <div className="flex h-[440px] flex-col items-center justify-center p-6 text-center">
                      <Sparkles className="mb-2 size-7 text-muted-foreground/50" />
                      <p className="text-sm font-medium text-foreground">
                        No LLM-parsed logs to display
                      </p>
                      <p className="mt-1 max-w-sm text-xs text-muted-foreground">
                        {logs.length > 0
                          ? "Try clearing your search query, severity filter, or service filter."
                          : "Send a test log from the Services panel or run scripts/simulate_http_logs.py to see Ollama parse incoming logs live."}
                      </p>
                    </div>
                  ) : (
                    <Table>
                      <TableHeader className="sticky top-0 z-10 bg-muted/80 backdrop-blur-xs">
                        <TableRow>
                          <TableHead className="w-[86px]">Time</TableHead>
                          <TableHead className="w-[92px]">Level</TableHead>
                          <TableHead className="w-[140px]">Service</TableHead>
                          <TableHead>LLM Summary &amp; Message</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {filteredLogs.map((entry) => {
                          const metaKeys = Object.keys(entry.metadata ?? {});
                          return (
                            <TableRow
                              key={entry._id}
                              onClick={() => setInspectedLog(entry)}
                              className="cursor-pointer align-top transition-colors hover:bg-muted/50"
                            >
                              <TableCell className="font-mono text-xs text-muted-foreground whitespace-nowrap">
                                {formatShortTime(entry.timestamp)}
                              </TableCell>
                              <TableCell>
                                <SeverityBadge level={entry.level} />
                              </TableCell>
                              <TableCell>
                                {entry.service ? (
                                  <Badge
                                    variant="outline"
                                    className="max-w-[130px] truncate font-mono text-[11px]"
                                  >
                                    {entry.service}
                                  </Badge>
                                ) : (
                                  <span className="text-xs text-muted-foreground">
                                    —
                                  </span>
                                )}
                              </TableCell>
                              <TableCell className="max-w-[460px] whitespace-normal">
                                <div className="flex flex-wrap items-center gap-1.5">
                                  <p className="text-sm font-medium leading-snug text-foreground">
                                    {entry.summary || entry.message}
                                  </p>
                                  {entry.matched_rules &&
                                    entry.matched_rules.map((rule) => (
                                      <Badge
                                        key={rule.id}
                                        variant="destructive"
                                        className="gap-1 font-mono text-[10px]"
                                      >
                                        <ShieldAlert className="size-2.5" />
                                        {rule.name}
                                      </Badge>
                                    ))}
                                  {entry.notifications &&
                                    entry.notifications.map((notif, nIdx) => (
                                      <Badge
                                        key={`${notif.rule_id}-${notif.channel}-${nIdx}`}
                                        variant="outline"
                                        className={
                                          notif.status === "suppressed"
                                            ? "gap-1 border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300 font-mono text-[10px]"
                                            : "gap-1 border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 font-mono text-[10px]"
                                        }
                                      >
                                        {notif.channel === "email" ? (
                                          <Mail className="size-2.5" />
                                        ) : (
                                          <MessageSquare className="size-2.5" />
                                        )}
                                        {notif.channel.toUpperCase()}
                                        {notif.status === "suppressed" && (
                                          <>
                                            <Clock className="size-2.5" />
                                            off-hours
                                          </>
                                        )}
                                      </Badge>
                                    ))}
                                </div>
                                {entry.message &&
                                  entry.message !== entry.summary && (
                                    <p className="mt-0.5 line-clamp-1 font-mono text-xs text-muted-foreground">
                                      {entry.message}
                                    </p>
                                  )}
                                {metaKeys.length > 0 && (
                                  <div className="mt-1.5 flex flex-wrap gap-1">
                                    {metaKeys.slice(0, 4).map((k) => (
                                      <span
                                        key={k}
                                        className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground"
                                      >
                                        {k}={String(entry.metadata[k])}
                                      </span>
                                    ))}
                                    {metaKeys.length > 4 && (
                                      <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
                                        +{metaKeys.length - 4} more
                                      </span>
                                    )}
                                  </div>
                                )}
                              </TableCell>
                            </TableRow>
                          );
                        })}
                      </TableBody>
                    </Table>
                  )}
                </ScrollArea>
              </div>
            </TabsContent>

            <TabsContent value="dlq" className="mt-3 flex-1 overflow-hidden">
              <div className="overflow-hidden rounded-lg border">
                <ScrollArea className="h-[564px]">
                  {deadLetters.length === 0 ? (
                    <div className="flex h-[460px] flex-col items-center justify-center p-6 text-center">
                      <AlertOctagon className="mb-2 size-7 text-muted-foreground/50" />
                      <p className="text-sm font-medium">
                        Dead-letter queue is empty
                      </p>
                      <p className="mt-1 text-xs text-muted-foreground">
                        Logs that fail Ollama parsing or schema validation appear
                        here with their error reason.
                      </p>
                    </div>
                  ) : (
                    <Table>
                      <TableHeader className="sticky top-0 z-10 bg-muted/80 backdrop-blur-xs">
                        <TableRow>
                          <TableHead className="w-1/3">Parser Error</TableHead>
                          <TableHead>Raw Payload</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {deadLetters.map((item, idx) => (
                          <TableRow key={idx} className="align-top">
                            <TableCell className="whitespace-normal">
                              <Badge
                                variant="destructive"
                                className="mb-1 font-mono text-[10px]"
                              >
                                DLQ
                              </Badge>
                              <p className="font-mono text-xs text-rose-700 dark:text-rose-300">
                                {item.error}
                              </p>
                            </TableCell>
                            <TableCell className="whitespace-normal">
                              <pre className="font-mono text-xs text-muted-foreground whitespace-pre-wrap break-all">
                                {item.raw}
                              </pre>
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  )}
                </ScrollArea>
              </div>
            </TabsContent>

            <TabsContent value="raw" className="mt-3 flex-1 overflow-hidden">
              <ScrollArea className="h-[564px] rounded-lg border bg-zinc-950 p-4 text-zinc-100">
                {rawLogs.trim() ? (
                  <pre className="font-mono text-xs leading-relaxed whitespace-pre-wrap break-all">
                    {rawLogs}
                  </pre>
                ) : (
                  <p className="font-mono text-xs text-zinc-400">
                    No raw logs available in topic.
                  </p>
                )}
              </ScrollArea>
            </TabsContent>

            <TabsContent value="rules" className="mt-3 flex-1 overflow-hidden">
              <RulebookPanel />
            </TabsContent>
          </Tabs>
        </CardContent>
      </Card>

      <LogDetailSheet
        log={inspectedLog}
        onClose={() => setInspectedLog(null)}
      />
    </>
  );
}
