import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { CircleAlert, Inbox, Loader2 } from "lucide-react";
import type { ReactNode } from "react";

export function LoadingState({
  label = "Loading…",
  full = false,
}: { label?: string; full?: boolean }) {
  return (
    <output
      className={cn(
        "flex flex-col items-center justify-center gap-3 text-muted-foreground",
        full ? "min-h-screen" : "py-12",
      )}
    >
      <Loader2 className="h-6 w-6 animate-spin" aria-hidden />
      <span className="text-sm">{label}</span>
    </output>
  );
}

export function ErrorState({
  message = "Something went wrong.",
  onRetry,
}: {
  message?: string;
  onRetry?: () => void;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 py-12 text-center">
      <CircleAlert className="h-8 w-8 text-destructive" aria-hidden />
      <p className="text-sm text-muted-foreground">{message}</p>
      {onRetry && (
        <Button variant="outline" size="sm" onClick={onRetry}>
          Try again
        </Button>
      )}
    </div>
  );
}

export function EmptyState({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border py-16 text-center">
      <Inbox className="h-8 w-8 text-muted-foreground" aria-hidden />
      <h3 className="font-medium">{title}</h3>
      {description && <p className="max-w-sm text-sm text-muted-foreground">{description}</p>}
      {children}
    </div>
  );
}
