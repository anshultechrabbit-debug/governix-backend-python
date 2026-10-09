import { useQuery } from "@tanstack/react-query";
import { GitCompare, ScrollText, Trash2, Upload, UploadCloud } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate } from "react-router";
import { api, qs } from "../api/client";
import type { Page, Policy } from "../api/types";
import { hasRealEffectiveDate, ScopeBadge, useCategories, useCategoryName } from "../components/domain";
import { DeletePolicyDialog } from "../components/DeletePolicy";
import { Badge, Button, Card, EmptyState, ErrorState, Input, Menu, PageHeader, Select, SkeletonRows, StatusBadge } from "../components/ui";
import { formatDate } from "../lib/format";
import { useAuth } from "../store/hooks";

export function PoliciesPage() {
  const { can, me } = useAuth();
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState("");
  const [status, setStatus] = useState("active");
  const [deleting, setDeleting] = useState<Policy | null>(null);
  const navigate = useNavigate();
  const categories = useCategories();
  const categoryName = useCategoryName();
  const params = { search, category_id: category, status, limit: 100 };
  const { data, error, isLoading, refetch } = useQuery({
    queryKey: ["policies", params],
    queryFn: () => api.get<Page<Policy>>(`/policies${qs(params)}`),
  });

  const isUser = me?.role === "department_user";
  const isBranchManager = me?.role === "branch_manager";
  const title = isUser ? "My Policies" : me?.role === "org_admin" ? "Global Policies" : "Policies";
  const subtitle = isUser
    ? "Only the policies assigned to you. The version in force is always used first."
    : isBranchManager
      ? "Global policies from your organization, plus your own branch's policies."
      : "Business identities and their versions";

  return (
    <>
      <PageHeader
        title={title}
        subtitle={subtitle}
        actions={can("documents:upload") && <Link to="/documents/upload"><Button><Upload className="size-4" />Upload</Button></Link>}
      />
      {isUser && (
        <p className="mb-4 rounded-md border border-brand-500/20 bg-brand-50 px-4 py-3 text-sm text-brand-700">
          You can ask Governix about any of these policies. Anything not assigned to you is never searched, even if it exists in the same organization.
        </p>
      )}
      {deleting && <DeletePolicyDialog policy={deleting} open onClose={() => setDeleting(null)} />}
      <Card>
        <div className="flex flex-wrap gap-3 border-b border-line p-4">
          <Input className="max-w-sm" placeholder="Search policies or policy number…" value={search} onChange={(e) => setSearch(e.target.value)} />
          <Select className="max-w-[14rem]" value={category} onChange={(e) => setCategory(e.target.value)}>
            <option value="">All categories</option>
            {categories.data?.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </Select>
          <Select className="max-w-[10rem]" value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="active">Active</option>
            <option value="archived">Archived</option>
            <option value="">All</option>
          </Select>
        </div>
        {error ? <div className="p-4"><ErrorState error={error} onRetry={refetch} /></div>
          : isLoading ? <SkeletonRows />
          : !data?.items.length ? (
            <EmptyState icon={<ScrollText className="size-6" />} title="No policies found"
              description={isUser
                ? "No policies are assigned to you yet. Your branch manager assigns the policies you can read."
                : "Upload a policy to let Governix automatically identify its category, policy identity and version."}
              action={!isUser && can("documents:upload") && <Link to="/documents/upload"><Button>Upload policy</Button></Link>} />
          ) : (
            <>
              <p className="border-b border-line px-4 sm:px-5 py-2 text-xs text-muted">{data.total} polic{data.total === 1 ? "y" : "ies"}</p>
              <ul className="divide-y divide-line">
                {data.items.map((policy) => {
                  const current = policy.current_version;
                  const versions = `${policy.version_count} version${policy.version_count === 1 ? "" : "s"}`;
                  return (
                    <li key={policy.id} className="group flex flex-col gap-3 px-4 sm:px-5 py-4 transition-colors hover:bg-subtle/40 sm:flex-row sm:items-center">
                      <div className="flex min-w-0 flex-1 items-start gap-3">
                        <div className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-brand-50 text-brand-700">
                          <ScrollText className="size-4.5" />
                        </div>
                        <div className="min-w-0">
                          <div className="flex flex-wrap items-center gap-2">
                            <Link to={`/policies/${policy.id}`} className="truncate font-medium text-ink hover:text-brand-700 hover:underline">{policy.name}</Link>
                            {policy.status !== "active" && <StatusBadge status={policy.status} />}
                          </div>
                          <p className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted">
                            {[policy.policy_number && `No. ${policy.policy_number}`, categoryName(policy.category_id)].filter(Boolean).join(" · ")}
                            <ScopeBadge branchId={policy.branch_id} />
                          </p>
                        </div>
                      </div>

                      <div className="flex items-center gap-3 pl-12 sm:pl-0">
                        <div className="text-left sm:text-right">
                          {current
                            ? <Badge tone="ok">v{current.version_label} in force</Badge>
                            : <Badge tone="warn">No version in force</Badge>}
                          <p className="mt-1 text-xs text-muted">
                            {current && hasRealEffectiveDate(current) ? `Since ${formatDate(current.effective_from)} · ${versions}` : versions}
                          </p>
                        </div>
                        <div className="ml-auto flex items-center gap-1 sm:ml-2">
                          <Link to={`/policies/${policy.id}`}><Button variant="secondary" size="sm">Open</Button></Link>
                          <Menu label={`Actions for ${policy.name}`} items={[
                            can("policies:manage") && policy.status === "active" && can("documents:upload") && {
                              label: "Add versions", icon: <UploadCloud className="size-4" />, onSelect: () => navigate(`/documents/upload?policy=${policy.id}`),
                            },
                            policy.version_count > 1 && {
                              label: "Compare versions", icon: <GitCompare className="size-4" />, onSelect: () => navigate(`/policies/${policy.id}/compare`),
                            },
                            can("policies:manage") && {
                              label: "Delete policy…", icon: <Trash2 className="size-4" />, danger: true, onSelect: () => setDeleting(policy),
                            },
                          ]} />
                        </div>
                      </div>
                    </li>
                  );
                })}
              </ul>
            </>
          )}
      </Card>
    </>
  );
}
