import { useQuery } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import type { ReactNode } from "react";
import { Link, useParams, useSearchParams } from "react-router";
import { api } from "../api/client";
import type { Comparison, DiffOp, PolicyDetail, SectionRef } from "../api/types";
import { Badge, Card, CardHeader, EmptyState, ErrorState, PageHeader, Select, SkeletonRows } from "../components/ui";
import { formatDate } from "../lib/format";
import { hasRealEffectiveDate } from "../components/domain";

function DiffText({ ops, side }: { ops: DiffOp[]; side: "old" | "new" }) {
  return (
    <p className="whitespace-pre-wrap text-sm leading-relaxed">
      {ops.map((op, i) => {
        if (op.op === "equal") return <span key={i}>{side === "old" ? op.old : op.new}</span>;
        const text = side === "old" ? op.old : op.new;
        if (!text) return null;
        return <span key={i} className={side === "old" ? "diff-del" : "diff-ins"}>{text}</span>;
      })}
    </p>
  );
}

function SectionTitle({ section }: { section: SectionRef }) {
  return <p className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-muted">{section.label} · p.{section.page_start}</p>;
}

export function ComparePage() {
  const { id } = useParams();
  const [params, setParams] = useSearchParams();
  const policy = useQuery({ queryKey: ["policy", id], queryFn: () => api.get<PolicyDetail>(`/policies/${id}`) });
  const active = policy.data?.versions.filter((v) => v.status === "active") ?? [];
  const base = params.get("base") ?? active.at(-2)?.id;
  const target = params.get("target") ?? active.at(-1)?.id;
  const comparison = useQuery({
    queryKey: ["compare", id, base, target],
    queryFn: () => api.get<Comparison>(`/policies/${id}/compare?base=${base}&target=${target}`),
    enabled: Boolean(base && target && base !== target),
  });

  const choose = (key: "base" | "target", value: string) => {
    const next = new URLSearchParams(params);
    if (!next.get("base") && base) next.set("base", base);
    if (!next.get("target") && target) next.set("target", target);
    next.set(key, value);
    setParams(next, { replace: true });
  };
  const data = comparison.data;

  return (
    <>
      <PageHeader
        title={<span className="flex items-center gap-3"><Link to={`/policies/${id}`} className="text-muted hover:text-ink"><ArrowLeft className="size-5" /></Link>Compare versions</span>}
        subtitle={policy.data?.name}
        actions={<div className="flex w-full items-center gap-2 sm:w-auto">
          <Select className="min-w-0 flex-1 sm:w-48 sm:flex-none" value={base ?? ""} onChange={(e) => choose("base", e.target.value)} aria-label="Earlier version">
            {active.map((v) => <option key={v.id} value={v.id}>v{v.version_label}{hasRealEffectiveDate(v) ? ` (${formatDate(v.effective_from)})` : ""}</option>)}
          </Select>
          <span className="shrink-0 text-sm text-muted">vs</span>
          <Select className="min-w-0 flex-1 sm:w-48 sm:flex-none" value={target ?? ""} onChange={(e) => choose("target", e.target.value)} aria-label="Later version">
            {active.map((v) => <option key={v.id} value={v.id}>v{v.version_label}{hasRealEffectiveDate(v) ? ` (${formatDate(v.effective_from)})` : ""}</option>)}
          </Select>
        </div>}
      />
      {active.length < 2 && policy.data && <Card><EmptyState title="Only one version" description="Comparison needs at least two versions." /></Card>}
      {comparison.error && <ErrorState error={comparison.error} />}
      {comparison.isLoading && <Card><SkeletonRows rows={6} /></Card>}
      {data && (
        <div className="space-y-6">
          <div className="flex flex-wrap gap-2 text-sm">
            <Badge tone="warn">{data.stats.modified} modified</Badge>
            <Badge tone="ok">{data.stats.added} added</Badge>
            <Badge tone="bad">{data.stats.removed} removed</Badge>
            <Badge>{data.stats.unchanged} unchanged</Badge>
            <Badge tone="brand">{data.stats.numeric_changes} changed figures</Badge>
            <span className="text-xs text-muted">Deterministic comparison — original text of both versions is always shown.</span>
          </div>

          {data.modified.map((m) => (
            <Card key={`${m.old.label}-${m.new.label}`}>
              <CardHeader
                title={m.new.label}
                subtitle={m.title_changed ? `Renamed from “${m.old.label}”` : undefined}
                actions={<Badge tone="warn">Modified</Badge>}
              />
              {m.numeric_changes.changed.length > 0 && (
                <div className="flex flex-wrap gap-2 border-b border-line bg-warn-50/50 px-4 py-2.5 text-sm sm:px-5">
                  {m.numeric_changes.changed.map((c, i) => (
                    <span key={i} className="rounded bg-surface px-2 py-0.5 ring-1 ring-line"><span className="diff-del">{c.old}</span> → <span className="diff-ins">{c.new}</span></span>
                  ))}
                </div>
              )}
              <div className="grid divide-y divide-line md:grid-cols-2 md:divide-x md:divide-y-0">
                <div className="p-4 sm:p-5"><SideLabel>Version {data.from_version.label}</SideLabel><SectionTitle section={m.old} />{m.diff ? <DiffText ops={m.diff} side="old" /> : <p className="text-sm">{m.old.content}</p>}</div>
                <div className="p-4 sm:p-5"><SideLabel>Version {data.to_version.label}</SideLabel><SectionTitle section={m.new} />{m.diff ? <DiffText ops={m.diff} side="new" /> : <p className="text-sm">{m.new.content}</p>}</div>
              </div>
            </Card>
          ))}

          {data.added.map((s) => (
            <Card key={`added-${s.label}`}>
              <CardHeader title={s.label} actions={<Badge tone="ok">Added</Badge>} />
              <div className="grid divide-y divide-line md:grid-cols-2 md:divide-x md:divide-y-0">
                <div className="p-4 text-sm italic text-muted sm:p-5">Not present in version {data.from_version.label}</div>
                <div className="p-4 sm:p-5"><SectionTitle section={s} /><p className="diff-ins whitespace-pre-wrap text-sm">{s.content}</p></div>
              </div>
            </Card>
          ))}
          {data.removed.map((s) => (
            <Card key={`removed-${s.label}`}>
              <CardHeader title={s.label} actions={<Badge tone="bad">Removed</Badge>} />
              <div className="grid divide-y divide-line md:grid-cols-2 md:divide-x md:divide-y-0">
                <div className="p-4 sm:p-5"><SectionTitle section={s} /><p className="diff-del whitespace-pre-wrap text-sm">{s.content}</p></div>
                <div className="p-4 text-sm italic text-muted sm:p-5">Not present in version {data.to_version.label}</div>
              </div>
            </Card>
          ))}
          {!data.modified.length && !data.added.length && !data.removed.length && <Card><EmptyState title="No differences" /></Card>}
        </div>
      )}
    </>
  );
}

/** Which version a side is, when the two sides are stacked on a narrow screen. */
function SideLabel({ children }: { children: ReactNode }) {
  return <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted md:hidden">{children}</p>;
}
