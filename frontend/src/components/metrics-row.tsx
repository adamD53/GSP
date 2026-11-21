"use client";

import {
  AlertTriangle,
  Boxes,
  FileWarning,
  Sparkles,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

interface MetricsRowProps {
  totalParsed: number;
  errorCount: number;
  registeredServiceCount: number;
  discoveredServiceCount: number;
  deadLetterCount: number;
  streamConnected: boolean;
}

export function MetricsRow({
  totalParsed,
  errorCount,
  registeredServiceCount,
  discoveredServiceCount,
  deadLetterCount,
  streamConnected,
}: MetricsRowProps) {
  const errorRate =
    totalParsed > 0 ? Math.round((errorCount / totalParsed) * 100) : 0;

  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      <Card size="sm">
        <CardHeader className="flex flex-row items-center justify-between pb-1">
          <CardTitle className="text-xs font-medium text-muted-foreground">
            LLM Parsed Logs
          </CardTitle>
          <Sparkles className="size-4 text-sky-500" />
        </CardHeader>
        <CardContent>
          <div className="text-2xl font-bold tracking-tight tabular-nums">
            {totalParsed}
          </div>
          <p className="mt-0.5 text-xs text-muted-foreground">
            {streamConnected ? "Streaming live NDJSON" : "Snapshot mode"}
          </p>
        </CardContent>
      </Card>

      <Card size="sm">
        <CardHeader className="flex flex-row items-center justify-between pb-1">
          <CardTitle className="text-xs font-medium text-muted-foreground">
            Errors &amp; Fatals
          </CardTitle>
          <AlertTriangle
            className={`size-4 ${
              errorCount > 0 ? "text-rose-500" : "text-muted-foreground"
            }`}
          />
        </CardHeader>
        <CardContent>
          <div className="flex items-baseline gap-2">
            <span className="text-2xl font-bold tracking-tight tabular-nums">
              {errorCount}
            </span>
            <span className="text-xs font-medium text-muted-foreground">
              ({errorRate}%)
            </span>
          </div>
          <p className="mt-0.5 text-xs text-muted-foreground">
            ERROR &amp; FATAL severity
          </p>
        </CardContent>
      </Card>

      <Card size="sm">
        <CardHeader className="flex flex-row items-center justify-between pb-1">
          <CardTitle className="text-xs font-medium text-muted-foreground">
            Services
          </CardTitle>
          <Boxes className="size-4 text-violet-500" />
        </CardHeader>
        <CardContent>
          <div className="text-2xl font-bold tracking-tight tabular-nums">
            {registeredServiceCount}
          </div>
          <p className="mt-0.5 text-xs text-muted-foreground">
            {discoveredServiceCount > 0
              ? `+${discoveredServiceCount} auto-discovered`
              : "Registered in panel"}
          </p>
        </CardContent>
      </Card>

      <Card size="sm">
        <CardHeader className="flex flex-row items-center justify-between pb-1">
          <CardTitle className="text-xs font-medium text-muted-foreground">
            Dead-Letter Queue
          </CardTitle>
          <FileWarning
            className={`size-4 ${
              deadLetterCount > 0 ? "text-amber-500" : "text-muted-foreground"
            }`}
          />
        </CardHeader>
        <CardContent>
          <div className="text-2xl font-bold tracking-tight tabular-nums">
            {deadLetterCount}
          </div>
          <p className="mt-0.5 text-xs text-muted-foreground">
            {deadLetterCount > 0 ? "Needs parser review" : "All logs parsed cleanly"}
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
