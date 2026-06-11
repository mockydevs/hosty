import { cn } from "@/lib/utils";

interface ProgressProps {
  value: number; // 0..100
  className?: string;
  "aria-label"?: string;
}

export function Progress({ value, className, ...props }: ProgressProps) {
  const clamped = Math.max(0, Math.min(100, value));
  const tone = clamped >= 90 ? "bg-destructive" : clamped >= 75 ? "bg-yellow-500" : "bg-primary";
  return (
    // biome-ignore lint/a11y/useFocusableInteractive: progressbar is a read-only indicator; it must not be focusable.
    <div
      role="progressbar"
      aria-valuenow={Math.round(clamped)}
      aria-valuemin={0}
      aria-valuemax={100}
      className={cn("h-2 w-full overflow-hidden rounded-full bg-secondary", className)}
      {...props}
    >
      <div className={cn("h-full transition-all", tone)} style={{ width: `${clamped}%` }} />
    </div>
  );
}
