import { useQuery, useQueryClient } from "@tanstack/react-query";
import { History, UserMinus, UserPlus, Users } from "lucide-react";
import { useEffect, useState } from "react";
import { api, ApiError, qs } from "../api/client";
import type { AssignableUser, Assignment, Page, Policy, User } from "../api/types";
import { formatDate } from "../lib/format";
import { useToast } from "../store/hooks";
import { Badge, Button, Card, CardHeader, EmptyState, ErrorState, Input, Modal, Select, SkeletonRows } from "./ui";

/**
 * Who may read this policy (spec §27-28). Users see, search and ask the AI
 * about a policy only while it is assigned to them. Managers see only the Users
 * of their own branch here.
 */
export function PolicyAssignments({ policy }: { policy: { id: string; name: string; status: string } }) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const [history, setHistory] = useState(false);
  const [adding, setAdding] = useState(false);
  const key = ["assignments", "policy", policy.id, history];
  const { data, error, isLoading, refetch } = useQuery({
    queryKey: key,
    queryFn: () => api.get<Assignment[]>(`/policies/${policy.id}/assignments${qs({ include_removed: history || undefined })}`),
  });

  async function remove(assignment: Assignment) {
    try {
      await api.delete(`/policies/${policy.id}/assignments/${assignment.user_id}`);
      toast("ok", `${assignment.user_name} no longer has access to ${policy.name}.`);
      void queryClient.invalidateQueries({ queryKey: ["assignments"] });
    } catch (err) {
      toast("bad", err instanceof ApiError ? err.message : "Could not remove the assignment.");
    }
  }

  return (
    <Card>
      <CardHeader
        title="Assigned users"
        subtitle="Users read and ask the AI only about policies assigned to them. Managers see policies in their scope automatically."
        actions={<div className="flex flex-wrap gap-2">
          <Button size="sm" variant="ghost" onClick={() => setHistory((v) => !v)}><History className="size-3.5" />{history ? "Hide history" : "History"}</Button>
          {policy.status === "active" && <Button size="sm" onClick={() => setAdding(true)}><UserPlus className="size-3.5" />Assign users</Button>}
        </div>}
      />
      {error ? <div className="p-4"><ErrorState error={error} onRetry={refetch} /></div>
        : isLoading ? <SkeletonRows rows={3} />
        : !data?.length ? (
          <EmptyState icon={<Users className="size-6" />} title="Not assigned to anyone yet"
            description="Assign this policy to the users who need it; they are notified." />
        ) : (
          <ul className="divide-y divide-line">
            {data.map((a) => (
              <li key={a.id} className="flex flex-wrap items-center justify-between gap-3 px-4 sm:px-5 py-3 text-sm">
                <div className="min-w-0">
                  <p className="font-medium">{a.user_name} {a.removed_at && <Badge className="ml-1">Removed</Badge>}</p>
                  <p className="text-xs text-muted">
                    {a.user_email} · assigned {formatDate(a.assigned_at)}{a.assigned_by_name ? ` by ${a.assigned_by_name}` : ""}
                    {a.removed_at && ` · removed ${formatDate(a.removed_at)}${a.removed_by_name ? ` by ${a.removed_by_name}` : ""}`}
                  </p>
                </div>
                {!a.removed_at && (
                  <Button size="sm" variant="ghost" className="text-bad-600" onClick={() => remove(a)}><UserMinus className="size-3.5" />Remove</Button>
                )}
              </li>
            ))}
          </ul>
        )}
      <AssignUsersModal policy={policy} open={adding} onClose={() => setAdding(false)} />
    </Card>
  );
}

function AssignUsersModal({ policy, open, onClose }: { policy: { id: string; name: string }; open: boolean; onClose: () => void }) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (open) { setSearch(""); setChosen(new Set()); } }, [open]);
  const candidates = useQuery({
    queryKey: ["assignable", policy.id, search],
    queryFn: () => api.get<AssignableUser[]>(`/policies/${policy.id}/assignable-users${qs({ search })}`),
    enabled: open,
  });
  const toggle = (id: string) => setChosen((current) => {
    const next = new Set(current);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    return next;
  });

  async function submit() {
    setBusy(true);
    try {
      await api.post(`/policies/${policy.id}/assignments`, { user_ids: [...chosen] });
      toast("ok", `Assigned to ${chosen.size} user${chosen.size === 1 ? "" : "s"}. They have been notified.`);
      void queryClient.invalidateQueries({ queryKey: ["assignments"] });
      onClose();
    } catch (err) {
      toast("bad", err instanceof ApiError ? err.message : "Could not assign the policy.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal open={open} onClose={onClose} title={`Assign ${policy.name}`}
      footer={<><Button variant="secondary" onClick={onClose}>Cancel</Button>
        <Button disabled={!chosen.size} loading={busy} onClick={submit}>Assign {chosen.size || ""}</Button></>}>
      <Input placeholder="Search users by name or email…" value={search} onChange={(e) => setSearch(e.target.value)} autoFocus />
      <p className="mt-2 text-xs text-muted">Only users this policy's branch scope reaches are listed.</p>
      <ul className="mt-3 max-h-80 divide-y divide-line overflow-y-auto rounded-md border border-line">
        {candidates.isLoading && <li className="p-3"><SkeletonRows rows={2} /></li>}
        {candidates.data?.map((u) => (
          <li key={u.id}>
            <label className="flex cursor-pointer items-center gap-3 px-3 py-2 text-sm hover:bg-subtle">
              <input type="checkbox" disabled={u.assigned} checked={u.assigned || chosen.has(u.id)} onChange={() => toggle(u.id)} />
              <span className="min-w-0 flex-1"><span className="block font-medium">{u.full_name}</span><span className="block text-xs text-muted">{u.email}</span></span>
              {u.assigned && <Badge tone="ok">Assigned</Badge>}
            </label>
          </li>
        ))}
        {candidates.data && !candidates.data.length && <li className="px-3 py-6 text-center text-sm text-muted">No users found.</li>}
      </ul>
    </Modal>
  );
}

/** The Users page: which policies one User can read, and assigning more. */
export function UserPoliciesModal({ user, onClose }: { user: User | null; onClose: () => void }) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const [policyId, setPolicyId] = useState("");
  const [busy, setBusy] = useState(false);
  const assignments = useQuery({
    queryKey: ["assignments", "user", user?.id],
    queryFn: () => api.get<Assignment[]>(`/users/${user!.id}/assignments`),
    enabled: Boolean(user),
  });
  const policies = useQuery({
    queryKey: ["policies", "assignable"],
    queryFn: () => api.get<Page<Policy>>(`/policies${qs({ status: "active", limit: 200 })}`),
    enabled: Boolean(user),
  });
  const assigned = new Set(assignments.data?.map((a) => a.policy_id));
  useEffect(() => setPolicyId(""), [user]);

  async function add() {
    setBusy(true);
    try {
      await api.post(`/users/${user!.id}/assignments`, { policy_ids: [policyId] });
      setPolicyId("");
      void queryClient.invalidateQueries({ queryKey: ["assignments"] });
    } catch (err) {
      toast("bad", err instanceof ApiError ? err.message : "Could not assign the policy.");
    } finally {
      setBusy(false);
    }
  }

  async function remove(assignment: Assignment) {
    try {
      await api.delete(`/policies/${assignment.policy_id}/assignments/${assignment.user_id}`);
      void queryClient.invalidateQueries({ queryKey: ["assignments"] });
    } catch (err) {
      toast("bad", err instanceof ApiError ? err.message : "Could not remove the assignment.");
    }
  }

  return (
    <Modal open={Boolean(user)} onClose={onClose} wide title={`Policies assigned to ${user?.full_name ?? ""}`}
      footer={<Button onClick={onClose}>Done</Button>}>
      <div className="flex flex-col gap-2 sm:flex-row">
        <Select value={policyId} onChange={(e) => setPolicyId(e.target.value)} aria-label="Policy to assign">
          <option value="">Choose a policy to assign…</option>
          {policies.data?.items.filter((p) => !assigned.has(p.id)).map((p) => <option key={p.id} value={p.id}>{p.name}{p.branch_id ? "" : " (global)"}</option>)}
        </Select>
        <Button disabled={!policyId} loading={busy} onClick={add}><UserPlus className="size-4" />Assign</Button>
      </div>
      {assignments.error ? <div className="mt-4"><ErrorState error={assignments.error} /></div> : (
        <ul className="mt-4 divide-y divide-line rounded-md border border-line">
          {assignments.data?.map((a) => (
            <li key={a.id} className="flex items-center justify-between gap-3 px-3 py-2 text-sm">
              <div className="min-w-0"><p className="break-words font-medium">{a.policy_name}</p><p className="text-xs text-muted">{a.category_name} · assigned {formatDate(a.assigned_at)}</p></div>
              <Button size="sm" variant="ghost" className="text-bad-600" onClick={() => remove(a)}>Remove</Button>
            </li>
          ))}
          {assignments.data && !assignments.data.length && <li className="px-3 py-6 text-center text-sm text-muted">No policies assigned yet.</li>}
        </ul>
      )}
    </Modal>
  );
}
