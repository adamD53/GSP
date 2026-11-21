"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  DashboardHeader,
  type HealthState,
} from "@/components/dashboard-header";
import { LLMLogsPanel } from "@/components/llm-logs-panel";
import { MetricsRow } from "@/components/metrics-row";
import { ServicesPanel } from "@/components/services-panel";
import type {
  DeadLetterLog,
  LogLevel,
  ParsedLog,
  ParsedLogEntry,
  RegisteredService,
} from "@/lib/types";

const STORAGE_KEY_SERVICES = "gsp.registered-services.v1";
const STORAGE_KEY_THEME = "gsp.theme.v1";
const MAX_LOGS_IN_MEMORY = 500;

const DEFAULT_SERVICES: RegisteredService[] = [
  {
    id: "svc-checkout-api",
    name: "checkout-api",
    description: "Payment & order checkout JSON logs",
    topic: "raw-logs",
    format: "json",
    createdAt: "2026-01-01T00:00:00Z",
  },
  {
    id: "svc-billing-worker",
    name: "billing-worker",
    description: "Async invoice & billing logfmt events",
    topic: "raw-logs",
    format: "logfmt",
    createdAt: "2026-01-01T00:00:00Z",
  },
];

function normalizeLog(rawObj: Partial<ParsedLog>, idPrefix: string): ParsedLogEntry {
  const validLevels: LogLevel[] = [
    "DEBUG",
    "INFO",
    "WARN",
    "ERROR",
    "FATAL",
    "UNKNOWN",
  ];
  const upper = String(rawObj.level ?? "UNKNOWN").toUpperCase() as LogLevel;
  return {
    timestamp: rawObj.timestamp ?? null,
    level: validLevels.includes(upper) ? upper : "UNKNOWN",
    service: rawObj.service ?? null,
    message: rawObj.message ?? "",
    summary: rawObj.summary ?? rawObj.message ?? "",
    metadata:
      rawObj.metadata && typeof rawObj.metadata === "object"
        ? (rawObj.metadata as Record<string, unknown>)
        : {},
    raw: rawObj.raw ?? "",
    matched_rules: Array.isArray(rawObj.matched_rules)
      ? rawObj.matched_rules
      : [],
    alert: Boolean(
      rawObj.alert ||
        (Array.isArray(rawObj.matched_rules) && rawObj.matched_rules.length > 0)
    ),
    _id: `${idPrefix}-${Math.random().toString(36).slice(2, 10)}`,
    _receivedAt: Date.now(),
  };
}

export function LogDashboard() {
  const [services, setServices] = useState<RegisteredService[]>(DEFAULT_SERVICES);
  const [servicesLoaded, setServicesLoaded] = useState(false);

  const [logs, setLogs] = useState<ParsedLogEntry[]>([]);
  const [deadLetters, setDeadLetters] = useState<DeadLetterLog[]>([]);
  const [rawLogs, setRawLogs] = useState("");
  const [selectedService, setSelectedService] = useState<string | null>(null);

  const [health, setHealth] = useState<HealthState>("checking");
  const [backendUrl, setBackendUrl] = useState("");
  const [isStreaming, setIsStreaming] = useState(true);
  const [streamConnected, setStreamConnected] = useState(false);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [darkMode, setDarkMode] = useState(true);

  const streamSeqRef = useRef(0);

  // Hydrate theme & registered services from localStorage
  useEffect(() => {
    try {
      const savedServices = window.localStorage.getItem(STORAGE_KEY_SERVICES);
      if (savedServices) {
        const parsed = JSON.parse(savedServices);
        if (Array.isArray(parsed)) {
          setServices(parsed);
        }
      }
      const savedTheme = window.localStorage.getItem(STORAGE_KEY_THEME);
      if (savedTheme === "light") {
        setDarkMode(false);
      } else if (savedTheme === "dark") {
        setDarkMode(true);
      }
    } catch {
      // Ignore storage errors
    } finally {
      setServicesLoaded(true);
    }
  }, []);

  useEffect(() => {
    if (!servicesLoaded) return;
    try {
      window.localStorage.setItem(
        STORAGE_KEY_SERVICES,
        JSON.stringify(services)
      );
    } catch {
      // Ignore storage errors
    }
  }, [services, servicesLoaded]);

  useEffect(() => {
    const root = document.documentElement;
    if (darkMode) {
      root.classList.add("dark");
    } else {
      root.classList.remove("dark");
    }
    try {
      window.localStorage.setItem(
        STORAGE_KEY_THEME,
        darkMode ? "dark" : "light"
      );
    } catch {
      // Ignore storage errors
    }
  }, [darkMode]);

  const checkHealth = useCallback(async () => {
    try {
      const res = await fetch("/api/health", { cache: "no-store" });
      const data = await res.json();
      setBackendUrl(data.backendUrl || "");
      if (res.ok && data.status === "ok") {
        setHealth("ok");
      } else if (data.status === "degraded") {
        setHealth("degraded");
      } else {
        setHealth("offline");
      }
    } catch {
      setHealth("offline");
    }
  }, []);

  const fetchSnapshot = useCallback(async () => {
    setIsRefreshing(true);
    try {
      const [parsedRes, dlqRes, rawRes] = await Promise.allSettled([
        fetch("/api/logs/parsed?limit=200&from_beginning=true", {
          cache: "no-store",
        }),
        fetch("/api/logs/dead-letter?limit=100", { cache: "no-store" }),
        fetch("/api/logs?limit=200&from_beginning=true", { cache: "no-store" }),
      ]);

      if (parsedRes.status === "fulfilled" && parsedRes.value.ok) {
        const data = await parsedRes.value.json();
        if (Array.isArray(data)) {
          const normalized = data
            .map((item, idx) => normalizeLog(item, `snap-${idx}`))
            .reverse();
          setLogs(normalized.slice(0, MAX_LOGS_IN_MEMORY));
        }
      }

      if (dlqRes.status === "fulfilled" && dlqRes.value.ok) {
        const data = await dlqRes.value.json();
        if (Array.isArray(data)) {
          setDeadLetters(data.reverse());
        }
      }

      if (rawRes.status === "fulfilled" && rawRes.value.ok) {
        const text = await rawRes.value.text();
        setRawLogs(text);
      }
    } finally {
      setIsRefreshing(false);
    }
  }, []);

  // Initial load + health poll
  useEffect(() => {
    void checkHealth();
    void fetchSnapshot();
    const timer = window.setInterval(() => {
      void checkHealth();
    }, 15000);
    return () => window.clearInterval(timer);
  }, [checkHealth, fetchSnapshot]);

  // Live NDJSON stream for /api/logs/parsed/stream
  useEffect(() => {
    if (!isStreaming) {
      setStreamConnected(false);
      return;
    }

    const controller = new AbortController();
    let cancelled = false;
    let reconnectTimer: number | undefined;

    async function connectStream() {
      try {
        const res = await fetch(
          "/api/logs/parsed/stream?from_beginning=false",
          {
            signal: controller.signal,
            cache: "no-store",
          }
        );

        if (!res.ok || !res.body) {
          setStreamConnected(false);
          if (!cancelled) {
            reconnectTimer = window.setTimeout(connectStream, 5000);
          }
          return;
        }

        setStreamConnected(true);
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (!cancelled) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop() ?? "";

          const incoming: ParsedLogEntry[] = [];
          for (const line of lines) {
            const trimmed = line.trim();
            if (!trimmed) continue;
            try {
              const obj = JSON.parse(trimmed) as Partial<ParsedLog>;
              streamSeqRef.current += 1;
              incoming.push(normalizeLog(obj, `live-${streamSeqRef.current}`));
            } catch {
              // Ignore malformed partial line
            }
          }

          if (incoming.length > 0) {
            setLogs((prev) =>
              [...incoming.reverse(), ...prev].slice(0, MAX_LOGS_IN_MEMORY)
            );
          }
        }
      } catch (err) {
        if (err instanceof Error && err.name === "AbortError") return;
      } finally {
        setStreamConnected(false);
        if (!cancelled) {
          reconnectTimer = window.setTimeout(connectStream, 5000);
        }
      }
    }

    void connectStream();

    return () => {
      cancelled = true;
      controller.abort();
      if (reconnectTimer) window.clearTimeout(reconnectTimer);
    };
  }, [isStreaming]);

  function handleAddService(
    input: Omit<RegisteredService, "id" | "createdAt">
  ) {
    const next: RegisteredService = {
      ...input,
      id: `svc-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
      createdAt: new Date().toISOString(),
    };
    setServices((prev) => [next, ...prev]);
  }

  function handleDeleteService(id: string) {
    setServices((prev) => prev.filter((item) => item.id !== id));
  }

  const errorCount = useMemo(
    () =>
      logs.filter((l) => l.level === "ERROR" || l.level === "FATAL").length,
    [logs]
  );

  const discoveredServiceCount = useMemo(() => {
    const reg = new Set(services.map((s) => s.name.toLowerCase()));
    const seen = new Set<string>();
    for (const log of logs) {
      if (log.service && !reg.has(log.service.toLowerCase())) {
        seen.add(log.service.toLowerCase());
      }
    }
    return seen.size;
  }, [logs, services]);

  return (
    <div className="flex min-h-screen flex-col bg-background text-foreground">
      <DashboardHeader
        health={health}
        backendUrl={backendUrl}
        isStreaming={isStreaming}
        streamConnected={streamConnected}
        isRefreshing={isRefreshing}
        darkMode={darkMode}
        onToggleStream={setIsStreaming}
        onRefresh={() => {
          void checkHealth();
          void fetchSnapshot();
        }}
        onToggleDarkMode={() => setDarkMode((prev) => !prev)}
      />

      <main className="mx-auto flex w-full max-w-[1600px] flex-1 flex-col gap-4 px-4 py-4 sm:px-6 sm:py-5">
        <MetricsRow
          totalParsed={logs.length}
          errorCount={errorCount}
          registeredServiceCount={services.length}
          discoveredServiceCount={discoveredServiceCount}
          deadLetterCount={deadLetters.length}
          streamConnected={streamConnected}
        />

        <div className="grid flex-1 grid-cols-1 gap-4 lg:grid-cols-12">
          <section className="lg:col-span-4 xl:col-span-3">
            <ServicesPanel
              services={services}
              logs={logs}
              selectedService={selectedService}
              onSelectService={setSelectedService}
              onAddService={handleAddService}
              onDeleteService={handleDeleteService}
              onLogSent={() => {
                window.setTimeout(() => {
                  void fetchSnapshot();
                }, 1500);
              }}
            />
          </section>

          <section className="lg:col-span-8 xl:col-span-9">
            <LLMLogsPanel
              logs={logs}
              deadLetters={deadLetters}
              rawLogs={rawLogs}
              selectedService={selectedService}
              onClearServiceFilter={() => setSelectedService(null)}
              onClearLogs={() => setLogs([])}
            />
          </section>
        </div>
      </main>
    </div>
  );
}
