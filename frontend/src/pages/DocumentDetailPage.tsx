import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Archive, BookOpen, Download, RotateCcw } from "lucide-react";
import { Link, useParams } from "react-router";
import { api, ApiError } from "../api/client";
import { AnalysisReview } from "../components/AnalysisReview";
import { StageList, useCategoryName } from "../components/domain";
import { Button, Card, CardHeader, ErrorState, KeyValue, PageHeader, SkeletonRows, StatusBadge } from "../components/ui";
import { formatBytes, formatDateTime, humanize } from "../lib/format";
import { useAnalysis, useDocument } from "../hooks";
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
      <PageHeader
        title={<span className="flex items-center gap-3">{document.title ?? document.original_filename}<StatusBadge status={document.status} /></span>}
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
          {document.status === "awaiting_confirmation" && analysis.data && <AnalysisReview document={document} analysis={analysis.data} />}
          <Card>
            <CardHeader title="Details" />
            <div className="p-5">
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
          <div className="p-5">{document.progress && <StageList progress={document.progress} />}</div>
        </Card>
      </div>
    </>
  );
}
