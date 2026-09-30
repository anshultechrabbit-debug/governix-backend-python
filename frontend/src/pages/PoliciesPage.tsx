import { useQuery } from "@tanstack/react-query";
import { ScrollText, Upload } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";
import { api, qs } from "../api/client";
import type { Page, Policy } from "../api/types";
import { hasRealEffectiveDate, ScopeBadge, useCategories, useCategoryName } from "../components/domain";
import { Button, Card, EmptyState, ErrorState, Input, PageHeader, Select, SkeletonRows, StatusBadge } from "../components/ui";
import { formatDate } from "../lib/format";
import { useAuth } from "../store/hooks";

export function PoliciesPage() {
  const { can, me } = useAuth();
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState("");
  const [status, setStatus] = useState("active");
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
            <ul className="divide-y divide-line">
              {data.items.map((policy) => (
                <li key={policy.id} className="flex flex-wrap items-center justify-between gap-4 px-5 py-4">
                  <div className="min-w-0">
                    <Link to={`/policies/${policy.id}`} className="font-medium text-brand-700 hover:underline">{policy.name}</Link>
                    <p className="mt-0.5 text-xs text-muted">
                      {[policy.policy_number && `Policy No: ${policy.policy_number}`, categoryName(policy.category_id)].filter(Boolean).join(" · ")}
                    </p>
                  </div>
                  <div className="flex items-center gap-4 text-sm">
                    <ScopeBadge branchId={policy.branch_id} />
                    <div className="text-right">
                      <p className="font-medium">{policy.current_version ? `Current: v${policy.current_version.version_label}` : "No version in force"}</p>
                      <p className="text-xs text-muted">
                        {policy.current_version && hasRealEffectiveDate(policy.current_version)
                          ? `Effective ${formatDate(policy.current_version.effective_from)} · ${policy.version_count} version${policy.version_count === 1 ? "" : "s"}`
                          : `${policy.version_count} version${policy.version_count === 1 ? "" : "s"}`}
                      </p>
                    </div>
                    <StatusBadge status={policy.status} />
                    <div className="flex gap-2">
                      <Link to={`/policies/${policy.id}`}><Button variant="secondary" size="sm">View</Button></Link>
                      {can("policies:manage") && policy.status === "active" && <Link to={`/documents/upload?policy=${policy.id}`}><Button variant="ghost" size="sm">Add versions</Button></Link>}
                      {policy.version_count > 1 && <Link to={`/policies/${policy.id}/compare`}><Button variant="ghost" size="sm">Compare</Button></Link>}
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          )}
      </Card>
    </>
  );
}
