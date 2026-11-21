"use client";

import {
  Activity,
  Moon,
  Radio,
  RefreshCw,
  Sun,
  TerminalSquare,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";

export type HealthState = "checking" | "ok" | "degraded" | "offline";

interface DashboardHeaderProps {
  health: HealthState;
  backendUrl: string;
  isStreaming: boolean;
  streamConnected: boolean;
  isRefreshing: boolean;
  darkMode: boolean;
  onToggleStream: (next: boolean) => void;
  onRefresh: () => void;
  onToggleDarkMode: () => void;
}

export function DashboardHeader({
  health,
  backendUrl,
  isStreaming,
  streamConnected,
  isRefreshing,
  darkMode,
  onToggleStream,
  onRefresh,
  onToggleDarkMode,
}: DashboardHeaderProps) {
  const healthConfig: Record<
    HealthState,
    { label: string; dotClass: string; badgeClass: string }
  > = {
    checking: {
      label: "Checking API",
      dotClass: "bg-zinc-400",
      badgeClass: "border-border text-muted-foreground",
    },
    ok: {
      label: "API Online",
      dotClass: "bg-emerald-500",
      badgeClass:
        "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
    },
    degraded: {
      label: "API Degraded",
      dotClass: "bg-amber-500",
      badgeClass:
        "border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300",
    },
    offline: {
      label: "API Offline",
      dotClass: "bg-rose-500",
      badgeClass:
        "border-rose-500/30 bg-rose-500/10 text-rose-700 dark:text-rose-300",
    },
  };

  const currentHealth = healthConfig[health];

  return (
    <header className="sticky top-0 z-30 border-b bg-background/85 backdrop-blur-md">
      <div className="mx-auto flex max-w-[1600px] flex-wrap items-center justify-between gap-3 px-4 py-3 sm:px-6">
        <div className="flex items-center gap-3">
          <div className="flex size-9 items-center justify-center rounded-lg bg-primary text-primary-foreground shadow-xs">
            <TerminalSquare className="size-5" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-base font-semibold tracking-tight sm:text-lg">
                GSP Log Observatory
              </h1>
              <Tooltip>
                <TooltipTrigger
                  render={
                    <Badge
                      variant="outline"
                      className={`gap-1.5 font-mono text-[11px] ${currentHealth.badgeClass}`}
                    />
                  }
                >
                  <span
                    className={`size-2 rounded-full ${currentHealth.dotClass}`}
                  />
                  {currentHealth.label}
                </TooltipTrigger>
                <TooltipContent>
                  <p className="font-mono text-xs">{backendUrl || "Proxy /api"}</p>
                </TooltipContent>
              </Tooltip>
            </div>
            <p className="text-xs text-muted-foreground">
              Real-time log stream, alert routing &amp; incident management
            </p>
          </div>
        </div>

        <div className="flex items-center gap-2 sm:gap-3">
          <div className="flex items-center gap-2 rounded-lg border bg-card px-3 py-1.5">
            <Radio
              className={`size-3.5 ${
                isStreaming && streamConnected
                  ? "animate-pulse text-emerald-500"
                  : isStreaming
                    ? "text-amber-500"
                    : "text-muted-foreground"
              }`}
            />
            <Label
              htmlFor="live-stream-toggle"
              className="cursor-pointer text-xs font-medium"
            >
              Live Stream
            </Label>
            <Switch
              id="live-stream-toggle"
              checked={isStreaming}
              onCheckedChange={(checked) => onToggleStream(Boolean(checked))}
              size="sm"
            />
          </div>

          <Button
            variant="outline"
            size="sm"
            onClick={onRefresh}
            disabled={isRefreshing}
            className="gap-1.5"
          >
            <RefreshCw
              className={`size-3.5 ${isRefreshing ? "animate-spin" : ""}`}
            />
            <span className="hidden sm:inline">Refresh</span>
          </Button>

          <Button
            variant="outline"
            size="icon-sm"
            onClick={onToggleDarkMode}
            aria-label="Toggle color theme"
          >
            {darkMode ? (
              <Sun className="size-4" />
            ) : (
              <Moon className="size-4" />
            )}
          </Button>
        </div>
      </div>

      {health === "offline" && (
        <div className="border-t border-rose-500/20 bg-rose-500/10 px-4 py-1.5 text-center text-xs text-rose-700 dark:text-rose-300">
          <Activity className="mr-1.5 inline size-3.5 align-text-bottom" />
          Backend API at{" "}
          <code className="font-mono font-semibold">{backendUrl}</code> is
          unreachable. Start the stack with{" "}
          <code className="font-mono">docker compose up -d</code> or check{" "}
          <code className="font-mono">API_BASE_URL</code>.
        </div>
      )}
    </header>
  );
}
