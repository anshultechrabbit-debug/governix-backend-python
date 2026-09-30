import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Bell, CheckCheck } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router";
import { api, qs } from "../api/client";
import type { AppNotification, Inbox } from "../api/types";
import { cn, formatDateTime } from "../lib/format";

export function useInbox(limit = 10, unreadOnly = false) {
  return useQuery({
    queryKey: ["notifications", limit, unreadOnly],
    queryFn: () => api.get<Inbox>(`/notifications${qs({ limit, unread_only: unreadOnly || undefined })}`),
    refetchInterval: 30_000,
  });
}

export function useMarkRead() {
  const queryClient = useQueryClient();
  return async (ids?: string[]) => {
    await api.post("/notifications/read", ids ? { ids } : {});
    await queryClient.invalidateQueries({ queryKey: ["notifications"] });
  };
}

export function NotificationItem({ item, onOpen }: { item: AppNotification; onOpen: (item: AppNotification) => void }) {
  return (
    <button type="button" onClick={() => onOpen(item)}
      className={cn("flex w-full gap-3 px-4 py-3 text-left text-sm hover:bg-subtle", !item.read_at && "bg-brand-50/60")}>
      <span className={cn("mt-1.5 size-2 shrink-0 rounded-full", item.read_at ? "bg-transparent" : "bg-brand-500")} aria-hidden />
      <span className="min-w-0 flex-1">
        <span className={cn("block", !item.read_at && "font-semibold")}>{item.title}</span>
        {item.body && <span className="mt-0.5 line-clamp-2 block text-xs text-ink-soft">{item.body}</span>}
        <span className="mt-1 block text-[11px] text-muted">{formatDateTime(item.created_at)}</span>
      </span>
    </button>
  );
}

/** The bell in the sidebar: unread count and the latest few, opening the linked page. */
export function NotificationBell() {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const navigate = useNavigate();
  const { data } = useInbox(8);
  const markRead = useMarkRead();
  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent) => !root.current?.contains(event.target as Node) && setOpen(false);
    window.addEventListener("mousedown", close);
    return () => window.removeEventListener("mousedown", close);
  }, [open]);

  const openItem = (item: AppNotification) => {
    setOpen(false);
    if (!item.read_at) void markRead([item.id]);
    if (item.link) navigate(item.link);
  };
  const unread = data?.unread ?? 0;
  return (
    <div ref={root} className="relative">
      <button type="button" onClick={() => setOpen((v) => !v)} aria-label={`Notifications${unread ? `, ${unread} unread` : ""}`}
        className="relative rounded p-1.5 text-ink-soft hover:bg-subtle hover:text-ink">
        <Bell className="size-4" />
        {unread > 0 && (
          <span className="absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-bad-600 px-1 text-[10px] font-semibold text-white">
            {unread > 99 ? "99+" : unread}
          </span>
        )}
      </button>
      {open && (
        <div className="absolute left-0 top-9 z-40 w-80 overflow-hidden rounded-lg border border-line bg-surface text-ink shadow-xl">
          <div className="flex items-center justify-between border-b border-line px-4 py-2.5">
            <p className="text-sm font-semibold">Notifications</p>
            {unread > 0 && <button type="button" onClick={() => void markRead()} className="flex items-center gap-1 text-xs font-medium text-brand-700 hover:underline"><CheckCheck className="size-3.5" />Mark all read</button>}
          </div>
          <div className="max-h-96 divide-y divide-line overflow-y-auto">
            {data?.items.length ? data.items.map((item) => <NotificationItem key={item.id} item={item} onOpen={openItem} />)
              : <p className="px-4 py-8 text-center text-sm text-muted">You're all caught up.</p>}
          </div>
          <Link to="/notifications" onClick={() => setOpen(false)} className="block border-t border-line px-4 py-2.5 text-center text-sm font-medium text-brand-700 hover:bg-subtle">View all</Link>
        </div>
      )}
    </div>
  );
}
