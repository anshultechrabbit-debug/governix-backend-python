import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, Cpu } from "lucide-react";
import { api } from "../api/client";
import { cn, formatDate, formatDateTime } from "../lib/format";
import { Badge, Card, CardHeader, ErrorState, SkeletonRows } from "./ui";

interface Totals { calls: number; input_tokens: number; output_tokens: number; cost_usd: number }

interface AIUsage {
  configured: boolean;
  status: "active" | "out_of_credit" | "not_configured";
  out_of_credit_at: string | null;
  models: { answers: string; embeddings: string };
  credit_usd: number | null;
  credit_since: string | null;
  spent_usd: number | null;
  spent_source: "openai" | "estimate" | null;
  remaining_usd: number | null;
  today: Totals;
  this_month: Totals;
  by_service: (Totals & { service: string })[];
  unpriced_models: string[];
}

const SERVICE_LABELS: Record<string, string> = { answers: "Answers & summaries", embeddings: "Search indexing" };

function usd(value: number | null | undefined) {
  if (value == null) return "—";
  if (value > 0 && value < 0.01) return "< $0.01";
  return `$${value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function tokens(value: number) {
  return Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(value);
}

/** OpenAI usage and the credit left (administrators only). */
export function AIUsageCard() {
  const { data, error, isLoading, refetch } = useQuery({
    queryKey: ["ai-usage"],
    queryFn: () => api.get<AIUsage>("/dashboard/ai-usage"),
    refetchInterval: 60_000,
  });

  const status = data?.status;
  const share = data?.credit_usd && data.remaining_usd != null ? data.remaining_usd / data.credit_usd : null;
  const low = share != null && share < 0.2;

  return (
    <Card className="overflow-hidden">
      <CardHeader
        title={<span className="flex items-center gap-2"><Cpu className="size-4 text-ai-600" />OpenAI usage</span>}
        subtitle={data ? `Answers: ${data.models.answers} · Indexing: ${data.models.embeddings}` : "AI provider credit and usage"}
        actions={status && (
          status === "active" ? <Badge tone="ok">Active</Badge>
            : status === "out_of_credit" ? <Badge tone="bad">Out of credit</Badge>
            : <Badge tone="neutral">Not configured</Badge>
        )}
      />
      {error ? <div className="p-4"><ErrorState error={error} onRetry={refetch} /></div>
        : isLoading || !data ? <SkeletonRows rows={3} />
        : (
          <div className="space-y-5 p-5">
            {status === "out_of_credit" && (
              <div className="flex gap-2 rounded-md border border-bad-600/20 bg-bad-50 p-3 text-sm text-bad-600">
                <AlertTriangle className="mt-0.5 size-4 shrink-0" />
                <p>
                  OpenAI refused a request for lack of credit{data.out_of_credit_at ? ` at ${formatDateTime(data.out_of_credit_at)}` : ""}.
                  Answers quote the documents directly and new uploads use basic search until credit is added.
                </p>
              </div>
            )}

            {data.credit_usd != null ? (
              <div>
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <p className="text-sm text-muted">Credit left</p>
                  <p className="text-xs text-muted">
                    {usd(data.spent_usd)} spent since {data.credit_since ? formatDate(data.credit_since) : "—"}
                    {data.spent_source === "estimate" ? " (estimated)" : " (from OpenAI)"}
                  </p>
                </div>
                <p className={cn("mt-1 text-3xl font-semibold tabular-nums tracking-tight", low ? "text-warn-600" : "text-ink")}>
                  {usd(data.remaining_usd)} <span className="text-base font-normal text-muted">of {usd(data.credit_usd)}</span>
                </p>
                <div className="mt-2 h-2 overflow-hidden rounded-full bg-subtle" role="progressbar" aria-label="Credit left"
                  aria-valuemin={0} aria-valuemax={100} aria-valuenow={share != null ? Math.round(share * 100) : undefined}>
                  <div className={cn("h-full rounded-full transition-[width]", low ? "bg-warn-600" : "bg-ok-600")}
                    style={{ width: `${Math.max(0, Math.min(100, (share ?? 0) * 100))}%` }} />
                </div>
                {low && <p className="mt-1.5 text-xs text-warn-600">Less than 20% left. Add credit in the OpenAI dashboard and update OPENAI_CREDIT_USD.</p>}
              </div>
            ) : (
              <div className="rounded-md bg-subtle px-3 py-2.5 text-xs text-ink-soft">
                OpenAI does not let an API key read its balance. To see the credit left, add to the backend <code>.env</code>:
                <code className="mt-1.5 block whitespace-pre rounded bg-surface px-2 py-1.5 text-[11px] text-ink">{"OPENAI_CREDIT_USD=10\nOPENAI_CREDIT_SINCE=2026-10-01"}</code>
                <span className="mt-1.5 block">the amount you added and the date you added it. For exact spend, also set <code>OPENAI_ADMIN_KEY</code> (an organization Admin key).</span>
              </div>
            )}

            <div className="grid grid-cols-2 gap-3">
              {([["Today", data.today], ["This month", data.this_month]] as const).map(([label, totals]) => (
                <div key={label} className="rounded-md border border-line px-3 py-2.5">
                  <p className="text-xs text-muted">{label}</p>
                  <p className="mt-0.5 text-lg font-semibold tabular-nums">{usd(totals.cost_usd)}</p>
                  <p className="text-xs text-muted">{tokens(totals.input_tokens + totals.output_tokens)} tokens · {totals.calls.toLocaleString()} calls</p>
                </div>
              ))}
            </div>

            {!!data.by_service.length && (
              <div>
                <p className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-muted">This month by use</p>
                <ul className="divide-y divide-line text-sm">
                  {data.by_service.map((row) => (
                    <li key={row.service} className="flex items-center justify-between gap-3 py-1.5">
                      <span className="text-ink-soft">{SERVICE_LABELS[row.service] ?? row.service}</span>
                      <span className="tabular-nums text-muted">{tokens(row.input_tokens + row.output_tokens)} tokens · <span className="text-ink">{usd(row.cost_usd)}</span></span>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            <p className="text-[11px] text-muted">
              {data.spent_source === "openai"
                ? "Spend is read from OpenAI and includes any use of the key outside Governix."
                : "Costs are estimated from the tokens Governix used, at OpenAI list prices; use of the key elsewhere is not included."}
              {!!data.unpriced_models.length && ` No price known for ${data.unpriced_models.join(", ")}; set OPENAI_PRICES to include them.`}
            </p>
          </div>
        )}
    </Card>
  );
}
