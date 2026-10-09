import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Download, Paperclip, Send } from "lucide-react";
import { useRef, useState } from "react";
import { Link, useParams } from "react-router";
import { api, ApiError } from "../api/client";
import type { TicketDetail, TicketStatus } from "../api/types";
import { Badge, Button, Card, CardHeader, ErrorState, Field, KeyValue, Select, SkeletonRows, Textarea } from "../components/ui";
import { cn, formatBytes, formatDateTime, humanize } from "../lib/format";
import { useAuth, useToast } from "../store/hooks";
import { PRIORITY_TONE, TICKET_STATUS } from "./SupportPage";

export function TicketPage() {
  const { id } = useParams();
  const { me } = useAuth();
  const toast = useToast();
  const queryClient = useQueryClient();
  const [reply, setReply] = useState("");
  const [status, setStatus] = useState<TicketStatus | "">("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const file = useRef<HTMLInputElement>(null);
  const { data: ticket, error, isLoading, refetch } = useQuery({
    queryKey: ["ticket", id],
    queryFn: () => api.get<TicketDetail>(`/tickets/${id}`),
    refetchInterval: 20_000,
  });

  async function run(action: () => Promise<unknown>, done?: string) {
    setBusy(true);
    try {
      const updated = await action();
      if (updated) queryClient.setQueryData(["ticket", id], updated);
      void queryClient.invalidateQueries({ queryKey: ["tickets"] });
      if (done) toast("ok", done);
      return true;
    } catch (err) {
      toast("bad", err instanceof ApiError ? err.message : "Something went wrong.");
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function download(attachmentId: string, filename: string) {
    try {
      const { url } = await api.blobUrl(`/tickets/${id}/attachments/${attachmentId}`);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 10_000);
    } catch {
      toast("bad", "Could not download the attachment.");
    }
  }

  if (error) return <ErrorState error={error} onRetry={refetch} />;
  if (isLoading || !ticket) return <Card><SkeletonRows rows={6} /></Card>;
  const [label, tone] = TICKET_STATUS[ticket.status];
  const mine = ticket.created_by_id === me?.id;
  const closed = ticket.status === "closed";

  return (
    <>
      <nav className="mb-3 text-sm text-muted"><Link to="/support" className="flex items-center gap-1 hover:text-ink"><ArrowLeft className="size-4" />Support</Link></nav>
      <div className="mb-5 flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="break-words text-lg font-semibold tracking-tight sm:text-xl">{ticket.subject}</h1>
          <p className="mt-1 text-sm text-muted">{ticket.kind === "complaint" ? "Complaint" : "Support request"}{ticket.category ? ` · ${ticket.category}` : ""} · raised {formatDateTime(ticket.created_at)} by {ticket.created_by_name ?? "—"}</p>
        </div>
        <div className="flex flex-wrap gap-2"><Badge tone={PRIORITY_TONE[ticket.priority]}>{humanize(ticket.priority)} priority</Badge><Badge tone={tone}>{label}</Badge></div>
      </div>

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_20rem]">
        <div className="min-w-0 space-y-4">
          <Card className="p-4 sm:p-5"><p className="whitespace-pre-wrap text-sm leading-relaxed">{ticket.description}</p></Card>
          <Card>
            <CardHeader title="Conversation" />
            <ol className="space-y-3 p-4 sm:p-5">
              {ticket.messages.map((m) => m.status_change && !m.body ? (
                <li key={m.id} className="text-center text-xs text-muted">{m.author_name ?? "Someone"} set the status to <span className="font-medium">{TICKET_STATUS[m.status_change][0]}</span> · {formatDateTime(m.created_at)}</li>
              ) : (
                <li key={m.id} className={cn("max-w-[85%] rounded-lg px-4 py-2.5 text-sm", m.author_id === me?.id ? "ml-auto bg-brand-50" : "bg-subtle")}>
                  <p className="mb-1 text-xs text-muted">{m.author_name ?? "—"}{m.author_role ? ` · ${humanize(m.author_role === "department_user" ? "user" : m.author_role)}` : ""} · {formatDateTime(m.created_at)}</p>
                  {m.status_change && <p className="mb-1 text-xs font-medium text-ink-soft">Status: {TICKET_STATUS[m.status_change][0]}</p>}
                  <p className="whitespace-pre-wrap">{m.body}</p>
                </li>
              ))}
              {!ticket.messages.length && <li className="text-center text-sm text-muted">No replies yet.</li>}
            </ol>
            {!closed ? (
              <div className="border-t border-line p-4">
                <Textarea rows={3} value={reply} onChange={(e) => setReply(e.target.value)} placeholder="Write a reply…" aria-label="Reply" />
                <div className="mt-2 flex justify-end">
                  <Button disabled={!reply.trim()} loading={busy} onClick={async () => {
                    if (await run(() => api.post(`/tickets/${id}/messages`, { body: reply.trim() }))) setReply("");
                  }}><Send className="size-4" />Send</Button>
                </div>
              </div>
            ) : <p className="border-t border-line px-4 sm:px-5 py-3 text-sm text-muted">This ticket is closed.{mine && " Reopen it to reply."}</p>}
          </Card>
        </div>

        <aside className="space-y-4">
          <Card className="p-4">
            <KeyValue items={[
              ["Handled by", ticket.level === "branch" ? "Branch manager" : ticket.level === "organization" ? "Organization admin" : "Platform support"],
              ["Assigned to", ticket.assigned_to_name ?? "Unassigned"],
              ["Resolved", ticket.resolved_at ? formatDateTime(ticket.resolved_at) : "—"],
            ]} />
            {ticket.can_handle && ticket.assigned_to_id !== me?.id && !closed && (
              <Button size="sm" variant="secondary" className="mt-4 w-full" loading={busy}
                onClick={() => run(() => api.post(`/tickets/${id}/assign`, { user_id: me?.id }), "Assigned to you.")}>Assign to me</Button>
            )}
          </Card>

          {ticket.can_handle && (
            <Card className="space-y-3 p-4">
              <p className="text-sm font-semibold">Update status</p>
              <Select value={status} onChange={(e) => setStatus(e.target.value as TicketStatus)} aria-label="New status">
                <option value="">Choose…</option>
                {(Object.keys(TICKET_STATUS) as TicketStatus[]).filter((s) => s !== ticket.status).map((s) => <option key={s} value={s}>{TICKET_STATUS[s][0]}</option>)}
              </Select>
              <Field label="Note (optional)"><Textarea rows={2} value={note} onChange={(e) => setNote(e.target.value)} /></Field>
              <Button className="w-full" disabled={!status} loading={busy} onClick={async () => {
                if (await run(() => api.post(`/tickets/${id}/status`, { status, note: note.trim() || null }), "Status updated.")) { setStatus(""); setNote(""); }
              }}>Update</Button>
            </Card>
          )}

          {mine && !ticket.can_handle && (
            <Card className="space-y-2 p-4">
              {ticket.status === "resolved" && <Button className="w-full" loading={busy} onClick={() => run(() => api.post(`/tickets/${id}/status`, { status: "closed" }), "Thanks — ticket closed.")}>Accept and close</Button>}
              {(ticket.status === "resolved" || closed) && <Button variant="secondary" className="w-full" loading={busy} onClick={() => run(() => api.post(`/tickets/${id}/status`, { status: "open" }), "Ticket reopened.")}>Reopen</Button>}
              {!["resolved", "closed"].includes(ticket.status) && <Button variant="ghost" className="w-full" loading={busy} onClick={() => run(() => api.post(`/tickets/${id}/status`, { status: "closed" }), "Ticket closed.")}>Withdraw ticket</Button>}
            </Card>
          )}

          <Card className="p-4">
            <div className="mb-2 flex items-center justify-between">
              <p className="text-sm font-semibold">Attachments</p>
              {!closed && <Button size="sm" variant="ghost" onClick={() => file.current?.click()}><Paperclip className="size-3.5" />Attach</Button>}
              <input ref={file} type="file" className="hidden" accept=".pdf,.png,.jpg,.jpeg,.txt,.csv,.docx,.xlsx"
                onChange={(e) => {
                  const chosen = e.target.files?.[0];
                  e.target.value = "";
                  if (!chosen) return;
                  const form = new FormData();
                  form.append("file", chosen);
                  void run(() => api.upload(`/tickets/${id}/attachments`, form), "Attached.");
                }} />
            </div>
            {ticket.attachments.length ? (
              <ul className="space-y-1.5 text-sm">
                {ticket.attachments.map((a) => (
                  <li key={a.id}>
                    <button type="button" onClick={() => download(a.id, a.filename)} className="flex w-full items-center gap-2 rounded px-1 py-1 text-left hover:bg-subtle">
                      <Download className="size-3.5 text-muted" /><span className="min-w-0 flex-1 truncate">{a.filename}</span><span className="text-xs text-muted">{formatBytes(a.size_bytes)}</span>
                    </button>
                  </li>
                ))}
              </ul>
            ) : <p className="text-xs text-muted">PDF, images, text, Word or Excel — up to 10 MB.</p>}
          </Card>
        </aside>
      </div>
    </>
  );
}
