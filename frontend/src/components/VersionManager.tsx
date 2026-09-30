import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowUpToLine, Eye, FolderInput, GitCompare, GripVertical, RotateCcw, Undo2 } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router";
import { api, ApiError, qs } from "../api/client";
import type { CategoryVersion, Page, Policy } from "../api/types";
import { cn, formatBytes, formatDate } from "../lib/format";
import { useReorder } from "../lib/useReorder";
import { useToast } from "../store/hooks";
import { EffectiveDate, hasRealEffectiveDate } from "./domain";
import { Badge, Button, Field, Menu, Modal, Select, StatusBadge, Textarea } from "./ui";

interface Props {
  policy: { id: string; name: string; status: string };
  /** Every version, newest first (as the API returns them). */
  versions: CategoryVersion[];
  canManage: boolean;
  /** Arranging is only meaningful on the full, unfiltered list. */
  arrangeable?: boolean;
  onChanged: () => void;
}

interface PendingOrder { order: CategoryVersion[]; current: CategoryVersion | null; next: CategoryVersion | null }

/**
 * A policy's versions (spec §6, §9, §18). Dragging active versions reorders the
 * timeline; the version on top becomes the latest after confirmation. Version
 * numbers printed on documents and stated effective dates are never overridden.
 */
export function VersionManager({ policy, versions, canManage, arrangeable = true, onChanged }: Props) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const active = versions.filter((v) => v.status === "active");
  const withdrawn = versions.filter((v) => v.status !== "active");
  const [order, setOrder] = useState(active);
  const [pending, setPending] = useState<PendingOrder | null>(null);
  const [saving, setSaving] = useState(false);
  const [withdrawing, setWithdrawing] = useState<CategoryVersion | null>(null);
  const [restoring, setRestoring] = useState<CategoryVersion | null>(null);
  const [moving, setMoving] = useState<CategoryVersion | null>(null);
  const activeKey = active.map((v) => `${v.id}:${v.version_label}:${v.timeline_state}`).join(",");
  useEffect(() => setOrder(active), [activeKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const draggable = canManage && arrangeable && policy.status === "active" && order.length > 1;
  const byId = new Map(versions.map((v) => [v.id, v]));

  async function save(next: CategoryVersion[], confirm: boolean) {
    setSaving(true);
    try {
      await api.put(`/policies/${policy.id}/versions/order`, {
        version_ids: next.map((v) => v.id), confirm_latest_change: confirm,
      });
      setPending(null);
      toast("ok", "Version order saved.");
      refresh();
    } catch (err) {
      if (err instanceof ApiError && err.code === "LATEST_CHANGE_REQUIRES_CONFIRMATION") {
        const details = err.details as { current?: { id: string }; new?: { id: string } };
        setPending({
          order: next,
          current: details.current ? byId.get(details.current.id) ?? null : null,
          next: details.new ? byId.get(details.new.id) ?? null : null,
        });
        return;
      }
      setOrder(active);
      setPending(null);
      toast("bad", err instanceof ApiError ? err.message : "Could not save the order.");
    } finally {
      setSaving(false);
    }
  }

  function refresh() {
    onChanged();
    void queryClient.invalidateQueries({ queryKey: ["policy", policy.id] });
    void queryClient.invalidateQueries({ queryKey: ["policy-versions", policy.id] });
    void queryClient.invalidateQueries({ queryKey: ["category-documents"] });
  }

  const reorder = useReorder(order, (next) => {
    setOrder(next);
    void save(next, false);
  }, draggable && !saving);

  const makeLatest = (version: CategoryVersion) => {
    const next = [version, ...order.filter((v) => v.id !== version.id)];
    setOrder(next);
    void save(next, false);
  };

  const rows = [...order, ...withdrawn];
  return (
    <>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead className="border-b border-line bg-subtle/60 text-xs uppercase tracking-wide text-muted">
            <tr>
              {draggable && <th className="w-8" aria-label="Order" />}
              {["Version", "Document", "Effective", "Uploaded", "Uploaded by", "Status", ""].map((h, i) => (
                <th key={i} className="px-3 py-2 font-medium">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {rows.map((v) => {
              const isActive = v.status === "active";
              const line = isActive ? reorder.dropLine(v.id) : null;
              const previous = v.supersedes_version_id ? byId.get(v.supersedes_version_id) : undefined;
              return (
                <tr
                  key={v.id}
                  {...(isActive ? reorder.rowProps(v) : {})}
                  className={cn(
                    "transition-colors",
                    reorder.dragId === v.id && "opacity-40",
                    line === "before" && "shadow-[inset_0_2px_0_0_var(--color-brand-500)]",
                    line === "after" && "shadow-[inset_0_-2px_0_0_var(--color-brand-500)]",
                    !isActive && "bg-subtle/40 text-muted",
                  )}
                >
                  {draggable && (
                    <td className="px-2 py-2.5">
                      {isActive && (
                        <span {...reorder.handleProps(v, `version ${v.version_label}`)} className="flex cursor-grab items-center rounded p-0.5 text-muted hover:bg-subtle focus-visible:bg-subtle">
                          <GripVertical className="size-4" />
                        </span>
                      )}
                    </td>
                  )}
                  <td className="whitespace-nowrap px-3 py-2.5">
                    <span className="font-semibold">v{v.version_label}</span>
                    {v.revision_number > 0 && <span className="text-muted"> rev {v.revision_number}</span>}
                    {v.timeline_state === "current" && <Badge tone="brand" className="ml-2">Latest</Badge>}
                  </td>
                  <td className="max-w-72 px-3 py-2.5">
                    <p className="truncate font-medium text-ink" title={v.filename}>{v.filename}</p>
                    <p className="text-xs text-muted">PDF{v.page_count ? ` · ${v.page_count} pages` : ""} · {formatBytes(v.size_bytes)}</p>
                  </td>
                  <td className="whitespace-nowrap px-3 py-2.5">
                    {hasRealEffectiveDate(v) ? formatDate(v.effective_from) : <EffectiveDate version={v} prefix="" />}
                    {v.effective_to && hasRealEffectiveDate(v) && <p className="text-xs text-muted">until {formatDate(v.effective_to)}</p>}
                  </td>
                  <td className="whitespace-nowrap px-3 py-2.5 text-ink-soft">{formatDate(v.uploaded_at)}</td>
                  <td className="whitespace-nowrap px-3 py-2.5 text-ink-soft">{v.uploaded_by_name ?? "—"}</td>
                  <td className="px-3 py-2.5">
                    <StatusBadge status={v.timeline_state ?? v.status} />
                    {v.withdrawn_reason && <p className="mt-1 max-w-48 text-xs text-muted">{v.withdrawn_reason}</p>}
                  </td>
                  <td className="px-2 py-2.5">
                    <div className="flex items-center justify-end gap-0.5">
                      <Link to={`/documents/${v.document_id}/view`} className="rounded p-1.5 text-muted hover:bg-subtle hover:text-ink" title="View version" aria-label={`View version ${v.version_label}`}>
                        <Eye className="size-4" />
                      </Link>
                      {previous && isActive && (
                        <Link to={`/policies/${policy.id}/compare?base=${previous.id}&target=${v.id}`} className="rounded p-1.5 text-muted hover:bg-subtle hover:text-ink" title={`Compare with v${previous.version_label}`} aria-label={`Compare version ${v.version_label} with the previous version`}>
                          <GitCompare className="size-4" />
                        </Link>
                      )}
                      {canManage && policy.status === "active" && (
                        <Menu label={`Actions for version ${v.version_label}`} items={[
                          isActive && v.timeline_state !== "current" && order[0]?.id !== v.id && {
                            label: "Make latest version", icon: <ArrowUpToLine className="size-4" />, onSelect: () => makeLatest(v),
                          },
                          isActive && { label: "Withdraw version", icon: <Undo2 className="size-4" />, danger: true, onSelect: () => setWithdrawing(v) },
                          !isActive && { label: "Restore version", icon: <RotateCcw className="size-4" />, onSelect: () => setRestoring(v) },
                          { label: "Move to another policy…", icon: <FolderInput className="size-4" />, onSelect: () => setMoving(v) },
                        ]} />
                      )}
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {draggable && (
        <p className="border-t border-line px-3 py-2 text-xs text-muted">
          Drag <GripVertical className="inline size-3" /> to change the order; the version on top becomes the latest.
          Dates stated in a document are never changed.
        </p>
      )}

      <Modal
        open={Boolean(pending)}
        onClose={() => { setPending(null); setOrder(active); }}
        title="Make this document the latest version?"
        footer={<>
          <Button variant="secondary" onClick={() => { setPending(null); setOrder(active); }}>Cancel</Button>
          <Button loading={saving} onClick={() => pending && save(pending.order, true)}>Confirm</Button>
        </>}
      >
        <dl className="space-y-3 text-sm">
          <div>
            <dt className="text-xs font-medium uppercase tracking-wide text-muted">Current latest</dt>
            <dd className="mt-0.5">{pending?.current ? `${pending.current.filename} (v${pending.current.version_label})` : "No version in force"}</dd>
          </div>
          <div>
            <dt className="text-xs font-medium uppercase tracking-wide text-muted">New latest</dt>
            <dd className="mt-0.5 font-medium">{pending?.next ? pending.next.filename : "No version in force"}</dd>
          </div>
        </dl>
        <p className="mt-4 text-xs text-muted">
          Answers and searches use the latest version from now on. Earlier versions are kept as archived versions,
          and version numbers assigned automatically follow the new order.
        </p>
      </Modal>

      <WithdrawModal policyId={policy.id} version={withdrawing} onClose={() => setWithdrawing(null)} onDone={refresh} />
      <RestoreModal policyId={policy.id} version={restoring} onClose={() => setRestoring(null)} onDone={refresh} />
      <MoveModal policy={policy} version={moving} onClose={() => setMoving(null)} onDone={refresh} />
    </>
  );
}

function WithdrawModal({ policyId, version, onClose, onDone }: { policyId: string; version: CategoryVersion | null; onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => setReason(""), [version]);
  async function submit() {
    setBusy(true);
    try {
      await api.post(`/policies/${policyId}/versions/${version!.id}/withdraw`, { reason: reason.trim() });
      toast("ok", `Version ${version!.version_label} withdrawn. It is kept in the history.`);
      onClose();
      onDone();
    } catch (err) {
      toast("bad", err instanceof ApiError ? err.message : "Could not withdraw the version.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal open={Boolean(version)} onClose={onClose} title={`Withdraw version ${version?.version_label}`}
      footer={<><Button variant="secondary" onClick={onClose}>Cancel</Button>
        <Button variant="danger" disabled={reason.trim().length < 5} loading={busy} onClick={submit}>Withdraw</Button></>}>
      <p className="text-sm text-ink-soft">The version leaves the timeline but is never deleted; the previous version covers its period again. This is recorded in the audit log.</p>
      <Field label="Reason *"><Textarea className="mt-1" rows={3} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="e.g. Issued in error" /></Field>
    </Modal>
  );
}

function RestoreModal({ policyId, version, onClose, onDone }: { policyId: string; version: CategoryVersion | null; onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  async function submit() {
    setBusy(true);
    try {
      await api.post(`/policies/${policyId}/versions/${version!.id}/restore`);
      toast("ok", `Version ${version!.version_label} restored to the timeline.`);
      onClose();
      onDone();
    } catch (err) {
      toast("bad", err instanceof ApiError ? err.message : "Could not restore the version.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal open={Boolean(version)} onClose={onClose} title={`Restore version ${version?.version_label}`}
      footer={<><Button variant="secondary" onClick={onClose}>Cancel</Button><Button loading={busy} onClick={submit}>Restore</Button></>}>
      <p className="text-sm text-ink-soft">
        The version rejoins the timeline at its effective date{version && hasRealEffectiveDate(version) ? ` (${formatDate(version.effective_from)})` : ""}.
        If that makes it the version in force, answers use it from now on.
      </p>
    </Modal>
  );
}

function MoveModal({ policy, version, onClose, onDone }: { policy: Props["policy"]; version: CategoryVersion | null; onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const [target, setTarget] = useState("");
  const [understood, setUnderstood] = useState(false);
  const [busy, setBusy] = useState(false);
  const policies = useQuery({
    queryKey: ["policies", "move-targets"],
    queryFn: () => api.get<Page<Policy>>(`/policies${qs({ status: "active", limit: 200 })}`),
    enabled: Boolean(version),
  });
  useEffect(() => { setTarget(""); setUnderstood(false); }, [version]);
  const targetName = policies.data?.items.find((p) => p.id === target)?.name;
  async function submit() {
    setBusy(true);
    try {
      await api.post(`/policies/${policy.id}/versions/${version!.id}/move`, { target_policy_id: target, confirm: true });
      toast("ok", `Moved to ${targetName}.`);
      onClose();
      onDone();
    } catch (err) {
      toast("bad", err instanceof ApiError ? err.message : "Could not move the document.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal open={Boolean(version)} onClose={onClose} title="Move to another policy"
      footer={<><Button variant="secondary" onClick={onClose}>Cancel</Button>
        <Button disabled={!target || !understood} loading={busy} onClick={submit}>Move document</Button></>}>
      <p className="text-sm text-ink-soft"><span className="font-medium text-ink">{version?.filename}</span> (v{version?.version_label}) currently belongs to <span className="font-medium text-ink">{policy.name}</span>.</p>
      <div className="mt-4 space-y-3">
        <Field label="Move to">
          <Select value={target} onChange={(e) => setTarget(e.target.value)}>
            <option value="">Choose a policy…</option>
            {policies.data?.items.filter((p) => p.id !== policy.id).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </Select>
        </Field>
        <label className="flex items-start gap-2 text-sm">
          <input type="checkbox" className="mt-1" checked={understood} onChange={(e) => setUnderstood(e.target.checked)} />
          <span>I understand this changes which policy the document belongs to{targetName ? ` (${targetName})` : ""}. It joins that policy's versions at its effective date; the move is recorded in the audit log.</span>
        </label>
      </div>
    </Modal>
  );
}
