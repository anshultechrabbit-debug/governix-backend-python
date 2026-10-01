import { useQuery } from "@tanstack/react-query";
import { FileText, Trash2, Upload } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";
import { api, qs } from "../api/client";
import type { DocumentRead, Page } from "../api/types";
import { canDelete, DeleteDocumentDialog } from "../components/DeleteDocument";
import { ScopeBadge, useCategories, useCategoryName } from "../components/domain";
import { Button, Card, EmptyState, ErrorState, Input, PageHeader, Select, SkeletonRows, StatusBadge, Table } from "../components/ui";
import { formatBytes, formatDate } from "../lib/format";
import { useAuth } from "../store/hooks";

const PAGE_SIZE = 25;

export function DocumentsPage() {
  const { can, me } = useAuth();
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [category, setCategory] = useState("");
  const [offset, setOffset] = useState(0);
  const [deleting, setDeleting] = useState<DocumentRead | null>(null);
  const categories = useCategories();
  const categoryName = useCategoryName();
  const params = { search, status, category_id: category, limit: PAGE_SIZE, offset };
  const { data, error, isLoading, refetch } = useQuery({
    queryKey: ["documents", params],
    queryFn: () => api.get<Page<DocumentRead>>(`/documents${qs(params)}`),
    refetchInterval: (query) =>
      query.state.data?.items.some((d) => ["uploaded", "processing", "indexing"].includes(d.status)) ? 3000 : false,
  });

  const isBranchManager = me?.role === "branch_manager";
  const subtitle = isBranchManager
    ? "Global documents from your organization, plus your own branch's documents."
    : me?.role === "org_admin"
      ? "Every source file registered in your organization."
      : "Every uploaded source file you can access.";

  return (
    <>
      <PageHeader
        title="Documents"
        subtitle={subtitle}
        actions={can("documents:upload") && <Link to="/documents/upload"><Button><Upload className="size-4" />Upload</Button></Link>}
      />
      {deleting && <DeleteDocumentDialog document={deleting} open onClose={() => setDeleting(null)} />}
      <Card>
        <div className="flex flex-wrap gap-3 border-b border-line p-4">
          <Input className="max-w-xs" placeholder="Search title or filename…" value={search} onChange={(e) => { setSearch(e.target.value); setOffset(0); }} />
          <Select className="max-w-[12rem]" value={status} onChange={(e) => { setStatus(e.target.value); setOffset(0); }}>
            <option value="">All statuses</option>
            {["ready", "awaiting_confirmation", "processing", "indexing", "failed", "archived", "rejected"].map((s) => (
              <option key={s} value={s}>{s.replace("_", " ")}</option>
            ))}
          </Select>
          <Select className="max-w-[14rem]" value={category} onChange={(e) => { setCategory(e.target.value); setOffset(0); }}>
            <option value="">All categories</option>
            {categories.data?.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </Select>
        </div>
        {error ? <div className="p-4"><ErrorState error={error} onRetry={refetch} /></div>
          : isLoading ? <SkeletonRows />
          : !data?.items.length ? (
            <EmptyState icon={<FileText className="size-6" />} title="No documents found"
              description="Upload a document to let Governix automatically identify its category, policy identity and version."
              action={can("documents:upload") && <Link to="/documents/upload"><Button>Upload document</Button></Link>} />
          ) : (
            <>
              <Table head={["Document", "Category", "Scope", "Status", "Pages", "Size", "Uploaded", ...(can("documents:upload") ? [<span className="sr-only">Actions</span>] : [])]}>
                {data.items.map((doc) => (
                  <tr key={doc.id} className="hover:bg-subtle/50">
                    <td className="px-4 py-3">
                      <Link to={`/documents/${doc.id}`} className="font-medium text-brand-700 hover:underline">{doc.title ?? doc.original_filename}</Link>
                      {doc.title && <p className="text-xs text-muted">{doc.original_filename}</p>}
                    </td>
                    <td className="px-4 py-3 text-ink-soft">{categoryName(doc.category_id)}</td>
                    <td className="px-4 py-3"><ScopeBadge branchId={doc.branch_id} /></td>
                    <td className="px-4 py-3"><StatusBadge status={doc.status} /></td>
                    <td className="px-4 py-3 tabular-nums">{doc.page_count?.toLocaleString() ?? "—"}</td>
                    <td className="px-4 py-3 tabular-nums text-ink-soft">{formatBytes(doc.size_bytes)}</td>
                    <td className="px-4 py-3 text-ink-soft">{formatDate(doc.created_at)}</td>
                    {can("documents:upload") && (
                      <td className="px-2 py-3 text-right">
                        {canDelete(doc) && (
                          <button
                            onClick={() => setDeleting(doc)}
                            className="rounded-md p-1.5 text-muted transition-colors hover:bg-bad-50 hover:text-bad-600"
                            aria-label={`Delete ${doc.title ?? doc.original_filename}`}
                            title="Delete"
                          ><Trash2 className="size-4" /></button>
                        )}
                      </td>
                    )}
                  </tr>
                ))}
              </Table>
              <div className="flex items-center justify-between border-t border-line px-4 py-3 text-sm text-muted">
                <span>{offset + 1}–{Math.min(offset + PAGE_SIZE, data.total)} of {data.total}</span>
                <div className="flex gap-2">
                  <Button variant="secondary" size="sm" disabled={offset === 0} onClick={() => setOffset(offset - PAGE_SIZE)}>Previous</Button>
                  <Button variant="secondary" size="sm" disabled={offset + PAGE_SIZE >= data.total} onClick={() => setOffset(offset + PAGE_SIZE)}>Next</Button>
                </div>
              </div>
            </>
          )}
      </Card>
    </>
  );
}
