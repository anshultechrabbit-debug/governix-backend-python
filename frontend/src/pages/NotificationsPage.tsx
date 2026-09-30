import { Bell, CheckCheck } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router";
import type { AppNotification } from "../api/types";
import { NotificationItem, useInbox, useMarkRead } from "../components/Notifications";
import { Button, Card, EmptyState, ErrorState, PageHeader, SkeletonRows, Tabs } from "../components/ui";

export function NotificationsPage() {
  const [filter, setFilter] = useState<"all" | "unread">("all");
  const { data, error, isLoading, refetch } = useInbox(100, filter === "unread");
  const markRead = useMarkRead();
  const navigate = useNavigate();
  const open = (item: AppNotification) => {
    if (!item.read_at) void markRead([item.id]);
    if (item.link) navigate(item.link);
  };
  return (
    <>
      <PageHeader
        title="Notifications"
        subtitle="New and updated policies, assignments, expiring policies and ticket responses"
        actions={!!data?.unread && <Button variant="secondary" onClick={() => void markRead()}><CheckCheck className="size-4" />Mark all read</Button>}
      />
      <Tabs value={filter} onChange={setFilter} tabs={[{ id: "all", label: "All" }, { id: "unread", label: "Unread", count: data?.unread }]} />
      <Card className="mt-4">
        {error ? <div className="p-4"><ErrorState error={error} onRetry={refetch} /></div>
          : isLoading ? <SkeletonRows />
          : !data?.items.length ? <EmptyState icon={<Bell className="size-6" />} title={filter === "unread" ? "No unread notifications" : "No notifications yet"} />
          : <div className="divide-y divide-line">{data.items.map((item) => <NotificationItem key={item.id} item={item} onOpen={open} />)}</div>}
      </Card>
    </>
  );
}
