import { useQuery } from "@tanstack/react-query";
import { GitCompare, Link2, Loader2, RefreshCw, Trash2, UploadCloud } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";
import { api, ApiError, qs } from "../api/client";
import type { AuditEvent, CategoryVersion, Page, PolicyDetail, Version, VersionDetail, VersionSummary } from "../api/types";
import { PolicyAssignments } from "../components/Assignments";
import { BulkUploadModal } from "../components/BulkUpload";
import { DeletePolicyDialog } from "../components/DeletePolicy";
import { hasRealEffectiveDate, ScopeBadge, useCategoryName } from "../components/domain";
import {
  Badge, Button, Card, CardHeader, EmptyState, ErrorState, KeyValue, Menu, PageHeader, Select, SkeletonRows, StatusBadge,
  SuggestedBadge, Tabs,
} from "../components/ui";
import { VersionManager } from "../components/VersionManager";
import { cn, formatDate, formatDateTime, humanize } from "../lib/format";
import { useAuth, useToast } from "../store/hooks";

type Tab = "overview" | "summary" | "versions" | "changes" | "assignments" | "related" | "audit";

export function PolicyDetailPage() {
  const { id } = useParams();
  const [params, setParams] = useSearchParams();
  const tab = (params.get("tab") as Tab) ?? "overview";
  const { can, me } = useAuth();
  const categoryName = useCategoryName();
  const [adding, setAdding] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const navigate = useNavigate();
  const { data: policy, error, isLoading, refetch } = useQuery({
    queryKey: ["policy", id],
    queryFn: () => api.get<PolicyDetail>(`/policies/${id}`),
  });

  if (error) return <ErrorState error={error} onRetry={refetch} />;
  if (isLoading || !policy) return <Card><SkeletonRows rows={6} /></Card>;
  const current = policy.current_version;
  const active = policy.versions.filter((v) => v.status === "active");
  const canAddVersions = can("policies:manage") && can("documents:upload") && policy.status === "active";
  const isUser = me?.role === "department_user";

  return (
    <>
      <nav className="mb-3 flex items-center gap-1.5 text-sm text-muted" aria-label="Breadcrumb">
        <Link to="/policies" className="hover:text-ink">{isUser ? "My Policies" : me?.role === "org_admin" ? "Global Policies" : "Policies"}</Link>
        <span>/</span><span className="truncate text-ink">{policy.name}</span>
      </nav>
      <PageHeader
        title={policy.name}
        subtitle={[policy.policy_number && `Policy No: ${policy.policy_number}`, categoryName(policy.category_id)].filter(Boolean).join(" · ")}
        actions={<>
          <ScopeBadge branchId={policy.branch_id} />
          <StatusBadge status={policy.status} />
          {current && <Badge tone="brand">Latest v{current.version_label}{hasRealEffectiveDate(current) && ` · effective ${formatDate(current.effective_from)}`}</Badge>}
          {canAddVersions && <Button variant="secondary" onClick={() => setAdding(true)}><UploadCloud className="size-4" />Add Version</Button>}
          {active.length > 1 && <Link to={`/policies/${policy.id}/compare`}><Button variant="secondary"><GitCompare className="size-4" />Compare Versions</Button></Link>}
          {can("policies:manage") && (
            <Menu label="Policy actions" items={[
              { label: "Delete policy…", icon: <Trash2 className="size-4" />, danger: true, onSelect: () => setDeleting(true) },
            ]} />
          )}
        </>}
      />
      <Tabs<Tab>
        value={tab}
        onChange={(next) => setParams({ tab: next }, { replace: true })}
        tabs={[
          { id: "overview", label: "Overview" },
          { id: "summary", label: "AI Summary" },
          { id: "versions", label: "Versions", count: policy.versions.length },
          { id: "changes", label: "What changed" },
          ...(can("policies:assign") ? [{ id: "assignments" as Tab, label: "Assignments" }] : []),
          { id: "related", label: "Related", count: policy.incoming_relationships.length + policy.outgoing_relationships.length },
          ...(can("audit:read") ? [{ id: "audit" as Tab, label: "Audit" }] : []),
        ]}
      />
      <div className="mt-6">
        {tab === "overview" && (
          <Card>
            <CardHeader title="Overview" />
            <div className="space-y-6 p-5">
              <KeyValue items={[
                ["Description", policy.description],
                ["Issuing department", policy.issuing_department ?? policy.issuer],
                ["Owner", policy.owner],
                ["Document number", policy.document_number],
                ["Latest version", current ? `v${current.version_label}` : "None in force"],
                ["Current effective period", current
                  ? hasRealEffectiveDate(current)
                    ? `${formatDate(current.effective_from)} – ${current.effective_to ? formatDate(current.effective_to) : "present"}`
                    : "No effective date stated (latest version by order)"
                  : "—"],
              ]} />
              {current && (
                <Link to={`/documents/${current.document_id}/view`}><Button variant="secondary">Open latest document</Button></Link>
              )}
            </div>
          </Card>
        )}
        {tab === "summary" && <SummaryTab policy={policy} />}
        {tab === "versions" && <VersionsTab policy={policy} onAdd={canAddVersions ? () => setAdding(true) : undefined} />}
        {tab === "changes" && <ChangesTab policy={policy} />}
        {tab === "assignments" && <PolicyAssignments policy={policy} />}
        {tab === "related" && <RelatedTab policy={policy} />}
        {tab === "audit" && <PolicyAudit policy={policy} />}
      </div>
      <DeletePolicyDialog policy={policy} open={deleting} onClose={() => setDeleting(false)} onDeleted={() => navigate("/policies")} />
      <BulkUploadModal open={adding} onClose={() => setAdding(false)} title={`New versions of ${policy.name}`}
        link={`/policies/${policy.id}?tab=versions`} policy={{ id: policy.id, name: policy.name }} />
    </>
  );
}

function Timeline({ versions }: { versions: Version[] }) {
  const active = versions.filter((v) => v.status === "active");
  return (
    <div className="overflow-x-auto pb-2">
      <ol className="flex min-w-fit items-start">
        {active.map((version, index) => (
          <li key={version.id} className="flex items-start">
            <div className="flex w-32 flex-col items-center text-center">
              <span className={cn(
                "flex size-10 items-center justify-center rounded-full border-2 text-sm font-semibold",
                version.timeline_state === "current" ? "border-ok-600 bg-ok-50 text-ok-600"
                  : version.timeline_state === "scheduled" ? "border-info-600 bg-info-50 text-info-600"
                  : "border-line-strong bg-surface text-ink-soft",
              )}>v{version.version_label}</span>
              <span className="mt-1.5 text-xs text-muted">{hasRealEffectiveDate(version) ? formatDate(version.effective_from) : "Date not stated"}</span>
              {version.timeline_state === "current" && <span className="mt-1 text-[11px] font-semibold uppercase text-ok-600">Latest</span>}
              {version.timeline_state === "scheduled" && <span className="mt-1 text-[11px] font-semibold uppercase text-info-600">Scheduled</span>}
            </div>
            {index < active.length - 1 && <span className="mt-5 h-0.5 w-10 bg-line-strong" />}
          </li>
        ))}
      </ol>
    </div>
  );
}

function VersionsTab({ policy, onAdd }: { policy: PolicyDetail; onAdd?: () => void }) {
  const { can } = useAuth();
  const versions = useQuery({
    queryKey: ["policy-versions", policy.id],
    queryFn: () => api.get<CategoryVersion[]>(`/policies/${policy.id}/versions`),
  });
  return (
    <div className="space-y-6">
      {policy.versions.some((v) => v.status === "active") && <Card className="p-5"><Timeline versions={policy.versions} /></Card>}
      <Card className="overflow-hidden">
        <CardHeader title="Versions" subtitle="Newest first. Withdraw a version to keep it in the history, or delete it permanently from its menu."
          actions={onAdd && <Button size="sm" onClick={onAdd}><UploadCloud className="size-3.5" />Add Version</Button>} />
        {versions.error ? <div className="p-4"><ErrorState error={versions.error} onRetry={versions.refetch} /></div>
          : versions.isLoading ? <SkeletonRows rows={3} />
          : !versions.data?.length ? <EmptyState title="No versions yet" description="Upload this policy's documents to add its versions."
              action={onAdd && <Button onClick={onAdd}><UploadCloud className="size-4" />Add Version</Button>} />
          : <VersionManager policy={policy} versions={versions.data} canManage={can("policies:manage")} onChanged={() => void versions.refetch()} />}
      </Card>
    </div>
  );
}

function SummaryTab({ policy }: { policy: PolicyDetail }) {
  const { can } = useAuth();
  const toast = useToast();
  const choices = [...policy.versions].reverse().filter((v) => v.status === "active");
  const [selected, setSelected] = useState(policy.current_version?.id ?? choices[0]?.id);
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["summary", selected],
    queryFn: () => api.get<VersionSummary>(`/policies/${policy.id}/versions/${selected}/summary`),
    enabled: Boolean(selected),
    refetchInterval: (query) => (query.state.data?.status === "pending" ? 5000 : false),
  });
  if (!choices.length) return <Card><EmptyState title="No version yet" description="The AI summary appears once the policy has a version." /></Card>;

  async function regenerate() {
    try {
      await api.post(`/policies/${policy.id}/versions/${selected}/summary`);
      toast("info", "Regenerating the summary in the background.");
    } catch (err) {
      toast("bad", err instanceof ApiError ? err.message : "Could not regenerate the summary.");
    }
  }

  const list = (title: string, items?: { text: string; section: string | null }[]) => !!items?.length && (
    <section>
      <h3 className="mb-2 text-sm font-semibold">{title}</h3>
      <ul className="space-y-1.5 text-sm">
        {items.map((item, index) => (
          <li key={index} className="flex gap-2">
            <span className="mt-2 size-1.5 shrink-0 rounded-full bg-brand-500" />
            <span>{item.text}{item.section && <span className="ml-1 text-xs text-muted">(section {item.section})</span>}</span>
          </li>
        ))}
      </ul>
    </section>
  );

  return (
    <Card>
      <CardHeader
        title={<span className="flex items-center gap-2">AI Summary <SuggestedBadge label="AI-generated" /></span>}
        subtitle="Generated from this version's text only. Dates, the version and the list of changes come from the records, not the AI."
        actions={<div className="flex items-center gap-2">
          <Select className="h-8 w-44" value={selected} onChange={(e) => setSelected(e.target.value)} aria-label="Version">
            {choices.map((v) => <option key={v.id} value={v.id}>v{v.version_label}{v.timeline_state === "current" ? " (latest)" : ""}</option>)}
          </Select>
          {can("policies:manage") && <Button size="sm" variant="ghost" onClick={regenerate}><RefreshCw className="size-3.5" />Regenerate</Button>}
        </div>}
      />
      <div className="space-y-6 p-5">
        {error ? <ErrorState error={error} onRetry={refetch} />
          : isLoading || !data ? <SkeletonRows rows={4} />
          : (
            <>
              <KeyValue items={[
                ["Policy version", `v${data.version_label}`],
                ["Effective date", hasRealEffectiveDate(data) ? formatDate(data.effective_from) : "Not stated in the document"],
                ["Expiry date", data.effective_to ? formatDate(data.effective_to) : "No expiry"],
                ["Applicable to", data.applicable_to ?? "Not stated"],
              ]} />
              {data.status === "pending" ? (
                <p className="flex items-center gap-2 rounded-md bg-subtle px-3 py-3 text-sm text-ink-soft"><Loader2 className="size-4 animate-spin" />The summary is being generated. This page updates by itself.</p>
              ) : (
                <>
                  {data.overview && <section><h3 className="mb-1 text-sm font-semibold">Overview</h3><p className="text-sm leading-relaxed text-ink-soft">{data.overview}</p></section>}
                  {data.purpose && <section><h3 className="mb-1 text-sm font-semibold">Purpose</h3><p className="text-sm text-ink-soft">{data.purpose}</p></section>}
                  {list("Key rules", data.key_rules)}
                  {list("What you need to do", data.user_actions)}
                  {list("Important exceptions", data.exceptions)}
                </>
              )}
              {!!data.important_changes.length && (
                <section className="rounded-md border border-line p-4">
                  <h3 className="mb-2 text-sm font-semibold">Important changes from the previous version</h3>
                  {data.change_summary && <p className="mb-2 text-sm text-ink-soft">{data.change_summary} <SuggestedBadge label="AI-generated" /></p>}
                  <ul className="space-y-1 text-sm">
                    {data.important_changes.map((line) => {
                      const [kind, ...rest] = line.split(": ");
                      return <li key={line} className="flex gap-2"><Badge tone={kind === "Added" ? "ok" : kind === "Removed" ? "bad" : "warn"}>{kind}</Badge><span>{rest.join(": ")}</span></li>;
                    })}
                  </ul>
                  <p className="mt-2 text-xs text-muted">Computed by comparing the documents, not by AI.</p>
                </section>
              )}
            </>
          )}
      </div>
    </Card>
  );
}

function ChangesTab({ policy }: { policy: PolicyDetail }) {
  const withChanges = [...policy.versions].reverse().filter((v) => v.supersedes_version_id && v.status === "active");
  const [selected, setSelected] = useState(withChanges[0]?.id);
  const { data, isLoading } = useQuery({
    queryKey: ["version", selected],
    queryFn: () => api.get<VersionDetail>(`/policies/${policy.id}/versions/${selected}`),
    enabled: Boolean(selected),
  });
  if (!withChanges.length) return <Card><EmptyState title="No changes yet" description="Changes appear when a policy has more than one version." /></Card>;
  const summary = data?.change_summary;
  return (
    <Card>
      <CardHeader
        title="Version changes"
        subtitle="Computed deterministically from the documents (not by AI)"
        actions={<select className="h-8 rounded-md border border-line-strong px-2 text-sm" value={selected} onChange={(e) => setSelected(e.target.value)}>
          {withChanges.map((v) => <option key={v.id} value={v.id}>Changes in v{v.version_label}</option>)}
        </select>}
      />
      <div className="p-5">
        {isLoading || !summary ? <SkeletonRows rows={3} /> : (
          <>
            <p className="mb-3 text-sm font-medium">Version {summary.from_version.label} → Version {summary.to_version.label}</p>
            {summary.summary_lines.length ? (
              <ul className="space-y-1.5 text-sm">
                {summary.summary_lines.map((line) => {
                  const [kind, ...rest] = line.split(": ");
                  const tone = kind === "Added" ? "ok" : kind === "Removed" ? "bad" : "warn";
                  return <li key={line} className="flex gap-2"><Badge tone={tone}>{kind}</Badge><span>{rest.join(": ")}</span></li>;
                })}
              </ul>
            ) : <p className="text-sm text-muted">No differences found.</p>}
            <Link to={`/policies/${policy.id}/compare?base=${summary.from_version.id}&target=${summary.to_version.id}`}>
              <Button variant="secondary" className="mt-4"><GitCompare className="size-4" />Side-by-side comparison</Button>
            </Link>
          </>
        )}
      </div>
    </Card>
  );
}

function RelatedTab({ policy }: { policy: PolicyDetail }) {
  const rows = [
    ...policy.incoming_relationships.map((r) => ({ ...r, direction: "incoming" as const })),
    ...policy.outgoing_relationships.map((r) => ({ ...r, direction: "outgoing" as const })),
  ];
  if (!rows.length) return <Card><EmptyState icon={<Link2 className="size-6" />} title="No related documents" description="Amending circulars, superseded documents and references appear here once confirmed." /></Card>;
  return (
    <Card>
      <ul className="divide-y divide-line">
        {rows.map((r) => (
          <li key={r.id} className="flex items-start justify-between gap-4 px-5 py-3 text-sm">
            <div>
              {r.direction === "incoming" ? (
                <p><Link className="font-medium text-brand-700 hover:underline" to={`/documents/${r.source_document_id}`}>{r.source_title}</Link>{" "}
                  <Badge tone="brand">{r.relation_type}</Badge> this policy{r.clauses.length ? ` (clause ${r.clauses.join(", ")})` : ""}</p>
              ) : (
                <p>This policy <Badge tone="brand">{r.relation_type}</Badge>{" "}
                  <Link className="font-medium text-brand-700 hover:underline" to={`/policies/${r.target_policy_id}`}>{r.target_policy_name}</Link></p>
              )}
              {r.evidence && <p className="mt-1 text-xs italic text-muted">“{r.evidence}”</p>}
            </div>
            <span className="shrink-0 text-xs text-muted">{formatDate(r.created_at)}</span>
          </li>
        ))}
      </ul>
    </Card>
  );
}

function PolicyAudit({ policy }: { policy: PolicyDetail }) {
  const { data, error } = useQuery({
    queryKey: ["audit", "policy", policy.id],
    queryFn: () => api.get<Page<AuditEvent>>(`/audit-events${qs({ resource_id: policy.id, limit: 100 })}`),
  });
  const versions = useQuery({
    queryKey: ["audit", "policy-versions", policy.id],
    queryFn: () => Promise.all(policy.versions.map((v) =>
      api.get<Page<AuditEvent>>(`/audit-events${qs({ resource_id: v.id, limit: 50 })}`))),
  });
  if (error) return <ErrorState error={error} />;
  const events = [...(data?.items ?? []), ...(versions.data?.flatMap((p) => p.items) ?? [])]
    .sort((a, b) => b.created_at.localeCompare(a.created_at));
  return (
    <Card>
      {!events.length ? <EmptyState title="No audit events" /> : (
        <ul className="divide-y divide-line">
          {events.map((e) => (
            <li key={e.id} className="flex justify-between gap-4 px-5 py-3 text-sm">
              <span>{humanize(e.action)}{e.details?.label ? ` · v${e.details.label}` : ""}{e.details?.reason ? ` — ${e.details.reason}` : ""}</span>
              <span className="text-xs text-muted">{formatDateTime(e.created_at)}</span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
