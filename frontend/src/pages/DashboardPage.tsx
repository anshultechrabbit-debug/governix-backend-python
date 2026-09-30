import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, Bell, Bot, LifeBuoy, ScrollText, Upload, Users } from "lucide-react";
import { Link } from "react-router";
import { api } from "../api/client";
import type { Dashboard } from "../api/types";
import { hasRealEffectiveDate } from "../components/domain";
import { Button, Card, CardHeader, EmptyState, ErrorState, PageHeader, SkeletonRows, Stat, StatusBadge } from "../components/ui";
import { formatDate, formatDateTime } from "../lib/format";
import { useAuth } from "../store/hooks";

export function DashboardPage() {
  const { can, me } = useAuth();
  const { data, error, isLoading, refetch } = useQuery({ queryKey: ["dashboard"], queryFn: () => api.get<Dashboard>("/dashboard") });
  const isUser = me?.role === "department_user";
  const isBranchManager = me?.role === "branch_manager";

  return (
    <>
      <PageHeader
        title={isUser ? "Home" : "Dashboard"}
        subtitle={isUser
          ? "Your assigned policies, the questions you asked, and anything that changed"
          : isBranchManager
            ? "Global policies, your branch's policies, and the users you manage"
            : "Documents, policies and activity you can access"}
        actions={<>
          {can("ai:query") && <Link to="/assistant"><Button variant="secondary"><Bot className="size-4" />Ask Governix</Button></Link>}
          {can("documents:upload") && <Link to="/documents/upload"><Button><Upload className="size-4" />Upload</Button></Link>}
        </>}
      />
      {error && <ErrorState error={error} onRetry={refetch} />}
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat label="Documents" value={data?.counts.documents ?? "—"} hint={data ? `${data.counts.ready_documents} searchable` : undefined} />
        <Stat label="Active policies" value={data?.counts.active_policies ?? "—"} />
        <Stat label="Processing" value={data?.counts.processing ?? "—"} />
        <Stat label="Needs attention" value={data ? data.counts.requires_review + data.counts.failed : "—"}
          tone={data && data.counts.requires_review + data.counts.failed > 0 ? "warn" : undefined}
          hint={data ? `${data.counts.version_conflicts} version conflicts · ${data.counts.failed} failed` : undefined} />
      </div>

      {/* Role-aware entry points into the spec's flows. */}
      <div className="mt-6 grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {can("policies:read") && (
          <Link to="/policies" className="rounded-lg border border-line bg-surface p-4 transition-colors hover:border-brand-500/40">
            <ScrollText className="size-5 text-brand-600" />
            <p className="mt-2 text-sm font-semibold">{isUser ? "My Policies" : "Policies"}</p>
            <p className="mt-0.5 text-xs text-muted">{isUser ? "Read, with summaries and what changed" : "Global and branch policies, versions and changes"}</p>
          </Link>
        )}
        {can("users:manage") && (
          <Link to="/users" className="rounded-lg border border-line bg-surface p-4 transition-colors hover:border-brand-500/40">
            <Users className="size-5 text-brand-600" />
            <p className="mt-2 text-sm font-semibold">{isBranchManager ? "Branch Users" : "Users & Roles"}</p>
            <p className="mt-0.5 text-xs text-muted">Create accounts and assign the policies they can read</p>
          </Link>
        )}
        <Link to="/notifications" className="rounded-lg border border-line bg-surface p-4 transition-colors hover:border-brand-500/40">
          <Bell className="size-5 text-brand-600" />
          <p className="mt-2 text-sm font-semibold">Notifications</p>
          <p className="mt-0.5 text-xs text-muted">New policies, assignments, updates and ticket replies</p>
        </Link>
        {can("support") && (
          <Link to="/support" className="rounded-lg border border-line bg-surface p-4 transition-colors hover:border-brand-500/40">
            <LifeBuoy className="size-5 text-brand-600" />
            <p className="mt-2 text-sm font-semibold">{isUser ? "Complaints & Support" : "Support"}</p>
            <p className="mt-0.5 text-xs text-muted">{isUser ? "Raise a complaint with your branch manager" : "Tickets raised to the level above you"}</p>
          </Link>
        )}
      </div>

      <div className="mt-6 grid gap-6 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <CardHeader title={isUser ? "Policies updated for you" : "Recent policy changes"} subtitle="Latest confirmed versions" />
          {isLoading ? <SkeletonRows rows={4} /> : !data?.recent_changes.length ? (
            <EmptyState title="No policies yet" description={isUser
              ? "Policies assigned to you, and their new versions, appear here."
              : "Upload a policy to let Governix identify its category, identity and version."}
              action={!isUser && can("documents:upload") && <Link to="/documents/upload"><Button>Upload policy</Button></Link>} />
          ) : (
            <ul className="divide-y divide-line">
              {data.recent_changes.map((change) => (
                <li key={change.version_id} className="flex items-center justify-between px-5 py-3 text-sm">
                  <Link to={`/policies/${change.policy_id}`} className="font-medium text-brand-700 hover:underline">{change.policy_name}</Link>
                  <span className="flex items-center gap-4 text-muted">
                    <span className="font-medium text-ink">v{change.version_label}</span>
                    {hasRealEffectiveDate(change) && <span>Effective {formatDate(change.effective_from)}</span>}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>

        {!isUser && (
          <Card>
            <CardHeader title="Needs attention" />
            {isLoading ? <SkeletonRows rows={3} /> : !data?.attention.length ? (
              <EmptyState title="All clear" description="No documents are waiting for review or have failed." />
            ) : (
              <ul className="divide-y divide-line">
                {data.attention.map((item) => (
                  <li key={item.id} className="px-5 py-3 text-sm">
                    <div className="flex items-center justify-between gap-2">
                      <Link to={`/documents/${item.id}`} className="truncate font-medium text-brand-700 hover:underline">{item.title}</Link>
                      <StatusBadge status={item.status} />
                    </div>
                    {item.status === "failed" && <p className="mt-1 flex gap-1 text-xs text-bad-600"><AlertTriangle className="size-3.5 shrink-0" />{item.reason}</p>}
                  </li>
                ))}
              </ul>
            )}
          </Card>
        )}

        <Card className={isUser ? "xl:col-span-3" : "xl:col-span-3"}>
          <CardHeader title="Recent AI queries" subtitle="Every answer is grounded in verified evidence" />
          {!data?.recent_queries.length ? (
            <EmptyState title="No questions asked yet" action={can("ai:query") ? <Link to="/assistant"><Button variant="secondary"><Bot className="size-4" />Ask Governix</Button></Link> : undefined} />
          ) : (
            <ul className="divide-y divide-line">
              {data.recent_queries.map((query) => (
                <li key={query.id} className="flex items-center justify-between gap-4 px-5 py-2.5 text-sm">
                  <span className="truncate">{query.question}</span>
                  <span className="flex shrink-0 items-center gap-3 text-xs text-muted">
                    {query.status === "no_answer" ? <StatusBadge status="rejected" /> : null}
                    {formatDateTime(query.created_at)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </>
  );
}
