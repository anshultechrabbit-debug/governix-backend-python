import { useQuery } from "@tanstack/react-query";
import { LifeBuoy, Plus } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router";
import { api, ApiError, qs } from "../api/client";
import type { Page, Ticket, TicketDetail } from "../api/types";
import { Badge, Button, Card, EmptyState, ErrorState, Field, Input, Modal, PageHeader, Select, SkeletonRows, Tabs, Textarea } from "../components/ui";
import { formatDateTime } from "../lib/format";
import { useAuth } from "../store/hooks";

type Box = "mine" | "queue";

export const TICKET_STATUS: Record<Ticket["status"], [string, "info" | "warn" | "ok" | "neutral"]> = {
  open: ["Open", "info"],
  in_progress: ["In progress", "info"],
  waiting_for_response: ["Waiting for response", "warn"],
  resolved: ["Resolved", "ok"],
  closed: ["Closed", "neutral"],
};
export const PRIORITY_TONE: Record<Ticket["priority"], "neutral" | "info" | "warn" | "bad"> = {
  low: "neutral", medium: "info", high: "warn", urgent: "bad",
};
const COMPLAINT_CATEGORIES = ["Branch service", "Staff behaviour", "Policy question", "Technical problem", "Other"];

/** Who handles what the caller raises (spec §45-46). */
function destination(role: string | undefined) {
  if (role === "department_user") return "your branch manager";
  if (role === "branch_manager") return "your organization admin";
  if (role === "org_admin") return "platform support";
  return null;
}

export function SupportPage() {
  const { me } = useAuth();
  const handles = me?.role === "branch_manager" || me?.role === "org_admin" || me?.role === "master_admin";
  const raises = me?.role !== "master_admin";
  const [box, setBox] = useState<Box>(raises ? "mine" : "queue");
  const [status, setStatus] = useState("active");
  const [kind, setKind] = useState("");
  const [creating, setCreating] = useState(false);
  const params = { box, status, kind, limit: 100 };
  const { data, error, isLoading, refetch } = useQuery({
    queryKey: ["tickets", params],
    queryFn: () => api.get<Page<Ticket>>(`/tickets${qs(params)}`),
  });
  const subtitle = me?.role === "department_user" ? "Raise complaints and support requests with your branch manager"
    : me?.role === "branch_manager" ? "Complaints and requests from your branch; support from your organization admin"
    : me?.role === "org_admin" ? "Requests from branch managers; support from the platform team"
    : "Platform support requests from organizations";

  return (
    <>
      <PageHeader title="Support" subtitle={subtitle}
        actions={raises && <Button onClick={() => setCreating(true)}><Plus className="size-4" />New ticket</Button>} />
      {raises && handles && (
        <Tabs<Box> value={box} onChange={setBox} tabs={[{ id: "queue", label: "To handle" }, { id: "mine", label: "My tickets" }]} />
      )}
      <Card className="mt-4">
        <div className="flex flex-wrap gap-3 border-b border-line p-4">
          <Select className="w-52" value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Status">
            <option value="active">Open and in progress</option>
            <option value="">All statuses</option>
            {Object.entries(TICKET_STATUS).map(([value, [label]]) => <option key={value} value={value}>{label}</option>)}
          </Select>
          <Select className="w-40" value={kind} onChange={(e) => setKind(e.target.value)} aria-label="Type">
            <option value="">All types</option>
            <option value="complaint">Complaints</option>
            <option value="support">Support</option>
          </Select>
        </div>
        {error ? <div className="p-4"><ErrorState error={error} onRetry={refetch} /></div>
          : isLoading ? <SkeletonRows />
          : !data?.items.length ? (
            <EmptyState icon={<LifeBuoy className="size-6" />} title="No tickets"
              description={box === "queue" ? "Nothing is waiting for you." : "Tickets you raise appear here with every response."}
              action={raises && box === "mine" && <Button onClick={() => setCreating(true)}><Plus className="size-4" />New ticket</Button>} />
          ) : (
            <ul className="divide-y divide-line">
              {data.items.map((t) => {
                const [label, tone] = TICKET_STATUS[t.status];
                return (
                  <li key={t.id}>
                    <Link to={`/support/${t.id}`} className="flex flex-wrap items-center gap-3 px-5 py-3 hover:bg-subtle/50">
                      <div className="min-w-0 flex-1">
                        <p className="truncate font-medium">{t.subject}</p>
                        <p className="text-xs text-muted">
                          {t.kind === "complaint" ? "Complaint" : "Support"}{t.category ? ` · ${t.category}` : ""} · {t.created_by_name ?? "—"} · updated {formatDateTime(t.updated_at)}
                          {t.message_count ? ` · ${t.message_count} update${t.message_count === 1 ? "" : "s"}` : ""}
                        </p>
                      </div>
                      <Badge tone={PRIORITY_TONE[t.priority]}>{t.priority}</Badge>
                      <Badge tone={tone}>{label}</Badge>
                    </Link>
                  </li>
                );
              })}
            </ul>
          )}
      </Card>
      <NewTicketModal open={creating} onClose={() => setCreating(false)} />
    </>
  );
}

function NewTicketModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { me } = useAuth();
  const navigate = useNavigate();
  const isUser = me?.role === "department_user";
  const [kind, setKind] = useState<"complaint" | "support">(isUser ? "complaint" : "support");
  const [category, setCategory] = useState("");
  const [subject, setSubject] = useState("");
  const [description, setDescription] = useState("");
  const [priority, setPriority] = useState("medium");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!open) return;
    setKind(isUser ? "complaint" : "support"); setCategory(""); setSubject(""); setDescription(""); setPriority("medium"); setError(null);
  }, [open, isUser]);
  const invalid = subject.trim().length < 3 || description.trim().length < 5;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (invalid) return;
    setBusy(true);
    try {
      const ticket = await api.post<TicketDetail>("/tickets", {
        kind, category: category || null, subject: subject.trim(), description: description.trim(), priority,
      });
      onClose();
      navigate(`/support/${ticket.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not raise the ticket.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal open={open} onClose={onClose} title="New ticket"
      footer={<><Button variant="secondary" onClick={onClose}>Cancel</Button>
        <Button type="submit" form="new-ticket" disabled={invalid} loading={busy}>Submit</Button></>}>
      <form id="new-ticket" onSubmit={submit} className="space-y-4">
        <p className="rounded-md bg-subtle px-3 py-2 text-sm text-ink-soft">This goes to <span className="font-medium text-ink">{destination(me?.role)}</span>. You are notified of every response.</p>
        {isUser && (
          <Field label="Type">
            <Select value={kind} onChange={(e) => setKind(e.target.value as "complaint" | "support")}>
              <option value="complaint">Complaint</option>
              <option value="support">Support request</option>
            </Select>
          </Field>
        )}
        {kind === "complaint" && (
          <Field label="Category">
            <Select value={category} onChange={(e) => setCategory(e.target.value)}>
              <option value="">Choose a category…</option>
              {COMPLAINT_CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
            </Select>
          </Field>
        )}
        <Field label="Subject *"><Input value={subject} onChange={(e) => setSubject(e.target.value)} maxLength={300} placeholder="Short summary" /></Field>
        <Field label="Description *"><Textarea rows={5} value={description} onChange={(e) => setDescription(e.target.value)} placeholder="What happened, and what would you like done?" /></Field>
        <Field label="Priority">
          <Select value={priority} onChange={(e) => setPriority(e.target.value)}>
            <option value="low">Low</option><option value="medium">Medium</option><option value="high">High</option><option value="urgent">Urgent</option>
          </Select>
        </Field>
        {error && <p className="rounded-md bg-bad-50 px-3 py-2 text-sm text-bad-600" role="alert">{error}</p>}
      </form>
    </Modal>
  );
}
