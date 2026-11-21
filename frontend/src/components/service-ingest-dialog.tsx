"use client";

import { useEffect, useState } from "react";
import {
  Check,
  Copy,
  RefreshCw,
  Send,
  Terminal,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import {
  buildCurlSnippet,
  buildKafkaSnippet,
  buildSampleLogPayload,
  getContentTypeForFormat,
} from "@/lib/log-templates";
import {
  SERVICE_FORMATS,
  type RegisteredService,
  type ServiceFormat,
} from "@/lib/types";

interface ServiceIngestDialogProps {
  service: RegisteredService | null;
  onClose: () => void;
  onLogSent: () => void;
}

export function ServiceIngestDialog({
  service,
  onClose,
  onLogSent,
}: ServiceIngestDialogProps) {
  const [format, setFormat] = useState<ServiceFormat>("json");
  const [payload, setPayload] = useState("");
  const [sending, setSending] = useState(false);
  const [feedback, setFeedback] = useState<{
    kind: "success" | "error";
    text: string;
  } | null>(null);
  const [copiedKey, setCopiedKey] = useState<string | null>(null);

  useEffect(() => {
    if (service) {
      setFormat(service.format);
      setPayload(buildSampleLogPayload(service.name, service.format));
      setFeedback(null);
    }
  }, [service]);

  if (!service) return null;

  function handleFormatChange(next: ServiceFormat) {
    setFormat(next);
    if (service) {
      setPayload(buildSampleLogPayload(service.name, next));
    }
    setFeedback(null);
  }

  function handleRegenerate() {
    if (!service) return;
    setPayload(buildSampleLogPayload(service.name, format));
    setFeedback(null);
  }

  async function handleSend() {
    if (!service || !payload.trim()) return;
    setSending(true);
    setFeedback(null);

    try {
      const qs = service.topic
        ? `?topic=${encodeURIComponent(service.topic)}`
        : "";
      const res = await fetch(`/api/logs${qs}`, {
        method: "POST",
        headers: { "Content-Type": getContentTypeForFormat(format) },
        body: payload,
      });

      if (!res.ok) {
        const errText = await res.text();
        setFeedback({
          kind: "error",
          text: `Failed (${res.status}): ${errText.slice(0, 140)}`,
        });
      } else {
        setFeedback({
          kind: "success",
          text: `Ingested into "${service.topic}". Log will appear in the stream shortly.`,
        });
        onLogSent();
      }
    } catch (err) {
      setFeedback({
        kind: "error",
        text: err instanceof Error ? err.message : "Network error sending log",
      });
    } finally {
      setSending(false);
    }
  }

  async function handleCopy(key: string, text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedKey(key);
      setTimeout(() => setCopiedKey(null), 1800);
    } catch {
      // Clipboard unavailable
    }
  }

  const curlSnippet = buildCurlSnippet(
    service.name,
    service.topic,
    format,
    payload
  );
  const kafkaSnippet = buildKafkaSnippet(service.topic, payload);

  return (
    <Dialog open={Boolean(service)} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <div className="flex items-center gap-2">
            <Terminal className="size-4 text-primary" />
            <DialogTitle>{service.name}</DialogTitle>
            <Badge variant="secondary" className="font-mono text-[11px]">
              {service.topic}
            </Badge>
          </div>
          <DialogDescription>
            {service.description ||
              "Send a test log directly to the receiver or copy an ingestion snippet."}
          </DialogDescription>
        </DialogHeader>

        <Tabs defaultValue="send" className="w-full">
          <TabsList className="w-full">
            <TabsTrigger value="send">Send Test Log</TabsTrigger>
            <TabsTrigger value="snippets">HTTP &amp; Kafka Snippets</TabsTrigger>
          </TabsList>

          <TabsContent value="send" className="mt-3 space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <Label htmlFor="ingest-format" className="text-xs">
                  Format
                </Label>
                <Select
                  value={format}
                  onValueChange={(val) => {
                    if (val) handleFormatChange(val as ServiceFormat);
                  }}
                >
                  <SelectTrigger id="ingest-format" size="sm">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {SERVICE_FORMATS.map((item) => (
                      <SelectItem key={item.value} value={item.value}>
                        {item.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={handleRegenerate}
                className="gap-1.5"
              >
                <RefreshCw className="size-3.5" />
                Fresh Sample
              </Button>
            </div>

            <Textarea
              value={payload}
              onChange={(e) => setPayload(e.target.value)}
              rows={8}
              className="font-mono text-xs leading-relaxed"
              placeholder="Paste or edit raw log payload..."
            />

            {feedback && (
              <div
                className={`rounded-lg border px-3 py-2 text-xs ${
                  feedback.kind === "success"
                    ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
                    : "border-rose-500/30 bg-rose-500/10 text-rose-700 dark:text-rose-300"
                }`}
              >
                {feedback.text}
              </div>
            )}

            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={onClose}>
                Close
              </Button>
              <Button
                onClick={handleSend}
                disabled={sending || !payload.trim()}
                className="gap-1.5"
              >
                <Send className="size-3.5" />
                {sending ? "Sending..." : "Send to /logs"}
              </Button>
            </div>
          </TabsContent>

          <TabsContent value="snippets" className="mt-3 space-y-4">
            <div className="space-y-1.5">
              <div className="flex items-center justify-between">
                <Label className="text-xs font-medium">
                  HTTP Ingestion (curl)
                </Label>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => handleCopy("curl", curlSnippet)}
                  className="h-7 gap-1 text-xs"
                >
                  {copiedKey === "curl" ? (
                    <>
                      <Check className="size-3.5 text-emerald-500" />
                      Copied
                    </>
                  ) : (
                    <>
                      <Copy className="size-3.5" />
                      Copy
                    </>
                  )}
                </Button>
              </div>
              <pre className="max-h-44 overflow-auto rounded-lg border bg-muted/60 p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap break-all">
                {curlSnippet}
              </pre>
            </div>

            <div className="space-y-1.5">
              <div className="flex items-center justify-between">
                <Label className="text-xs font-medium">
                  Kafka / Redpanda Produce (rpk)
                </Label>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => handleCopy("kafka", kafkaSnippet)}
                  className="h-7 gap-1 text-xs"
                >
                  {copiedKey === "kafka" ? (
                    <>
                      <Check className="size-3.5 text-emerald-500" />
                      Copied
                    </>
                  ) : (
                    <>
                      <Copy className="size-3.5" />
                      Copy
                    </>
                  )}
                </Button>
              </div>
              <pre className="max-h-36 overflow-auto rounded-lg border bg-muted/60 p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap break-all">
                {kafkaSnippet}
              </pre>
            </div>
          </TabsContent>
        </Tabs>
      </DialogContent>
    </Dialog>
  );
}
