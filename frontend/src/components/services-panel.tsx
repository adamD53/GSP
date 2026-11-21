"use client";

import { useState } from "react";
import {
  Layers,
  PlusCircle,
  Send,
  Server,
  Trash2,
} from "lucide-react";
import { AddServiceDialog } from "@/components/add-service-dialog";
import { ServiceIngestDialog } from "@/components/service-ingest-dialog";
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
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import type { ParsedLogEntry, RegisteredService } from "@/lib/types";
import { cn } from "@/lib/utils";

interface ServicesPanelProps {
  services: RegisteredService[];
  logs: ParsedLogEntry[];
  selectedService: string | null;
  onSelectService: (name: string | null) => void;
  onAddService: (service: Omit<RegisteredService, "id" | "createdAt">) => void;
  onDeleteService: (id: string) => void;
  onLogSent: () => void;
}

export function ServicesPanel({
  services,
  logs,
  selectedService,
  onSelectService,
  onAddService,
  onDeleteService,
  onLogSent,
}: ServicesPanelProps) {
  const [ingestTarget, setIngestTarget] = useState<RegisteredService | null>(
    null
  );

  // Count logs & errors per service name (case-insensitive key)
  const statsByService = new Map<
    string,
    { displayName: string; total: number; errors: number }
  >();

  for (const log of logs) {
    if (!log.service) continue;
    const key = log.service.toLowerCase();
    const prev = statsByService.get(key) ?? {
      displayName: log.service,
      total: 0,
      errors: 0,
    };
    prev.total += 1;
    if (log.level === "ERROR" || log.level === "FATAL") {
      prev.errors += 1;
    }
    statsByService.set(key, prev);
  }

  const registeredLower = new Set(services.map((s) => s.name.toLowerCase()));
  const discoveredServices = Array.from(statsByService.entries())
    .filter(([key]) => !registeredLower.has(key))
    .map(([, val]) => val)
    .sort((a, b) => b.total - a.total);

  return (
    <>
      <Card className="flex h-full flex-col">
        <CardHeader className="border-b pb-3">
          <div className="flex items-center gap-2">
            <Server className="size-4 text-primary" />
            <CardTitle>Services</CardTitle>
          </div>
          <CardDescription>
            Register sources, filter logs, or send test events
          </CardDescription>
          <CardAction>
            <AddServiceDialog
              existingNames={services.map((s) => s.name)}
              onAddService={onAddService}
            />
          </CardAction>
        </CardHeader>

        <CardContent className="flex flex-1 flex-col gap-3 pt-3 overflow-hidden">
          <button
            type="button"
            onClick={() => onSelectService(null)}
            className={cn(
              "flex w-full items-center justify-between rounded-lg border px-3 py-2 text-left text-sm transition-colors",
              selectedService === null
                ? "border-primary/40 bg-primary/10 font-medium text-foreground"
                : "border-transparent bg-muted/40 text-muted-foreground hover:bg-muted/80 hover:text-foreground"
            )}
          >
            <span className="flex items-center gap-2">
              <Layers className="size-4" />
              All Services
            </span>
            <Badge variant="secondary" className="font-mono text-[11px]">
              {logs.length}
            </Badge>
          </button>

          <ScrollArea className="flex-1 pr-2">
            <div className="space-y-2">
              {services.length === 0 ? (
                <div className="rounded-lg border border-dashed p-5 text-center">
                  <Server className="mx-auto mb-2 size-6 text-muted-foreground/60" />
                  <p className="text-xs font-medium text-foreground">
                    No services registered yet
                  </p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    Click &ldquo;Add Service&rdquo; to register a log source and
                    test ingestion.
                  </p>
                </div>
              ) : (
                services.map((svc) => {
                  const stats = statsByService.get(svc.name.toLowerCase());
                  const isSelected =
                    selectedService?.toLowerCase() === svc.name.toLowerCase();

                  return (
                    <div
                      key={svc.id}
                      className={cn(
                        "group rounded-lg border p-2.5 transition-colors",
                        isSelected
                          ? "border-primary/50 bg-primary/5"
                          : "border-border/80 bg-card hover:border-border hover:bg-muted/40"
                      )}
                    >
                      <div className="flex items-start justify-between gap-2">
                        <button
                          type="button"
                          onClick={() =>
                            onSelectService(isSelected ? null : svc.name)
                          }
                          className="min-w-0 flex-1 text-left"
                        >
                          <div className="flex items-center gap-1.5">
                            <span className="truncate text-sm font-medium text-foreground">
                              {svc.name}
                            </span>
                            <Badge
                              variant="outline"
                              className="shrink-0 font-mono text-[10px] uppercase"
                            >
                              {svc.format}
                            </Badge>
                          </div>
                          {svc.description && (
                            <p className="mt-0.5 line-clamp-1 text-xs text-muted-foreground">
                              {svc.description}
                            </p>
                          )}
                          <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[11px] text-muted-foreground">
                            <span className="font-mono">{svc.topic}</span>
                            <span>•</span>
                            <span className="tabular-nums">
                              {stats?.total ?? 0} logs
                            </span>
                            {stats && stats.errors > 0 && (
                              <Badge
                                variant="destructive"
                                className="h-4 px-1.5 font-mono text-[10px]"
                              >
                                {stats.errors} err
                              </Badge>
                            )}
                          </div>
                        </button>

                        <div className="flex shrink-0 items-center gap-0.5">
                          <Tooltip>
                            <TooltipTrigger
                              render={
                                <Button
                                  variant="ghost"
                                  size="icon-xs"
                                  onClick={() => setIngestTarget(svc)}
                                  aria-label={`Send test log for ${svc.name}`}
                                />
                              }
                            >
                              <Send className="size-3.5" />
                            </TooltipTrigger>
                            <TooltipContent>
                              Send test log / snippets
                            </TooltipContent>
                          </Tooltip>

                          <Tooltip>
                            <TooltipTrigger
                              render={
                                <Button
                                  variant="ghost"
                                  size="icon-xs"
                                  onClick={() => {
                                    if (isSelected) onSelectService(null);
                                    onDeleteService(svc.id);
                                  }}
                                  aria-label={`Remove ${svc.name}`}
                                  className="text-muted-foreground hover:text-destructive"
                                />
                              }
                            >
                              <Trash2 className="size-3.5" />
                            </TooltipTrigger>
                            <TooltipContent>Remove service</TooltipContent>
                          </Tooltip>
                        </div>
                      </div>
                    </div>
                  );
                })
              )}

              {discoveredServices.length > 0 && (
                <>
                  <Separator className="my-3" />
                  <div className="space-y-1.5">
                    <p className="px-1 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
                      Discovered from LLM Logs
                    </p>
                    {discoveredServices.map((disc) => {
                      const isSelected =
                        selectedService?.toLowerCase() ===
                        disc.displayName.toLowerCase();
                      return (
                        <div
                          key={disc.displayName}
                          className={cn(
                            "flex items-center justify-between gap-2 rounded-lg border border-dashed px-2.5 py-2 text-xs transition-colors",
                            isSelected
                              ? "border-primary/50 bg-primary/5"
                              : "border-border/70 hover:bg-muted/40"
                          )}
                        >
                          <button
                            type="button"
                            onClick={() =>
                              onSelectService(
                                isSelected ? null : disc.displayName
                              )
                            }
                            className="flex min-w-0 flex-1 items-center gap-2 text-left"
                          >
                            <span className="truncate font-mono font-medium text-foreground">
                              {disc.displayName}
                            </span>
                            <Badge
                              variant="secondary"
                              className="font-mono text-[10px]"
                            >
                              {disc.total}
                            </Badge>
                          </button>

                          <Button
                            variant="ghost"
                            size="xs"
                            className="gap-1 text-xs"
                            onClick={() =>
                              onAddService({
                                name: disc.displayName,
                                description: "Auto-discovered from parsed logs",
                                topic: "raw-logs",
                                format: "json",
                              })
                            }
                          >
                            <PlusCircle className="size-3" />
                            Track
                          </Button>
                        </div>
                      );
                    })}
                  </div>
                </>
              )}
            </div>
          </ScrollArea>
        </CardContent>
      </Card>

      <ServiceIngestDialog
        service={ingestTarget}
        onClose={() => setIngestTarget(null)}
        onLogSent={onLogSent}
      />
    </>
  );
}
