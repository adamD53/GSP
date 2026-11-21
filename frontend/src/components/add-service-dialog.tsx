"use client";

import { useState } from "react";
import { Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
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
import {
  SERVICE_FORMATS,
  type RegisteredService,
  type ServiceFormat,
} from "@/lib/types";

interface AddServiceDialogProps {
  existingNames: string[];
  onAddService: (
    service: Omit<RegisteredService, "id" | "createdAt">
  ) => void;
}

export function AddServiceDialog({
  existingNames,
  onAddService,
}: AddServiceDialogProps) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [topic, setTopic] = useState("raw-logs");
  const [format, setFormat] = useState<ServiceFormat>("json");
  const [error, setError] = useState<string | null>(null);

  function resetForm() {
    setName("");
    setDescription("");
    setTopic("raw-logs");
    setFormat("json");
    setError(null);
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const cleaned = name.trim();
    if (!cleaned) {
      setError("Service name is required.");
      return;
    }
    if (
      existingNames.some(
        (existing) => existing.toLowerCase() === cleaned.toLowerCase()
      )
    ) {
      setError(`Service "${cleaned}" is already registered.`);
      return;
    }

    onAddService({
      name: cleaned,
      description: description.trim(),
      topic: topic.trim() || "raw-logs",
      format,
    });
    resetForm();
    setOpen(false);
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) resetForm();
      }}
    >
      <DialogTrigger render={<Button size="sm" className="gap-1.5" />}>
        <Plus className="size-3.5" />
        Add Service
      </DialogTrigger>

      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Register Service</DialogTitle>
          <DialogDescription>
            Add a service to track its logs, generate ingestion snippets, and
            send test events to Redpanda.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="service-name">Service Name</Label>
            <Input
              id="service-name"
              placeholder="e.g. checkout-api, billing-worker"
              value={name}
              onChange={(e) => {
                setName(e.target.value);
                setError(null);
              }}
              autoFocus
            />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label htmlFor="service-topic">Kafka Topic</Label>
              <Input
                id="service-topic"
                placeholder="raw-logs"
                value={topic}
                onChange={(e) => setTopic(e.target.value)}
                className="font-mono text-xs"
              />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="service-format">Log Format</Label>
              <Select
                value={format}
                onValueChange={(val) => {
                  if (val) setFormat(val as ServiceFormat);
                }}
              >
                <SelectTrigger id="service-format" className="w-full">
                  <SelectValue placeholder="Format" />
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
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="service-desc">Description (optional)</Label>
            <Textarea
              id="service-desc"
              placeholder="Short note about what this service emits..."
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={2}
            />
          </div>

          {error && (
            <p className="text-xs font-medium text-destructive">{error}</p>
          )}

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setOpen(false)}
            >
              Cancel
            </Button>
            <Button type="submit">Add Service</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
