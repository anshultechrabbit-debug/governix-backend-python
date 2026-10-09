import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Archive, ArrowRight, BookOpen, CheckCircle2, Download, Loader2, RotateCcw, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { api, ApiError } from "../api/client";
import type { DocumentDetail } from "../api/types";
import { AnalysisReview } from "../components/AnalysisReview";
import { canDelete, DeleteDocumentDialog } from "../components/DeleteDocument";
import { StageList, useCategoryName } from "../components/domain";
import { Button, Card, CardHeader, ErrorState, KeyValue, PageHeader, SkeletonRows, StatusBadge } from "../components/ui";
import { formatBytes, formatDateTime, humanize } from "../lib/format";
import { semanticIndexing, useAnalysis, useDocument } from "../hooks";
import { useAuth, useToast } from "../store/hooks";

export function DocumentDetailPage() {
  const { id } = useParams();
  const { can } = useAuth();
  const toast = useToast();
  const queryClient = useQueryClient();
  const { data: document, error, isLoading, refetch } = useDocument(id);
  const reviewable = Boolean(document && ["awaiting_confirmation", "indexing", "ready"].includes(document.status));
  const analysis = useAnalysis(id, reviewable);
  const categoryName = useCategoryName();
  const navigate = useNavigate();
  const [deleting, setDeleting] = useState(false);

  // Celebrate only a document seen finishing here, not one opened when already ready.
  const [justReady, setJustReady] = useState(false);
  const lastStatus = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (!document) return;
    if (lastStatus.current && lastStatus.current !== "ready" && document.status === "ready") setJustReady(true);
    lastStatus.current = document.status;
  }, [document]);

  const act = useMutation({
    mutationFn: (action: "retry" | "archive") => api.post(`/documents/${id}/${action}`),
    onSuccess: (_d, action) => {
      toast("ok", action === "retry" ? "Processing restarted." : "Document archived.");
      queryClient.invalidateQueries({ queryKey: ["document", id] });
    },
    onError: (err) => toast("bad", err instanceof ApiError ? err.message : "Action failed."),
  });

  async function download() {
    const { url } = await api.blobUrl(`/documents/${id}/file`);
    window.open(url, "_blank", "noopener");
  }

  if (error) return <ErrorState error={error} onRetry={refetch} />;
  if (isLoading || !document) return <Card><SkeletonRows rows={6} /></Card>;

  return (
    <>
      <DeleteDocumentDialog document={document} open={deleting} onClose={() => setDeleting(false)} onDeleted={() => navigate("/documents")} />
      {justReady && <ReadyDialog document={document} category={categoryName(document.category_id)} onStay={() => setJustReady(false)} />}
      <PageHeader
        title={<span className="flex flex-wrap items-center gap-x-3 gap-y-1"><span className="min-w-0 [overflow-wrap:anywhere]">{document.title ?? document.original_filename}</span><StatusBadge status={document.filed_by_batch ? "processing" : document.status} /></span>}
        subtitle={document.title ? document.original_filename : "Name pending confirmation"}
        actions={<>
          {document.page_count && <Link to={`/documents/${document.id}/view`}><Button variant="secondary"><BookOpen className="size-4" />Open viewer</Button></Link>}
          <Button variant="secondary" onClick={download}><Download className="size-4" />Download</Button>
          {document.status === "failed" && can("documents:upload") && (
            <Button onClick={() => act.mutate("retry")} loading={act.isPending}><RotateCcw className="size-4" />Retry</Button>
          )}
          {!["archived", "rejected"].includes(document.status) && can("documents:upload") && (
            <Button variant="ghost" onClick={() => act.mutate("archive")}><Archive className="size-4" />Archive</Button>
          )}
          {can("documents:upload") && canDelete(document) && (
            <Button variant="ghost" className="text-bad-600 hover:bg-bad-50" onClick={() => setDeleting(true)}><Trash2 className="size-4" />Delete</Button>
          )}
        </>}
      />

      {document.status === "failed" && document.error && (
        <div className="mb-6 flex gap-3 rounded-lg border border-bad-600/20 bg-bad-50 p-4 text-sm">
          <AlertTriangle className="size-5 shrink-0 text-bad-600" />
          <div>
            <p className="font-medium text-bad-600">Document processing failed</p>
            <p className="mt-1 text-ink-soft">Reason: {document.error.message}</p>
            <p className="mt-1 text-xs text-muted">Stage: {humanize(document.error.stage ?? "")} · Retry after the cause is fixed, or contact your administrator.</p>
          </div>
        </div>
      )}

      <div className="grid gap-6 xl:grid-cols-3">
        <div className="space-y-6 xl:col-span-2">
          {document.status === "awaiting_confirmation" && document.filed_by_batch && <FiledByBatch batchId={document.upload_batch_id} />}
          {document.status === "awaiting_confirmation" && !document.filed_by_batch && analysis.data && <AnalysisReview document={document} analysis={analysis.data} />}
          <Card>
            <CardHeader title="Details" />
            <div className="p-4 sm:p-5">
              <KeyValue items={[
                ["Category", categoryName(document.category_id)],
                ["Policy", document.policy_id ? <Link className="text-brand-700 hover:underline" to={`/policies/${document.policy_id}`}>Open policy</Link> : "Not yet confirmed"],
                ["Pages", document.page_count?.toLocaleString()],
                ["Size", formatBytes(document.size_bytes)],
                ["Uploaded", formatDateTime(document.created_at)],
                ["Ready", formatDateTime(document.ready_at)],
                ["SHA-256", <code className="break-all text-xs">{document.file_sha256}</code>],
                ["Duplicate override", document.duplicate_override_reason],
              ]} />
            </div>
          </Card>
        </div>
        <Card className="h-fit">
          <CardHeader title="Processing" subtitle="Live status from the ingestion pipeline" />
          <div className="p-4 sm:p-5">{document.progress && <StageList progress={document.progress} />}</div>
        </Card>
      </div>
    </>
  );
}

/** The bulk upload files this document with the other files of its group as soon as they are all read. */
function FiledByBatch({ batchId }: { batchId: string | null }) {
  return (
    <Card>
      <div className="flex items-start gap-3 p-4 sm:p-5 text-sm">
        <Loader2 className="mt-0.5 size-4 shrink-0 animate-spin text-brand-500" />
        <div>
          <p className="font-medium">Filing with its bulk upload</p>
          <p className="mt-1 text-muted">
            This file is registered automatically, as a version of its group's policy, once the other files of the group
            are read. Nothing to confirm here.
            {batchId && <> <Link className="text-brand-700 hover:underline" to={`/uploads/${batchId}`}>Open the upload</Link></>}
          </p>
        </div>
      </div>
    </Card>
  );
}

const REDIRECT_SECONDS = 5;

function ReadyDialog({ document, category, onStay }: { document: DocumentDetail; category: string; onStay: () => void }) {
  const navigate = useNavigate();
  const [left, setLeft] = useState(REDIRECT_SECONDS);
  // The page re-renders while it polls; keep one timer for the dialog's lifetime.
  const stay = useRef(onStay);
  stay.current = onStay;
  useEffect(() => {
    const tick = window.setInterval(() => setLeft((seconds) => seconds - 1), 1000);
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && stay.current();
    window.addEventListener("keydown", onKey);
    return () => { window.clearInterval(tick); window.removeEventListener("keydown", onKey); };
  }, []);
  useEffect(() => { if (left <= 0) navigate("/documents"); }, [left, navigate]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 p-4 backdrop-blur-[2px] motion-safe:animate-[fade-in_150ms_ease-out]" onMouseDown={onStay}>
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="ready-title"
        className="w-full max-w-md overflow-hidden rounded-xl bg-surface text-center shadow-2xl motion-safe:animate-[pop-in_220ms_cubic-bezier(.2,.9,.3,1.2)]"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="px-6 pb-5 pt-7">
          <div className="mx-auto flex size-14 items-center justify-center rounded-full bg-ok-50 ring-8 ring-ok-50/50">
            <CheckCircle2 className="size-8 text-ok-600" />
          </div>
          <h2 id="ready-title" className="mt-4 text-lg font-semibold tracking-tight">Upload complete</h2>
          <p className="mt-1 text-sm text-muted">
            {semanticIndexing(document)
              ? "Your document is ready to search and ask about. Semantic search keeps improving in the background while embeddings finish."
              : "Your document is processed, indexed and ready to search and ask about."}
          </p>

          <div className="mt-5 rounded-lg border border-line bg-subtle/50 px-4 py-3 text-left">
            <p className="truncate text-sm font-medium text-ink" title={document.title ?? document.original_filename}>
              {document.title ?? document.original_filename}
            </p>
            <p className="mt-0.5 truncate text-xs text-muted">
              {[document.title ? document.original_filename : null, category, document.page_count ? `${document.page_count.toLocaleString()} pages` : null]
                .filter(Boolean).join(" · ")}
            </p>
          </div>
        </div>

        <div className="border-t border-line px-6 py-4">
          <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
            <Button variant="secondary" onClick={onStay}>Stay on this page</Button>
            <Button autoFocus onClick={() => navigate("/documents")}>Go to Documents<ArrowRight className="size-4" /></Button>
          </div>
          <p className="mt-3 text-xs text-muted" aria-live="polite">Taking you to Documents in {Math.max(left, 0)}s</p>
          <div className="mt-2 h-1 overflow-hidden rounded-full bg-subtle">
            <div
              className="h-full rounded-full bg-brand-600 transition-[width] duration-1000 ease-linear"
              style={{ width: `${((REDIRECT_SECONDS - Math.max(left, 0)) / REDIRECT_SECONDS) * 100}%` }}
            />
          </div>
        </div>
      </div>
    </div>
  );
}
