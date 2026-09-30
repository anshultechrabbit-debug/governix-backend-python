import { AlertTriangle, Eye, EyeOff, Inbox, Loader2, MoreHorizontal, X } from "lucide-react";
import {
  useEffect,
  useRef,
  useState,
  type ButtonHTMLAttributes, type ComponentProps, type ReactNode, type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from "react";
import { cn } from "../../lib/format";
import { useAppDispatch, useAppSelector } from "../../store/hooks";
import { dismissToast } from "../../store/toastSlice";

/* ---------- Button ---------- */
type Variant = "primary" | "secondary" | "ghost" | "danger";
const variants: Record<Variant, string> = {
  primary: "bg-brand-600 text-white hover:bg-brand-700 shadow-sm",
  secondary: "bg-surface text-ink border border-line-strong hover:bg-subtle",
  ghost: "text-ink-soft hover:bg-subtle",
  danger: "bg-bad-600 text-white hover:brightness-95",
};

export function Button({
  variant = "primary", size = "md", loading, className, children, disabled, ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; size?: "sm" | "md"; loading?: boolean }) {
  return (
    <button
      className={cn(
        "inline-flex items-center justify-center gap-1.5 rounded-md font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed",
        size === "sm" ? "h-8 px-3 text-sm" : "h-9 px-4 text-sm",
        variants[variant],
        className,
      )}
      disabled={disabled || loading}
      {...props}
    >
      {loading && <Loader2 className="size-4 animate-spin" aria-hidden />}
      {children}
    </button>
  );
}

/* ---------- Form controls ---------- */
const control =
  "w-full rounded-md border border-line-strong bg-surface px-3 text-sm text-ink placeholder:text-muted focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-100";

export function Input({ className, ...props }: ComponentProps<"input">) {
  return <input className={cn(control, "h-9", className)} {...props} />;
}

/** A password field with an eye button to show or hide what was typed. */
export function PasswordInput({ className, ...props }: Omit<ComponentProps<"input">, "type">) {
  const [visible, setVisible] = useState(false);
  return (
    <div className="relative">
      <Input {...props} type={visible ? "text" : "password"} className={cn("pr-9", className)} />
      <button
        type="button"
        onClick={() => setVisible((shown) => !shown)}
        className="absolute inset-y-0 right-0 flex w-9 items-center justify-center rounded-r-md text-muted hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-100"
        aria-label={visible ? "Hide password" : "Show password"}
        aria-pressed={visible}
        title={visible ? "Hide password" : "Show password"}
      >
        {visible ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
      </button>
    </div>
  );
}

export function Textarea({ className, ...props }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={cn(control, "py-2", className)} {...props} />;
}

export function Select({ className, children, ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select className={cn(control, "h-9 pr-8", className)} {...props}>
      {children}
    </select>
  );
}

export function Field({ label, hint, children, htmlFor }: { label: string; hint?: ReactNode; children: ReactNode; htmlFor?: string }) {
  return (
    <div className="space-y-1.5">
      <label htmlFor={htmlFor} className="block text-sm font-medium text-ink-soft">{label}</label>
      {children}
      {hint && <p className="text-xs text-muted">{hint}</p>}
    </div>
  );
}

/* ---------- Surfaces ---------- */
export function Card({ className, children }: { className?: string; children: ReactNode }) {
  return <div className={cn("rounded-lg border border-line bg-surface shadow-[0_1px_2px_rgba(16,24,40,0.04)]", className)}>{children}</div>;
}

export function CardHeader({ title, subtitle, actions }: { title: ReactNode; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-line px-5 py-3.5">
      <div>
        <h2 className="text-sm font-semibold text-ink">{title}</h2>
        {subtitle && <p className="mt-0.5 text-xs text-muted">{subtitle}</p>}
      </div>
      {actions}
    </div>
  );
}

export function PageHeader({ title, subtitle, actions }: { title: ReactNode; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-ink">{title}</h1>
        {subtitle && <p className="mt-1 text-sm text-muted">{subtitle}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}

/* ---------- Badges ---------- */
type Tone = "neutral" | "ok" | "warn" | "bad" | "info" | "brand" | "ai";
const tones: Record<Tone, string> = {
  neutral: "bg-subtle text-ink-soft ring-line-strong",
  ok: "bg-ok-50 text-ok-600 ring-ok-600/20",
  warn: "bg-warn-50 text-warn-600 ring-warn-600/20",
  bad: "bg-bad-50 text-bad-600 ring-bad-600/20",
  info: "bg-info-50 text-info-600 ring-info-600/20",
  brand: "bg-brand-50 text-brand-700 ring-brand-500/20",
  ai: "bg-ai-50 text-ai-600 ring-ai-600/20",
};

export function Badge({ tone = "neutral", children, className }: { tone?: Tone; children: ReactNode; className?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset whitespace-nowrap", tones[tone], className)}>
      {children}
    </span>
  );
}

const STATUS: Record<string, [Tone, string]> = {
  uploaded: ["info", "Uploaded"],
  processing: ["info", "Processing"],
  awaiting_confirmation: ["warn", "Requires review"],
  indexing: ["info", "Indexing"],
  ready: ["ok", "Ready"],
  failed: ["bad", "Failed"],
  rejected: ["neutral", "Rejected"],
  archived: ["neutral", "Archived"],
  active: ["ok", "Active"],
  current: ["ok", "Active"],
  historical: ["neutral", "Archived"],
  scheduled: ["info", "Scheduled"],
  withdrawn: ["bad", "Withdrawn"],
  inactive: ["neutral", "Inactive"],
  suspended: ["bad", "Suspended"],
  pending: ["warn", "Pending review"],
  confirmed: ["ok", "Confirmed"],
};

/** Lifecycle states (Detected / Suggested / Confirmed / Active / Conflict ...). */
export function StatusBadge({ status }: { status: string }) {
  const [tone, label] = STATUS[status] ?? ["neutral", status];
  return <Badge tone={tone}>{label}</Badge>;
}

/** Marks system-generated content so it is never mistaken for confirmed fact. */
export function SuggestedBadge({ label = "Suggested" }: { label?: string }) {
  return <Badge tone="ai">{label}</Badge>;
}

export function Confidence({ value }: { value: number }) {
  const pct = Math.round(value * 100);
  const tone = pct >= 80 ? "bg-ok-600" : pct >= 50 ? "bg-warn-600" : "bg-bad-600";
  return (
    <span className="inline-flex items-center gap-2 text-xs text-ink-soft" title="System confidence (not a confirmation)">
      <span className="h-1.5 w-16 overflow-hidden rounded-full bg-subtle">
        <span className={cn("block h-full rounded-full", tone)} style={{ width: `${pct}%` }} />
      </span>
      {pct}%
    </span>
  );
}

/* ---------- Feedback ---------- */
export function Spinner({ className }: { className?: string }) {
  return <Loader2 className={cn("size-5 animate-spin text-muted", className)} aria-label="Loading" />;
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={cn("animate-pulse rounded-md bg-subtle", className)} />;
}

export function SkeletonRows({ rows = 5 }: { rows?: number }) {
  return (
    <div className="space-y-3 p-5">
      {Array.from({ length: rows }, (_, i) => <Skeleton key={i} className="h-10 w-full" />)}
    </div>
  );
}

export function ProgressBar({ value, tone = "brand" }: { value: number; tone?: "brand" | "ok" | "bad" }) {
  const color = tone === "ok" ? "bg-ok-600" : tone === "bad" ? "bg-bad-600" : "bg-brand-500";
  return (
    <div className="h-2 w-full overflow-hidden rounded-full bg-subtle" role="progressbar" aria-valuenow={Math.round(value)} aria-valuemin={0} aria-valuemax={100}>
      <div className={cn("h-full rounded-full transition-all duration-500", color)} style={{ width: `${Math.min(100, Math.max(0, value))}%` }} />
    </div>
  );
}

export function EmptyState({ title, description, action, icon }: { title: string; description?: ReactNode; action?: ReactNode; icon?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center px-6 py-14 text-center">
      <div className="mb-3 rounded-full bg-subtle p-3 text-muted">{icon ?? <Inbox className="size-6" />}</div>
      <h3 className="text-sm font-semibold text-ink">{title}</h3>
      {description && <p className="mt-1 max-w-md text-sm text-muted">{description}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const message = error instanceof Error ? error.message : "Something went wrong.";
  const requestId = (error as { requestId?: string })?.requestId;
  return (
    <div className="flex items-start gap-3 rounded-lg border border-bad-600/20 bg-bad-50 p-4 text-sm">
      <AlertTriangle className="mt-0.5 size-5 shrink-0 text-bad-600" />
      <div className="flex-1">
        <p className="font-medium text-bad-600">{message}</p>
        {requestId && <p className="mt-1 text-xs text-muted">Reference: {requestId}</p>}
        {onRetry && <Button variant="secondary" size="sm" className="mt-3" onClick={onRetry}>Retry</Button>}
      </div>
    </div>
  );
}

/* ---------- Modal ---------- */
export function Modal({ open, onClose, title, children, footer, wide }: {
  open: boolean; onClose: () => void; title: ReactNode; children: ReactNode; footer?: ReactNode; wide?: boolean;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 p-4" onMouseDown={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        className={cn("max-h-[90vh] w-full overflow-auto rounded-lg bg-surface shadow-xl", wide ? "max-w-3xl" : "max-w-lg")}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-line px-5 py-3.5">
          <h2 className="text-base font-semibold">{title}</h2>
          <button onClick={onClose} className="rounded p-1 text-muted hover:bg-subtle" aria-label="Close"><X className="size-4" /></button>
        </div>
        <div className="px-5 py-4">{children}</div>
        {footer && <div className="flex justify-end gap-2 border-t border-line px-5 py-3">{footer}</div>}
      </div>
    </div>
  );
}

/* ---------- Tabs ---------- */
export function Tabs<T extends string>({ tabs, value, onChange }: { tabs: { id: T; label: string; count?: number }[]; value: T; onChange: (id: T) => void }) {
  return (
    <div className="flex gap-1 border-b border-line" role="tablist">
      {tabs.map((tab) => (
        <button
          key={tab.id}
          role="tab"
          aria-selected={value === tab.id}
          onClick={() => onChange(tab.id)}
          className={cn(
            "-mb-px border-b-2 px-3 py-2 text-sm font-medium transition-colors",
            value === tab.id ? "border-brand-600 text-brand-700" : "border-transparent text-muted hover:text-ink",
          )}
        >
          {tab.label}
          {tab.count !== undefined && <span className="ml-1.5 rounded-full bg-subtle px-1.5 text-xs text-muted">{tab.count}</span>}
        </button>
      ))}
    </div>
  );
}

/* ---------- Table ---------- */
export function Table({ head, children }: { head: ReactNode[]; children: ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="border-b border-line bg-subtle/60 text-xs uppercase tracking-wide text-muted">
          <tr>{head.map((h, i) => <th key={i} className="px-4 py-2.5 font-medium">{h}</th>)}</tr>
        </thead>
        <tbody className="divide-y divide-line">{children}</tbody>
      </table>
    </div>
  );
}

/* ---------- Toasts (state lives in the toasts slice) ---------- */
export function Toaster() {
  const toasts = useAppSelector((state) => state.toasts);
  const dispatch = useAppDispatch();
  return (
    <div className="fixed bottom-4 right-4 z-[60] flex w-80 flex-col gap-2" aria-live="polite">
      {toasts.map((toast) => (
        <div key={toast.id} className={cn(
          "flex items-start justify-between gap-3 rounded-md border px-4 py-3 text-sm shadow-lg",
          toast.tone === "ok" && "border-ok-600/20 bg-ok-50 text-ok-600",
          toast.tone === "bad" && "border-bad-600/20 bg-bad-50 text-bad-600",
          toast.tone === "info" && "border-info-600/20 bg-info-50 text-info-600",
        )}>
          <span>{toast.message}</span>
          <button onClick={() => dispatch(dismissToast(toast.id))} aria-label="Dismiss"><X className="size-4" /></button>
        </div>
      ))}
    </div>
  );
}

export function Stat({ label, value, tone, hint }: { label: string; value: ReactNode; tone?: "warn" | "bad"; hint?: string }) {
  return (
    <Card className="px-5 py-4">
      <p className="text-xs font-medium uppercase tracking-wide text-muted">{label}</p>
      <p className={cn("mt-1 text-2xl font-semibold tabular-nums", tone === "warn" && "text-warn-600", tone === "bad" && "text-bad-600")}>{value}</p>
      {hint && <p className="mt-0.5 text-xs text-muted">{hint}</p>}
    </Card>
  );
}

export function KeyValue({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-1 gap-x-6 gap-y-3 sm:grid-cols-2">
      {items.map(([k, v]) => (
        <div key={k}>
          <dt className="text-xs font-medium uppercase tracking-wide text-muted">{k}</dt>
          <dd className="mt-0.5 text-sm text-ink">{v ?? "—"}</dd>
        </div>
      ))}
    </dl>
  );
}

/* ---------- Menu (row actions) ---------- */
export interface MenuItem { label: string; onSelect: () => void; danger?: boolean; disabled?: boolean; icon?: ReactNode }

export function Menu({ items, label = "More actions" }: { items: (MenuItem | false | null | undefined)[]; label?: string }) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const shown = items.filter(Boolean) as MenuItem[];
  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent | KeyboardEvent) => {
      if (event instanceof KeyboardEvent ? event.key === "Escape" : !root.current?.contains(event.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", close);
    window.addEventListener("keydown", close);
    return () => {
      window.removeEventListener("mousedown", close);
      window.removeEventListener("keydown", close);
    };
  }, [open]);
  if (!shown.length) return null;
  return (
    <div ref={root} className="relative">
      <button type="button" aria-label={label} aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen((v) => !v)}
        className="rounded p-1.5 text-muted hover:bg-subtle hover:text-ink">
        <MoreHorizontal className="size-4" />
      </button>
      {open && (
        <div role="menu" className="absolute right-0 z-30 mt-1 min-w-44 overflow-hidden rounded-md border border-line bg-surface py-1 shadow-lg">
          {shown.map((item) => (
            <button key={item.label} type="button" role="menuitem" disabled={item.disabled}
              onClick={() => { setOpen(false); item.onSelect(); }}
              className={cn("flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm hover:bg-subtle disabled:opacity-40",
                item.danger ? "text-bad-600" : "text-ink")}>
              {item.icon}{item.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
