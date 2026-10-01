import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, MessageSquare, Pencil, Plus, Search, Trash2, X } from "lucide-react";
import { useState } from "react";
import { api, ApiError, qs } from "../api/client";
import type { Conversation, Page } from "../api/types";
import { cn } from "../lib/format";
import { clearConversation, openConversation } from "../store/assistantSlice";
import { useAppDispatch, useAppSelector, useToast } from "../store/hooks";
import { Button, Input, Menu, Modal } from "./ui";

const DAY = 24 * 60 * 60 * 1000;

function bucket(iso: string): string {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const age = today.getTime() - new Date(iso).getTime();
  if (age <= 0) return "Today";
  if (age <= DAY) return "Yesterday";
  if (age <= 7 * DAY) return "Previous 7 days";
  if (age <= 30 * DAY) return "Previous 30 days";
  return "Older";
}

/** The person's saved conversations with the AI Assistant: open, rename, delete, start a new one. */
export function ChatHistory({ onPicked }: { onPicked?: () => void }) {
  const dispatch = useAppDispatch();
  const toast = useToast();
  const queryClient = useQueryClient();
  const { conversationId, loading } = useAppSelector((state) => state.assistant);
  const [search, setSearch] = useState("");
  const [renaming, setRenaming] = useState<{ id: string; title: string } | null>(null);
  const [deleting, setDeleting] = useState<Conversation | "all" | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: ["conversations", search],
    queryFn: () => api.get<Page<Conversation>>(`/ai/conversations${qs({ search, limit: 100 })}`),
  });
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["conversations"] });

  const rename = useMutation({
    mutationFn: ({ id, title }: { id: string; title: string }) => api.patch(`/ai/conversations/${id}`, { title }),
    onSuccess: () => { setRenaming(null); refresh(); },
    onError: (err) => toast("bad", err instanceof ApiError ? err.message : "Could not rename the chat."),
  });
  const remove = useMutation({
    mutationFn: (target: Conversation | "all") =>
      api.delete(target === "all" ? "/ai/conversations" : `/ai/conversations/${target.id}`),
    onSuccess: (_result, target) => {
      if (target === "all" || target.id === conversationId) dispatch(clearConversation());
      toast("ok", target === "all" ? "Chat history cleared." : "Chat deleted.");
      setDeleting(null);
      refresh();
    },
    onError: (err) => toast("bad", err instanceof ApiError ? err.message : "Could not delete."),
  });

  function pick(id: string) {
    if (id !== conversationId) dispatch(openConversation(id));
    onPicked?.();
  }

  const groups: [string, Conversation[]][] = [];
  for (const conversation of data?.items ?? []) {
    const label = bucket(conversation.last_message_at);
    const group = groups.find(([name]) => name === label);
    if (group) group[1].push(conversation);
    else groups.push([label, [conversation]]);
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="space-y-2 p-3">
        <Button className="w-full" onClick={() => { dispatch(clearConversation()); onPicked?.(); }}>
          <Plus className="size-4" />New chat
        </Button>
        <div className="relative">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted" />
          <Input className="h-8 pl-8 text-xs" placeholder="Search chats…" value={search} onChange={(e) => setSearch(e.target.value)} aria-label="Search chats" />
        </div>
      </div>

      <nav className="min-h-0 flex-1 overflow-y-auto px-2 pb-2" aria-label="Chat history">
        {isLoading ? (
          <div className="space-y-2 px-1">{[0, 1, 2, 3].map((i) => <div key={i} className="h-8 animate-pulse rounded-md bg-subtle" />)}</div>
        ) : !groups.length ? (
          <div className="px-3 py-8 text-center text-xs text-muted">
            <MessageSquare className="mx-auto mb-2 size-5 opacity-60" />
            {search ? "No chats match your search." : "Your chats appear here, so you can come back to them."}
          </div>
        ) : groups.map(([label, conversations]) => (
          <div key={label} className="mb-3">
            <p className="px-2 pb-1 pt-2 text-[11px] font-semibold uppercase tracking-wide text-muted">{label}</p>
            <ul className="space-y-0.5">
              {conversations.map((conversation) => {
                const active = conversation.id === conversationId;
                if (renaming?.id === conversation.id) {
                  return (
                    <li key={conversation.id}>
                      <form className="flex items-center gap-1 rounded-md bg-subtle px-1.5 py-1"
                        onSubmit={(e) => { e.preventDefault(); if (renaming.title.trim()) rename.mutate(renaming); }}>
                        <Input autoFocus className="h-7 text-xs" value={renaming.title} maxLength={200}
                          onChange={(e) => setRenaming({ ...renaming, title: e.target.value })}
                          onKeyDown={(e) => e.key === "Escape" && setRenaming(null)} aria-label="Chat name" />
                        <button type="submit" className="rounded p-1 text-ok-600 hover:bg-surface" aria-label="Save name"><Check className="size-3.5" /></button>
                        <button type="button" onClick={() => setRenaming(null)} className="rounded p-1 text-muted hover:bg-surface" aria-label="Cancel"><X className="size-3.5" /></button>
                      </form>
                    </li>
                  );
                }
                return (
                  <li key={conversation.id} className={cn("group flex items-center rounded-md", active ? "bg-brand-50" : "hover:bg-subtle")}>
                    <button
                      type="button"
                      onClick={() => pick(conversation.id)}
                      aria-current={active ? "page" : undefined}
                      className={cn("min-w-0 flex-1 truncate px-2.5 py-1.5 text-left text-sm", active ? "font-medium text-brand-700" : "text-ink-soft")}
                      title={conversation.title}
                    >
                      {loading === conversation.id ? <span className="animate-pulse">{conversation.title}</span> : conversation.title}
                    </button>
                    <div className={cn("shrink-0 pr-1", !active && "opacity-0 focus-within:opacity-100 group-hover:opacity-100")}>
                      <Menu label={`Actions for ${conversation.title}`} items={[
                        { label: "Rename", icon: <Pencil className="size-4" />, onSelect: () => setRenaming({ id: conversation.id, title: conversation.title }) },
                        { label: "Delete", icon: <Trash2 className="size-4" />, danger: true, onSelect: () => setDeleting(conversation) },
                      ]} />
                    </div>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      </nav>

      {!!data?.total && (
        <div className="border-t border-line p-2">
          <button type="button" onClick={() => setDeleting("all")}
            className="flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-xs text-muted hover:bg-bad-50 hover:text-bad-600">
            <Trash2 className="size-3.5" />Clear chat history
          </button>
        </div>
      )}

      <Modal
        open={Boolean(deleting)}
        onClose={() => !remove.isPending && setDeleting(null)}
        title={deleting === "all" ? "Clear chat history?" : "Delete chat?"}
        footer={<>
          <Button variant="secondary" onClick={() => setDeleting(null)} disabled={remove.isPending}>Cancel</Button>
          <Button variant="danger" loading={remove.isPending} onClick={() => deleting && remove.mutate(deleting)}>
            <Trash2 className="size-4" />{deleting === "all" ? "Clear all" : "Delete"}
          </Button>
        </>}
      >
        <p className="text-sm text-ink-soft">
          {deleting === "all"
            ? `All ${data?.total ?? ""} of your chats are deleted. This cannot be undone.`
            : <>“<span className="font-medium text-ink">{deleting?.title}</span>” is deleted. This cannot be undone.</>}
        </p>
        <p className="mt-2 text-xs text-muted">Policies and documents are not affected. The audit log of questions asked is kept.</p>
      </Modal>
    </div>
  );
}
