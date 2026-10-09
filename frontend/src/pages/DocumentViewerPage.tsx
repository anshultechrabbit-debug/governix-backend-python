import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, ChevronLeft, ChevronRight, Info, ListTree, Quote, ZoomIn, ZoomOut } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router";
import { api } from "../api/client";
import type { PolicyDetail } from "../api/types";
import { useCategoryName } from "../components/domain";
import { Badge, Button, Card, ErrorState, KeyValue, Spinner, StatusBadge } from "../components/ui";
import { useDocument } from "../hooks";
import { cn, formatDate } from "../lib/format";

interface OutlineEntry { id: string; number: string | null; title: string; level: number; page_start: number }
interface ChunkRead { id: string; section_path: string; text: string; page_start: number; page_end: number }

function usePageImage(documentId: string, page: number, zoom: number, chunk: string | null, audit: boolean) {
  const [state, setState] = useState<{ url: string | null; highlights: number; error: unknown }>({ url: null, highlights: 0, error: null });
  useEffect(() => {
    let revoked: string | null = null;
    let cancelled = false;
    setState((s) => ({ ...s, url: null, error: null }));
    const params = new URLSearchParams({ zoom: String(zoom) });
    if (chunk) params.set("highlight_chunk", chunk);
    if (audit) params.set("audit", "true");
    api.blobUrl(`/documents/${documentId}/pages/${page}/image?${params}`)
      .then(({ url, headers }) => {
        revoked = url;
        if (!cancelled) setState({ url, highlights: Number(headers.get("X-Highlights") ?? 0), error: null });
      })
      .catch((error) => !cancelled && setState({ url: null, highlights: 0, error }));
    return () => {
      cancelled = true;
      if (revoked) URL.revokeObjectURL(revoked);
    };
  }, [documentId, page, zoom, chunk, audit]);
  return state;
}

export function DocumentViewerPage() {
  const { id = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const page = Math.max(1, Number(params.get("page") ?? 1));
  const chunkId = params.get("chunk");
  const [zoom, setZoom] = useState(1.4);
  // Below the widths where they fit beside the page, the outline and the details open over it.
  const [panel, setPanel] = useState<"outline" | "info" | null>(null);
  const audited = useRef(false);
  const categoryName = useCategoryName();

  const { data: document, error } = useDocument(id);
  const outline = useQuery({ queryKey: ["outline", id], queryFn: () => api.get<OutlineEntry[]>(`/documents/${id}/outline`), enabled: Boolean(id) });
  const chunk = useQuery({
    queryKey: ["chunk", id, chunkId],
    queryFn: () => api.get<ChunkRead>(`/documents/${id}/chunks/${chunkId}`),
    enabled: Boolean(chunkId),
  });
  const policy = useQuery({
    queryKey: ["policy", document?.policy_id],
    queryFn: () => api.get<PolicyDetail>(`/policies/${document!.policy_id}`),
    enabled: Boolean(document?.policy_id),
  });
  const shouldAudit = !audited.current;
  const image = usePageImage(id, page, zoom, chunkId, shouldAudit);
  useEffect(() => {
    if (image.url) audited.current = true;
  }, [image.url]);

  const pageCount = document?.page_count ?? 1;
  const goTo = (next: number) => {
    const bounded = Math.min(Math.max(1, next), pageCount);
    const nextParams = new URLSearchParams(params);
    nextParams.set("page", String(bounded));
    setParams(nextParams, { replace: true });
  };
  const version = policy.data?.versions.find((v) => v.id === document?.policy_version_id);

  if (error) return <ErrorState error={error} />;

  return (
    <div className="-mx-4 -my-5 flex h-[calc(100dvh-3.5rem)] flex-col sm:-mx-6 lg:-mx-8 lg:-my-7 lg:h-screen">
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1.5 border-b border-line bg-surface px-3 py-2 sm:px-5 sm:py-2.5">
        <div className="flex min-w-0 flex-1 items-center gap-2 sm:gap-3">
          <Link to={`/documents/${id}`} className="rounded p-1 text-muted hover:bg-subtle" aria-label="Back"><ArrowLeft className="size-4" /></Link>
          <p className="truncate text-sm font-semibold">{document?.title ?? document?.original_filename}</p>
          {version && <Badge tone="brand">v{version.version_label}</Badge>}
        </div>
        <div className="flex flex-wrap items-center gap-1">
          <Button variant="ghost" size="sm" className="px-2 md:hidden" onClick={() => setPanel("outline")} aria-label="Sections and pages"><ListTree className="size-4" /></Button>
          <Button variant={chunkId ? "secondary" : "ghost"} size="sm" className="px-2 xl:hidden" onClick={() => setPanel("info")}
            aria-label={chunkId ? "Cited evidence and details" : "Document details"}>
            {chunkId ? <Quote className="size-4" /> : <Info className="size-4" />}<span className="hidden sm:inline">{chunkId ? "Evidence" : "Details"}</span>
          </Button>
          <span className="mx-1 h-5 w-px bg-line xl:hidden" />
          <Button variant="ghost" size="sm" className="px-2" onClick={() => setZoom((z) => Math.max(0.6, +(z - 0.2).toFixed(1)))} aria-label="Zoom out"><ZoomOut className="size-4" /></Button>
          <span className="w-10 text-center text-xs tabular-nums text-muted sm:w-12">{Math.round(zoom * 100)}%</span>
          <Button variant="ghost" size="sm" className="px-2" onClick={() => setZoom((z) => Math.min(2.6, +(z + 0.2).toFixed(1)))} aria-label="Zoom in"><ZoomIn className="size-4" /></Button>
          <span className="mx-1 h-5 w-px bg-line sm:mx-2" />
          <Button variant="ghost" size="sm" className="px-2" disabled={page <= 1} onClick={() => goTo(page - 1)} aria-label="Previous page"><ChevronLeft className="size-4" /></Button>
          <span className="text-sm tabular-nums"><span className="hidden sm:inline">Page </span>{page}<span className="hidden sm:inline"> of</span><span className="sm:hidden">/</span>{" "}{pageCount.toLocaleString()}</span>
          <Button variant="ghost" size="sm" className="px-2" disabled={page >= pageCount} onClick={() => goTo(page + 1)} aria-label="Next page"><ChevronRight className="size-4" /></Button>
        </div>
      </div>

      <div className="flex min-h-0 flex-1">
        {panel && <div className={cn("fixed inset-0 z-40 bg-ink/40", panel === "outline" ? "md:hidden" : "xl:hidden")} onClick={() => setPanel(null)} />}
        {/* Left: pages and outline */}
        <aside className={cn(
          "w-60 shrink-0 overflow-y-auto border-r border-line bg-surface p-3",
          panel === "outline" ? "fixed inset-y-0 left-0 z-50 w-72 max-w-[85vw] shadow-xl md:static md:z-auto md:w-60 md:shadow-none" : "hidden md:block",
        )}>
          <p className="px-1 pb-2 text-xs font-medium uppercase tracking-wide text-muted">Go to page</p>
          <input
            type="number" min={1} max={pageCount} defaultValue={page} key={page}
            onKeyDown={(e) => { if (e.key === "Enter") { goTo(Number((e.target as HTMLInputElement).value)); setPanel(null); } }}
            className="mb-4 h-8 w-full rounded-md border border-line-strong px-2 text-sm"
          />
          <p className="px-1 pb-2 text-xs font-medium uppercase tracking-wide text-muted">Sections</p>
          {outline.data?.length ? (
            <ul className="space-y-0.5">
              {outline.data.map((s) => (
                <li key={s.id}>
                  <button
                    onClick={() => { goTo(s.page_start); setPanel(null); }}
                    className={cn("w-full truncate rounded px-2 py-1 text-left text-xs hover:bg-subtle", s.page_start === page && "bg-brand-50 text-brand-700")}
                    style={{ paddingLeft: `${0.5 + (s.level - 1) * 0.75}rem` }}
                    title={`${s.number ?? ""} ${s.title}`}
                  >
                    {s.number && <span className="mr-1 font-medium">{s.number}</span>}{s.title}
                  </button>
                </li>
              ))}
            </ul>
          ) : <p className="px-1 text-xs text-muted">No sections detected.</p>}
        </aside>

        {/* Center: rendered page */}
        <section className="min-w-0 flex-1 overflow-auto bg-subtle p-3 sm:p-6">
          {/* On a phone the page fits the screen width until zoomed in past the default. */}
          <div className={cn("mx-auto w-fit", zoom <= 1.4 && "max-sm:max-w-full")}>
            {image.error ? <ErrorState error={image.error} /> : image.url ? (
              <img src={image.url} alt={`Page ${page}`} className={cn("rounded bg-white shadow-md", zoom <= 1.4 && "max-sm:h-auto max-sm:max-w-full")} />
            ) : <div className="flex h-96 w-72 max-w-full items-center justify-center sm:w-96"><Spinner /></div>}
          </div>
        </section>

        {/* Right: document and evidence information */}
        <aside className={cn(
          "w-80 shrink-0 space-y-4 overflow-y-auto border-l border-line bg-surface p-4",
          panel === "info" ? "fixed inset-y-0 right-0 z-50 max-w-[90vw] shadow-xl xl:static xl:z-auto xl:shadow-none" : "hidden xl:block",
        )}>
          {chunkId && chunk.data && (
            <Card className="border-warn-600/30 bg-warn-50 p-3">
              <p className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-warn-600"><Quote className="size-3.5" />Cited evidence</p>
              <p className="mt-1 text-xs text-muted">{chunk.data.section_path} · page {chunk.data.page_start}{chunk.data.page_end !== chunk.data.page_start ? `–${chunk.data.page_end}` : ""}</p>
              <p className="mt-2 whitespace-pre-line text-sm leading-relaxed">{chunk.data.text}</p>
              {page >= chunk.data.page_start && page <= chunk.data.page_end && (
                <p className="mt-2 text-xs text-muted">
                  {image.highlights > 0 ? "Highlighted on the page." : "Exact position could not be highlighted on this page (e.g. scanned text)."}
                </p>
              )}
              {(page < chunk.data.page_start || page > chunk.data.page_end) && (
                <Button size="sm" variant="secondary" className="mt-2" onClick={() => { goTo(chunk.data!.page_start); setPanel(null); }}>Go to evidence</Button>
              )}
            </Card>
          )}
          {document && (
            <div className="space-y-4">
              <div className="flex items-center gap-2"><StatusBadge status={document.status} />{version?.timeline_state && <StatusBadge status={version.timeline_state} />}</div>
              <KeyValue items={[
                ["Policy", policy.data ? <Link className="text-brand-700 hover:underline" to={`/policies/${policy.data.id}`}>{policy.data.name}</Link> : "—"],
                ["Version", version ? `v${version.version_label}` : "—"],
                ["Effective", version ? `${formatDate(version.effective_from)} – ${version.effective_to ? formatDate(version.effective_to) : "present"}` : "—"],
                ["Category", categoryName(document.category_id)],
                ["Pages", document.page_count?.toLocaleString()],
              ]} />
            </div>
          )}
        </aside>
      </div>
    </div>
  );
}
