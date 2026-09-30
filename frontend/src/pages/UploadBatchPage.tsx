import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, FileText, Layers, Loader2, XCircle } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { api } from "../api/client";
import type { UploadBatch, UploadBatchItem, UploadBatchSummary } from "../api/types";
import { useCategoryName } from "../components/domain";
import { Badge, Button, Card, EmptyState, ErrorState, Modal, PageHeader, ProgressBar, SkeletonRows } from "../components/ui";
import { formatDate, formatDateTime } from "../lib/format";

const BATCH_LABELS: Record<UploadBatch["status"], [string, "info" | "ok" | "warn"]> = {
  uploading: ["Receiving files", "info"],
  processing: ["Reading and registering", "info"],
  completed: ["Completed", "ok"],
  attention: ["Needs your attention", "warn"],
};

const ITEM_LABELS: Record<UploadBatchItem["status"], string> = {
  awaiting_file: "Waiting for the file",
  processing: "Reading the document",
  confirmed: "Registered",
  needs_review: "Needs review",
  failed: "Failed",
  cancelled: "Cancelled",
};

function ItemIcon({ status }: { status: UploadBatchItem["status"] }) {
  if (status === "confirmed") return <CheckCircle2 className="size-4 text-ok-600" />;
  if (status === "needs_review") return <AlertTriangle className="size-4 text-warn-600" />;
  if (status === "failed") return <XCircle className="size-4 text-bad-600" />;
  if (status === "cancelled") return <XCircle className="size-4 text-muted" />;
  return <Loader2 className="size-4 animate-spin text-info-600" />;
}

export function UploadBatchPage() {
  const { id } = useParams();
  const categoryName = useCategoryName();
  const { data: batch, error, isLoading, refetch } = useQuery({
    queryKey: ["upload-batch", id],
    queryFn: () => api.get<UploadBatch>(`/uploads/batches/${id}`),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "completed" || status === "attention" ? false : 2000;
    },
  });
  // Finishing while the page is open pops up a summary (not when opening an already finished upload).
  const [finishedNow, setFinishedNow] = useState(false);
  const previous = useRef<string | undefined>(undefined);
  useEffect(() => {
    const status = batch?.status;
    if ((status === "completed" || status === "attention") && (previous.current === "uploading" || previous.current === "processing")) {
      setFinishedNow(true);
    }
    previous.current = status;
  }, [batch?.status]);

  if (error) return <ErrorState error={error} onRetry={refetch} />;
  if (isLoading || !batch) return <SkeletonRows />;

  const total = batch.counts.total || 1;
  const settled = (batch.counts.confirmed ?? 0) + (batch.counts.needs_review ?? 0) + (batch.counts.failed ?? 0);
  const [label, tone] = BATCH_LABELS[batch.status];

  return (
    <>
      <FinishedModal batch={batch} open={finishedNow} onClose={() => setFinishedNow(false)} />
      <PageHeader
        title="Bulk upload"
        subtitle={`Started ${formatDateTime(batch.created_at)}${batch.completed_at ? ` · finished ${formatDateTime(batch.completed_at)}` : ""}`}
        actions={<>
          <Link to="/uploads"><Button variant="secondary">All uploads</Button></Link>
          <Link to="/documents/upload"><Button>Upload more</Button></Link>
        </>}
      />
      <Card className="mb-5 p-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <Badge tone={tone}>{label}</Badge>
          <p className="text-sm text-muted">
            <span className="font-medium text-ink">{batch.counts.confirmed ?? 0}</span> registered ·{" "}
            <span className="font-medium text-ink">{batch.counts.needs_review ?? 0}</span> need review ·{" "}
            <span className="font-medium text-ink">{batch.counts.failed ?? 0}</span> failed · {batch.counts.total} files
          </p>
        </div>
        <div className="mt-3"><ProgressBar value={(settled / total) * 100} tone={batch.status === "completed" ? "ok" : "brand"} /></div>
        {batch.status === "attention" && (
          <p className="mt-3 text-sm text-ink-soft">Files that need review were not registered automatically. Open each one, confirm or correct the suggestion, and this upload completes.</p>
        )}
      </Card>

      <div className="space-y-4">
        {batch.groups.map((group) => (
          <Card key={group.id} className="overflow-hidden">
            <div className="flex flex-wrap items-center gap-3 border-b border-line bg-subtle/60 px-4 py-3">
              <Layers className="size-4 text-brand-600" />
              <div className="min-w-0 flex-1">
                {group.policy_id ? (
                  <Link to={`/policies/${group.policy_id}`} className="font-medium text-brand-700 hover:underline">
                    {group.policy_name ?? group.new_policy_name ?? "Policy"}
                  </Link>
                ) : (
                  <span className="font-medium">{group.new_policy_name ?? "New policy (name from the document)"}</span>
                )}
                <p className="text-xs text-muted">{group.category_id ? categoryName(group.category_id) : "Category detected from the document"} · {group.items.length} version{group.items.length === 1 ? "" : "s"}, oldest first</p>
              </div>
              <Badge tone={group.status === "confirmed" ? "ok" : group.status === "attention" ? "warn" : "info"}>
                {group.status === "confirmed" ? "Registered" : group.status === "attention" ? "Needs attention" : "In progress"}
              </Badge>
            </div>
            <ol className="divide-y divide-line">
              {group.items.map((item) => (
                <li key={item.id} className="flex flex-wrap items-center gap-3 px-4 py-3 text-sm">
                  <ItemIcon status={item.status} />
                  <FileText className="size-4 text-muted" />
                  <span className="min-w-0 flex-1 truncate" title={item.original_filename}>{item.original_filename}</span>
                  {item.version_label && <Badge>v{item.version_label}</Badge>}
                  {item.status === "confirmed" && item.effective_from && <span className="text-xs text-muted">from {formatDate(item.effective_from)}</span>}
                  <span className="w-40 text-right text-xs text-muted">{ITEM_LABELS[item.status]}</span>
                  {item.document_id && (item.status === "needs_review" || item.status === "failed") && (
                    <Link to={`/documents/${item.document_id}`}><Button size="sm" variant={item.status === "needs_review" ? "primary" : "secondary"}>{item.status === "needs_review" ? "Review" : "Details"}</Button></Link>
                  )}
                  {item.message && <p className="w-full pl-8 text-xs text-ink-soft">{item.message}</p>}
                </li>
              ))}
            </ol>
          </Card>
        ))}
      </div>
    </>
  );
}

/** How the upload ended, with the way to its policies (or to what needs review). */
function FinishedModal({ batch, open, onClose }: { batch: UploadBatch; open: boolean; onClose: () => void }) {
  const navigate = useNavigate();
  const go = (to: string) => { onClose(); navigate(to); };
  const { confirmed = 0, needs_review: review = 0, failed = 0 } = batch.counts;
  const clean = batch.status === "completed" && !failed;
  const policies = batch.groups.filter((g) => g.policy_id);
  const toReview = batch.groups.flatMap((g) => g.items).find((i) => i.status === "needs_review" && i.document_id);
  const only = policies.length === 1 ? policies[0] : null;
  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Upload finished"
      footer={<>
        {toReview && <Button variant={clean ? "secondary" : "primary"} onClick={() => go(`/documents/${toReview.document_id}`)}>Review</Button>}
        {only
          ? <Button variant={toReview ? "secondary" : "primary"} onClick={() => go(`/policies/${only.policy_id}?tab=versions`)}>Open policy</Button>
          : <Button variant="secondary" onClick={onClose}>Close</Button>}
      </>}
    >
      <div className={`flex items-start gap-3 rounded-md p-3 text-sm ${clean ? "bg-ok-50 text-ok-600" : "bg-warn-50 text-warn-600"}`}>
        {clean ? <CheckCircle2 className="mt-0.5 size-5 shrink-0" /> : <AlertTriangle className="mt-0.5 size-5 shrink-0" />}
        <p className="font-medium">
          {clean
            ? `${confirmed === 1 ? "The document is" : `All ${confirmed} documents are`} uploaded and registered.`
            : [confirmed && `${confirmed} registered`, review && `${review} need${review === 1 ? "s" : ""} your review`, failed && `${failed} failed`]
              .filter(Boolean).join(" · ")}
        </p>
      </div>
      {policies.length > 1 && (
        <ul className="mt-4 divide-y divide-line rounded-md border border-line">
          {policies.map((group) => (
            <li key={group.id} className="flex items-center gap-3 px-3 py-2 text-sm">
              <Layers className="size-4 shrink-0 text-brand-600" />
              <span className="min-w-0 flex-1 truncate">{group.policy_name ?? group.new_policy_name ?? "Policy"}</span>
              <Button size="sm" variant="secondary" onClick={() => go(`/policies/${group.policy_id}?tab=versions`)}>Open</Button>
            </li>
          ))}
        </ul>
      )}
    </Modal>
  );
}

export function UploadBatchesPage() {
  const { data, error, isLoading, refetch } = useQuery({
    queryKey: ["upload-batches"],
    queryFn: () => api.get<UploadBatchSummary[]>("/uploads/batches"),
    refetchInterval: 5000,
  });
  return (
    <>
      <PageHeader title="Recent uploads" subtitle="Bulk uploads and how far each one got" actions={<Link to="/documents/upload"><Button>Upload documents</Button></Link>} />
      <Card>
        {error ? <div className="p-4"><ErrorState error={error} onRetry={refetch} /></div>
          : isLoading ? <SkeletonRows />
          : !data?.length ? <EmptyState title="No bulk uploads yet" description="Drop several PDFs on the upload page to register policies and their versions together." />
          : (
            <ul className="divide-y divide-line">
              {data.map((batch) => {
                const [label, tone] = BATCH_LABELS[batch.status];
                return (
                  <li key={batch.id} className="flex flex-wrap items-center justify-between gap-3 px-5 py-4">
                    <div>
                      <Link to={`/uploads/${batch.id}`} className="font-medium text-brand-700 hover:underline">{batch.counts.total} file{batch.counts.total === 1 ? "" : "s"}</Link>
                      <p className="text-xs text-muted">{formatDateTime(batch.created_at)}</p>
                    </div>
                    <p className="text-sm text-muted">{batch.counts.confirmed ?? 0} registered · {batch.counts.needs_review ?? 0} need review · {batch.counts.failed ?? 0} failed</p>
                    <Badge tone={tone}>{label}</Badge>
                  </li>
                );
              })}
            </ul>
          )}
      </Card>
    </>
  );
}
