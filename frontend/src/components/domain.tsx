import { useQuery } from "@tanstack/react-query";
import { CheckCircle2, Circle, Clock, ExternalLink, Loader2, MinusCircle, XCircle } from "lucide-react";
import { Link } from "react-router";
import { api } from "../api/client";
import type { Branch, Category, EffectiveDateSource, Page, Progress, Source } from "../api/types";
import { cn, formatDate, formatDuration, humanize } from "../lib/format";
import { useAuth } from "../store/hooks";
import { Badge, ProgressBar } from "./ui";

export function useCategories() {
  return useQuery({ queryKey: ["categories"], queryFn: () => api.get<Category[]>("/categories"), staleTime: 300_000 });
}

export function useCategoryName() {
  const { data } = useCategories();
  return (id: string | null | undefined) => data?.find((c) => c.id === id)?.name ?? "—";
}

export function useBranches() {
  const { me } = useAuth();
  return useQuery({
    queryKey: ["branches"],
    queryFn: () => api.get<Page<Branch>>("/branches?limit=200"),
    enabled: Boolean(me?.organization_id),
    staleTime: 300_000,
  });
}

/** Every document states its scope explicitly (spec §52, Golden Rule 11). */
export function ScopeBadge({ branchId, branchName }: { branchId?: string | null; branchName?: string | null }) {
  return branchId
    ? <Badge tone="info">Branch{branchName ? ` · ${branchName}` : ""}</Badge>
    : <Badge tone="brand">Global</Badge>;
}

const STAGE_LABELS: Record<string, string> = {
  upload: "Uploading", validation: "Validation", extraction: "Text extraction", ocr: "OCR (scanned pages)",
  structure: "Structure analysis", analysis: "Policy matching", confirmation: "Confirmation",
  chunking: "Chunking", embedding: "Embedding", indexing: "Indexing", verification: "Validation",
};

function StageIcon({ status }: { status: string }) {
  if (status === "completed") return <CheckCircle2 className="size-4 text-ok-600" />;
  if (status === "running") return <Loader2 className="size-4 animate-spin text-brand-500" />;
  if (status === "failed") return <XCircle className="size-4 text-bad-600" />;
  if (status === "skipped") return <MinusCircle className="size-4 text-muted" />;
  if (status === "waiting") return <Clock className="size-4 text-warn-600" />;
  return <Circle className="size-4 text-line-strong" />;
}

/** Real progress from the backend: unit counts, never invented percentages or ETAs. */
export function StageList({ progress }: { progress: Progress }) {
  const extraction = progress.stages.find((s) => s.stage === "extraction");
  const embedding = progress.stages.find((s) => s.stage === "embedding");
  const chunking = progress.stages.find((s) => s.stage === "chunking");
  return (
    <div className="space-y-4">
      <div>
        <div className="mb-1.5 flex justify-between text-sm">
          <span className="font-medium">Overall</span>
          <span className="tabular-nums text-muted">{Math.round(progress.overall_percent)}%</span>
        </div>
        <ProgressBar value={progress.overall_percent} />
      </div>
      <ul className="space-y-2">
        {progress.stages.map((stage) => {
          const eta = formatDuration(stage.estimated_seconds_remaining);
          return (
            <li key={stage.stage} className="flex items-center gap-3 text-sm">
              <StageIcon status={stage.status} />
              <span className={cn("flex-1", stage.status === "pending" && "text-muted")}>{STAGE_LABELS[stage.stage] ?? humanize(stage.stage)}</span>
              <span className="text-xs tabular-nums text-muted">
                {stage.status === "running" && stage.total_units
                  ? `${stage.done_units.toLocaleString()} / ${stage.total_units.toLocaleString()}${eta ? ` · ~${eta} left (estimate)` : ""}`
                  : stage.status === "waiting" ? "Waiting for review"
                  : stage.status === "skipped" ? "Not needed"
                  : stage.status === "failed" ? "Failed"
                  : stage.status === "completed" ? "Done" : "Pending"}
              </span>
            </li>
          );
        })}
      </ul>
      {(extraction?.total_units ?? 0) > 200 && (
        <div className="grid grid-cols-3 gap-3 rounded-md bg-subtle p-3 text-xs">
          <div><p className="text-muted">Pages processed</p><p className="font-medium tabular-nums">{extraction!.done_units.toLocaleString()} / {extraction!.total_units!.toLocaleString()}</p></div>
          <div><p className="text-muted">Chunks</p><p className="font-medium tabular-nums">{(chunking?.detail?.chunks ?? chunking?.total_units ?? 0).toLocaleString()}</p></div>
          <div><p className="text-muted">Embeddings</p><p className="font-medium tabular-nums">{embedding?.done_units.toLocaleString()} / {(embedding?.total_units ?? 0).toLocaleString()}</p></div>
        </div>
      )}
    </div>
  );
}

/** The steps a person watches while a file is read, split and indexed (spec §8). */
const TRAIL_STAGES = ["extraction", "ocr", "structure", "analysis", "chunking", "embedding", "indexing"];

/** The part of a stage row the trail needs; a queue item carries just this much. */
export interface TrailStage {
  stage: string;
  status: string;
  done_units: number;
  total_units: number | null;
}

/**
 * The pipeline as it really runs, kept to one short line: a tick per step, then the
 * step being worked on with its real unit counts.
 */
export function StageTrail({ stages }: { stages: TrailStage[] }) {
  const steps = TRAIL_STAGES
    .map((name) => stages.find((s) => s.stage === name))
    .filter((s): s is TrailStage => Boolean(s) && s!.status !== "skipped");
  if (!steps.length) return <span className="text-xs text-muted">Reading the file…</span>;
  const current = steps.find((s) => s.status === "running") ?? steps.find((s) => s.status === "waiting") ?? steps[steps.length - 1];
  const done = steps.filter((s) => s.status === "completed").length;
  const counts = current.status === "running" && current.total_units
    ? `${current.done_units.toLocaleString()} / ${current.total_units.toLocaleString()}`
    : null;
  return (
    <div className="space-y-1">
      <div className="flex flex-wrap items-center gap-x-1.5 gap-y-1 text-xs">
        {steps.map((step) => (
          <StageIcon key={step.stage} status={step.status} />
        ))}
        <span className={cn("font-medium", current.status === "running" && "text-brand-700", current.status === "failed" && "text-bad-600")}>
          {STAGE_LABELS[current.stage] ?? humanize(current.stage)}
        </span>
        {counts && <span className="tabular-nums text-muted">{counts}</span>}
        <span className="tabular-nums text-muted">· {done}/{steps.length} done</span>
      </div>
      <ProgressBar value={(done / steps.length) * 100} tone={current.status === "failed" ? "bad" : "brand"} />
    </div>
  );
}

export function viewerLink(source: { document_id: string | null; page_start: number | null; chunk_id?: string | null }) {
  const params = new URLSearchParams({ page: String(source.page_start ?? 1) });
  if (source.chunk_id) params.set("chunk", source.chunk_id);
  return `/documents/${source.document_id}/view?${params}`;
}

export interface SourceGroup {
  key: string;
  title: string;
  sources: Source[];
}

/** Sources grouped by the document version they come from, in citation order. */
export function groupSources(sources: Source[]): SourceGroup[] {
  const groups = new Map<string, SourceGroup>();
  for (const source of sources) {
    const key = source.kind === "comparison" ? `comparison-${source.number}` : `${source.document_id ?? source.policy_name}-${source.version_id ?? ""}`;
    const title = source.kind === "comparison" ? "Version comparison" : source.policy_name ?? source.document_title ?? "Document";
    if (!groups.has(key)) groups.set(key, { key, title, sources: [] });
    groups.get(key)!.sources.push(source);
  }
  return [...groups.values()];
}

function pages(source: Source) {
  if (!source.page_start) return null;
  return source.page_end && source.page_end !== source.page_start ? `pp. ${source.page_start}–${source.page_end}` : `p. ${source.page_start}`;
}

/** One card per document, its cited passages listed under it. `anchor` gives each passage a scroll target id. */
export function SourceGroupCard({ group, anchor }: { group: SourceGroup; anchor: (number: number) => string }) {
  const first = group.sources[0];
  return (
    <div className="rounded-md border border-line bg-surface text-sm">
      <div className="flex items-start justify-between gap-2 border-b border-line px-3 py-2.5">
        <div className="min-w-0">
          <p className="truncate font-medium text-ink" title={group.title}>{group.title}</p>
          <p className="mt-0.5 text-xs text-muted">
            {[
              first.version_label && `Version ${first.version_label}`,
              first.effective_from && `Effective ${formatDate(first.effective_from)}${first.effective_to ? ` – ${formatDate(first.effective_to)}` : ""}`,
              `${group.sources.length} passage${group.sources.length === 1 ? "" : "s"}`,
            ].filter(Boolean).join(" • ")}
          </p>
        </div>
        {first.previous_version ? <Badge tone="warn">Previous version</Badge> : first.category ? <Badge>{first.category}</Badge> : null}
      </div>
      {first.previous_version && (
        <p className="mx-3 mt-2 rounded bg-warn-50 px-2 py-1 text-[11px] text-warn-600">
          The version in force did not cover this, so it is answered from this earlier version.
        </p>
      )}
      <ul className="divide-y divide-line">
        {group.sources.map((source) => (
          <li key={source.evidence_id} id={anchor(source.number)} className="flex scroll-mt-4 gap-2.5 px-3 py-2.5">
            <span className="mt-0.5 inline-flex size-5 shrink-0 items-center justify-center rounded bg-brand-50 text-xs font-medium text-brand-700">{source.number}</span>
            <div className="min-w-0 flex-1">
              <p className="text-xs font-medium text-ink-soft">
                {[source.section_number && `Section ${source.section_number}`, pages(source)].filter(Boolean).join(" · ") || "Passage"}
              </p>
              <p className="mt-0.5 line-clamp-2 whitespace-pre-line text-xs leading-relaxed text-muted">{source.excerpt}</p>
            </div>
            {source.kind === "passage" && source.document_id && (
              <Link to={viewerLink(source)} className="shrink-0 self-start rounded p-1 text-brand-700 hover:bg-brand-50" title="Open evidence" aria-label={`Open evidence for source ${source.number}`}>
                <ExternalLink className="size-3.5" />
              </Link>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

const REAL_DATES: EffectiveDateSource[] = ["entered", "detected"];

/** True when the effective date is a business date, not a placeholder that only orders versions. */
export function hasRealEffectiveDate(version: { effective_date_source?: EffectiveDateSource } | null | undefined) {
  return !version?.effective_date_source || REAL_DATES.includes(version.effective_date_source);
}

/** "Effective 1 Jan 2025", or a clear note when no effective date was stated. */
export function EffectiveDate({ version, prefix = "Effective " }: {
  version: { effective_from: string; effective_date_source?: EffectiveDateSource };
  prefix?: string;
}) {
  if (hasRealEffectiveDate(version)) return <>{prefix}{formatDate(version.effective_from)}</>;
  return (
    <span className="text-muted" title="No effective date was stated or entered. The version order you arranged is kept.">
      No effective date stated
    </span>
  );
}
