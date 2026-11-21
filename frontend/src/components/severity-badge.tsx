"use client";

import { Badge } from "@/components/ui/badge";
import type { LogLevel } from "@/lib/types";
import { cn } from "@/lib/utils";

const LEVEL_STYLES: Record<LogLevel, string> = {
  FATAL:
    "border-red-500/40 bg-red-500/15 text-red-700 dark:text-red-300 font-semibold",
  ERROR:
    "border-rose-500/35 bg-rose-500/10 text-rose-700 dark:text-rose-300 font-medium",
  WARN:
    "border-amber-500/35 bg-amber-500/10 text-amber-700 dark:text-amber-300 font-medium",
  INFO:
    "border-sky-500/30 bg-sky-500/10 text-sky-700 dark:text-sky-300 font-medium",
  DEBUG:
    "border-zinc-500/30 bg-zinc-500/10 text-zinc-600 dark:text-zinc-400",
  UNKNOWN:
    "border-border bg-muted text-muted-foreground",
};

export function SeverityBadge({
  level,
  className,
}: {
  level: LogLevel;
  className?: string;
}) {
  const normalized = (level || "UNKNOWN").toUpperCase() as LogLevel;
  return (
    <Badge
      variant="outline"
      className={cn(
        "font-mono text-[11px] tracking-wide px-2 py-0.5",
        LEVEL_STYLES[normalized] ?? LEVEL_STYLES.UNKNOWN,
        className
      )}
    >
      {normalized}
    </Badge>
  );
}
