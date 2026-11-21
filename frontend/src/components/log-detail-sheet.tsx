"use client";

import { useState } from "react";
import {
  Check,
  Clock,
  Copy,
  Mail,
  MessageSquare,
  ShieldAlert,
  ScrollText,
} from "lucide-react";
import { SeverityBadge } from "@/components/severity-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type { ParsedLogEntry } from "@/lib/types";

interface LogDetailSheetProps {
  log: ParsedLogEntry | null;
  onClose: () => void;
}

export function LogDetailSheet({ log, onClose }: LogDetailSheetProps) {
  const [copied, setCopied] = useState<string | null>(null);

  async function handleCopy(key: string, text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(key);
      setTimeout(() => setCopied(null), 1600);
    } catch {
      // Clipboard unavailable
    }
  }

  const metadataEntries = log ? Object.entries(log.metadata ?? {}) : [];

  return (
    <Sheet open={Boolean(log)} onOpenChange={(open) => !open && onClose()}>
      <SheetContent className="w-full sm:max-w-xl p-0 flex flex-col">
        {log && (
          <>
            <SheetHeader className="border-b px-5 py-4">
              <div className="flex flex-wrap items-center gap-2 pr-6">
                <SeverityBadge level={log.level} />
                {log.service && (
                  <Badge variant="secondary" className="font-mono text-xs">
                    {log.service}
                  </Badge>
                )}
                {log.timestamp && (
                  <span className="font-mono text-xs text-muted-foreground">
                    {log.timestamp}
                  </span>
                )}
              </div>
              <SheetTitle className="mt-1 text-base leading-snug">
                {log.summary || "Log Entry"}
              </SheetTitle>
              <SheetDescription className="text-xs">
                Structured log details and routing status
              </SheetDescription>
            </SheetHeader>

            <ScrollArea className="flex-1 px-5 py-4">
              <Tabs defaultValue="structured" className="w-full">
                <TabsList className="w-full">
                  <TabsTrigger value="structured">Structured Fields</TabsTrigger>
                  <TabsTrigger value="json">Full JSON</TabsTrigger>
                </TabsList>

                <TabsContent value="structured" className="mt-4 space-y-4">
                  <div className="rounded-lg border border-sky-500/25 bg-sky-500/5 p-3">
                    <div className="mb-1 flex items-center gap-1.5 text-xs font-medium text-sky-700 dark:text-sky-300">
                      <ScrollText className="size-3.5" />
                      Summary
                    </div>
                    <p className="text-sm leading-relaxed text-foreground">
                      {log.summary || "—"}
                    </p>
                  </div>

                  {log.matched_rules && log.matched_rules.length > 0 && (
                    <div className="rounded-lg border border-rose-500/30 bg-rose-500/10 p-3">
                      <div className="mb-1.5 flex items-center gap-1.5 text-xs font-medium text-rose-700 dark:text-rose-300">
                        <ShieldAlert className="size-3.5" />
                        Matched Routing Rules ({log.matched_rules.length})
                      </div>
                      <div className="flex flex-wrap gap-1.5">
                        {log.matched_rules.map((rule) => (
                          <Badge
                            key={rule.id}
                            variant="destructive"
                            className="font-mono text-[11px]"
                          >
                            {rule.name} ({rule.severity})
                          </Badge>
                        ))}
                      </div>
                    </div>
                  )}

                  {log.notifications && log.notifications.length > 0 && (
                    <div className="space-y-1.5">
                      <h4 className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
                        Notification Routing ({log.notifications.length})
                      </h4>
                      <div className="space-y-1.5">
                        {log.notifications.map((notif, idx) => (
                          <div
                            key={`${notif.rule_id}-${notif.channel}-${idx}`}
                            className="flex items-start justify-between gap-2 rounded-lg border bg-card px-3 py-2 text-xs"
                          >
                            <div className="min-w-0 space-y-0.5">
                              <div className="flex items-center gap-1.5 font-medium">
                                {notif.channel === "email" ? (
                                  <Mail className="size-3.5 text-sky-500" />
                                ) : (
                                  <MessageSquare className="size-3.5 text-violet-500" />
                                )}
                                <span className="uppercase">{notif.channel}</span>
                                <span className="font-mono text-muted-foreground">
                                  → {notif.target}
                                </span>
                                {notif.working_hours_only && (
                                  <Badge
                                    variant="outline"
                                    className="gap-1 font-mono text-[10px]"
                                  >
                                    <Clock className="size-2.5" />
                                    Work Hours
                                  </Badge>
                                )}
                              </div>
                              <p className="text-[11px] text-muted-foreground">
                                {notif.reason}
                              </p>
                            </div>
                            <Badge
                              variant={
                                notif.status === "suppressed"
                                  ? "secondary"
                                  : notif.status === "failed"
                                    ? "destructive"
                                    : "outline"
                              }
                              className={
                                notif.status === "routed" || notif.status === "sent"
                                  ? "border-emerald-500/35 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 font-mono text-[10px] uppercase"
                                  : "font-mono text-[10px] uppercase"
                              }
                            >
                              {notif.status}
                            </Badge>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  <div className="space-y-1.5">
                    <h4 className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
                      Normalized Message
                    </h4>
                    <div className="rounded-lg border bg-muted/40 p-3 font-mono text-xs leading-relaxed break-words">
                      {log.message || "—"}
                    </div>
                  </div>

                  <div className="space-y-1.5">
                    <h4 className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
                      Extracted Metadata ({metadataEntries.length})
                    </h4>
                    {metadataEntries.length === 0 ? (
                      <p className="text-xs text-muted-foreground">
                        No extra key-value fields extracted.
                      </p>
                    ) : (
                      <div className="overflow-hidden rounded-lg border">
                        <table className="w-full text-xs">
                          <tbody className="divide-y">
                            {metadataEntries.map(([key, val]) => (
                              <tr key={key} className="bg-card">
                                <td className="w-1/3 bg-muted/40 px-3 py-2 font-mono font-medium text-muted-foreground">
                                  {key}
                                </td>
                                <td className="px-3 py-2 font-mono text-foreground break-all">
                                  {typeof val === "object" && val !== null
                                    ? JSON.stringify(val)
                                    : String(val)}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </div>

                  <Separator />

                  <div className="space-y-1.5">
                    <div className="flex items-center justify-between">
                      <h4 className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
                        Original Raw Log
                      </h4>
                      <Button
                        variant="ghost"
                        size="xs"
                        onClick={() => handleCopy("raw", log.raw)}
                        className="gap-1 text-xs"
                      >
                        {copied === "raw" ? (
                          <>
                            <Check className="size-3 text-emerald-500" />
                            Copied
                          </>
                        ) : (
                          <>
                            <Copy className="size-3" />
                            Copy
                          </>
                        )}
                      </Button>
                    </div>
                    <pre className="max-h-56 overflow-auto rounded-lg border bg-muted/60 p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap break-all">
                      {log.raw}
                    </pre>
                  </div>
                </TabsContent>

                <TabsContent value="json" className="mt-4 space-y-2">
                  <div className="flex justify-end">
                    <Button
                      variant="outline"
                      size="xs"
                      onClick={() =>
                        handleCopy(
                          "json",
                          JSON.stringify(
                            {
                              timestamp: log.timestamp,
                              level: log.level,
                              service: log.service,
                              message: log.message,
                              summary: log.summary,
                              metadata: log.metadata,
                              raw: log.raw,
                            },
                            null,
                            2
                          )
                        )
                      }
                      className="gap-1 text-xs"
                    >
                      {copied === "json" ? (
                        <>
                          <Check className="size-3 text-emerald-500" />
                          Copied JSON
                        </>
                      ) : (
                        <>
                          <Copy className="size-3" />
                          Copy JSON
                        </>
                      )}
                    </Button>
                  </div>
                  <pre className="overflow-auto rounded-lg border bg-muted/60 p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap break-all">
                    {JSON.stringify(
                      {
                        timestamp: log.timestamp,
                        level: log.level,
                        service: log.service,
                        message: log.message,
                        summary: log.summary,
                        metadata: log.metadata,
                        raw: log.raw,
                      },
                      null,
                      2
                    )}
                  </pre>
                </TabsContent>
              </Tabs>
            </ScrollArea>
          </>
        )}
      </SheetContent>
    </Sheet>
  );
}
