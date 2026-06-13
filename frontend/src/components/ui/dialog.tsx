import { cn } from "@/lib/utils";
/**
 * Modal dialog built on the native <dialog> element: focus trapping, Escape,
 * and backdrop dismissal come from the platform — no extra dependency.
 */
import { type HTMLAttributes, type MouseEvent, type ReactNode, useEffect, useRef } from "react";

interface DialogProps {
  open: boolean;
  onClose: () => void;
  children: ReactNode;
  className?: string;
}

export function Dialog({ open, onClose, children, className }: DialogProps) {
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (open && !el.open) el.showModal();
    if (!open && el.open) el.close();
  }, [open]);

  // Click on the backdrop (outside the panel) dismisses.
  const onBackdropClick = (e: MouseEvent<HTMLDialogElement>) => {
    if (e.target === ref.current) onClose();
  };

  return (
    // biome-ignore lint/a11y/useKeyWithClickEvents: Escape is handled natively by <dialog> via onCancel.
    <dialog
      ref={ref}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
      onClick={onBackdropClick}
      className={cn(
        "m-auto w-full max-w-md rounded-lg border border-border bg-card p-0 text-card-foreground shadow-lg",
        "backdrop:bg-black/50",
        className,
      )}
    >
      {children}
    </dialog>
  );
}

export function DialogContent({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("flex flex-col gap-3 p-6", className)} {...props} />;
}

export function DialogTitle({ className, ...props }: HTMLAttributes<HTMLHeadingElement>) {
  return <h2 className={cn("text-lg font-semibold leading-none", className)} {...props} />;
}

export function DialogDescription({ className, ...props }: HTMLAttributes<HTMLParagraphElement>) {
  return <p className={cn("text-sm text-muted-foreground", className)} {...props} />;
}

export function DialogActions({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("mt-2 flex justify-end gap-2", className)} {...props} />;
}
