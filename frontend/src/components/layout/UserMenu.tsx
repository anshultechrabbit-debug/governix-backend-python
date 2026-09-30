import { KeyRound, LogOut, ShieldCheck, UserRound } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, ApiError } from "../../api/client";
import { Button, Field, KeyValue, PasswordInput } from "../ui";
import { humanize } from "../../lib/format";
import { useAuth, useToast } from "../../store/hooks";

const ROLE_LABEL: Record<string, string> = {
  master_admin: "Master Admin",
  org_admin: "Organization Admin",
  branch_manager: "Branch Manager",
  department_user: "User",
};

/** The signed-in identity, the exact access it carries, and sign out. */
export function UserMenu() {
  const { me, can, logout } = useAuth();
  const [open, setOpen] = useState(false);
  const [changing, setChanging] = useState(false);
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent | KeyboardEvent) => {
      if (event instanceof KeyboardEvent ? event.key !== "Escape" : !root.current?.contains(event.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", close);
    window.addEventListener("keydown", close);
    return () => {
      window.removeEventListener("mousedown", close);
      window.removeEventListener("keydown", close);
    };
  }, [open]);

  if (!me) return null;
  return (
    <div ref={root} className="relative">
      <button type="button" onClick={() => setOpen((shown) => !shown)} aria-expanded={open} aria-haspopup="menu"
        aria-label="Account and access"
        className="flex size-8 items-center justify-center rounded-full bg-brand-50 text-xs font-semibold text-brand-700 hover:bg-brand-100">
        {me.full_name.trim().slice(0, 1).toUpperCase()}
      </button>
      {open && (
        <div role="menu" className="absolute bottom-11 right-0 z-40 w-80 overflow-hidden rounded-lg border border-line bg-surface shadow-xl">
          <div className="border-b border-line p-4">
            <p className="text-sm font-semibold text-ink">{me.full_name}</p>
            <p className="text-xs text-muted">{me.email}</p>
            <p className="mt-2 flex items-center gap-1.5 text-xs font-medium text-brand-700">
              <ShieldCheck className="size-3.5" />{ROLE_LABEL[me.role]}
            </p>
            <p className="mt-1 text-[11px] text-muted">
              {[me.organization_name ?? "Platform", me.branch_id ? "Branch scope" : "Organization scope"]
                .filter(Boolean).join(" · ")}
            </p>
          </div>
          <details className="group border-b border-line">
            <summary className="flex cursor-pointer list-none items-center gap-2 px-4 py-2.5 text-sm text-ink-soft hover:bg-subtle">
              <UserRound className="size-4" />What I can access
            </summary>
            <div className="space-y-3 px-4 pb-4">
              <KeyValue items={[
                ["Organization", me.organization_name ?? "Platform"],
                ["Branch", me.branch_id ? "Own branch" : "All branches"],
                ["Reading", me.role === "department_user" ? "Assigned policies only" : "Global + own branch"],
                ["Uploading", can("documents:upload") ? "Global or branch policies" : "No upload rights"],
              ]} />
              <div>
                <p className="text-xs font-medium uppercase tracking-wide text-muted">Permissions</p>
                <div className="mt-1.5 flex flex-wrap gap-1">
                  {me.permissions.map((permission) => (
                    <span key={permission} className="rounded bg-subtle px-1.5 py-0.5 text-[11px] text-ink-soft">{humanize(permission)}</span>
                  ))}
                </div>
              </div>
            </div>
          </details>
          <button type="button" role="menuitem" onClick={() => { setOpen(false); setChanging(true); }}
            className="flex w-full items-center gap-2 border-b border-line px-4 py-2.5 text-sm text-ink-soft hover:bg-subtle">
            <KeyRound className="size-4" />Change password
          </button>
          <button type="button" role="menuitem" onClick={() => void logout()}
            className="flex w-full items-center gap-2 px-4 py-2.5 text-sm text-bad-600 hover:bg-bad-50">
            <LogOut className="size-4" />Sign out
          </button>
        </div>
      )}
      {changing && <ChangePasswordDialog onClose={() => setChanging(false)} />}
    </div>
  );
}

function ChangePasswordDialog({ onClose }: { onClose: () => void }) {
  const { logout } = useAuth();
  const toast = useToast();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.post("/auth/change-password", { current_password: current, new_password: next });
      toast("ok", "Password changed. Please sign in again.");
      onClose();
      await logout();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not change password.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 p-4" onMouseDown={onClose}>
      <div role="dialog" aria-modal="true" aria-label="Change password"
        className="w-full max-w-md rounded-lg bg-surface shadow-xl" onMouseDown={(event) => event.stopPropagation()}>
        <form onSubmit={submit} className="space-y-4 p-5">
          <div>
            <h2 className="text-base font-semibold">Change password</h2>
            <p className="text-sm text-muted">Signs you out of every device.</p>
          </div>
          <Field label="Current password"><PasswordInput autoComplete="current-password" required value={current} onChange={(event) => setCurrent(event.target.value)} /></Field>
          <Field label="New password" hint="At least 12 characters"><PasswordInput autoComplete="new-password" required minLength={12} value={next} onChange={(event) => setNext(event.target.value)} /></Field>
          {error && <p className="rounded-md bg-bad-50 px-3 py-2 text-sm text-bad-600" role="alert">{error}</p>}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="secondary" onClick={onClose}>Cancel</Button>
            <Button type="submit" loading={busy}>Change password</Button>
          </div>
        </form>
      </div>
    </div>
  );
}
