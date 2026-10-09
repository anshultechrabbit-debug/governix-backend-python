import { useMutation } from "@tanstack/react-query";
import { FileSearch, Search } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link } from "react-router";
import { api } from "../api/client";
import type { SearchResponse } from "../api/types";
import { useCategories } from "./domain";
import { Badge, Button, Card, EmptyState, ErrorState, Field, Select } from "./ui";
import { formatDate } from "../lib/format";

type SearchMode = "current" | "as_of" | "versions" | "all";

const MODE_OPTIONS: { value: SearchMode; label: string }[] = [
  { value: "current", label: "Latest version of each policy" },
  { value: "as_of", label: "The version in force on a date" },
  { value: "all", label: "All versions, including archived" },
];

/**
 * Raw passage retrieval (spec §25 "search available policy information").
 * Lives inside the assistant rather than as its own screen: it is the same
 * access-filtered index the answers are built from, without the LLM step.
 */
export function PassageSearch({ initialQuery = "" }: { initialQuery?: string }) {
  const [query, setQuery] = useState(initialQuery);
  const [mode, setMode] = useState<SearchMode>("current");
  const [asOf, setAsOf] = useState("");
  const [categoryId, setCategoryId] = useState("");
  const categories = useCategories();
  const search = useMutation({
    mutationFn: () => api.post<SearchResponse>("/search", {
      query: query.trim(),
      mode,
      as_of: mode === "as_of" ? asOf || undefined : undefined,
      category_ids: categoryId ? [categoryId] : [],
      limit: 30,
    }),
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    if (query.trim()) search.mutate();
  }

  const result = search.data;
  return (
    <div className="space-y-5">
      <Card className="p-4">
        <form onSubmit={submit} className="space-y-3">
          <div className="flex flex-wrap gap-3">
            <input
              className="h-10 min-w-0 flex-1 basis-full rounded-md sm:min-w-[16rem] sm:basis-auto border border-line-strong px-3 text-sm placeholder:text-muted focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-100"
              placeholder="Search policy text, clauses or procedures…"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              aria-label="Search the policies you can access"
            />
            <Select className="min-w-0 flex-1 sm:w-56 sm:flex-none" value={mode} onChange={(event) => setMode(event.target.value as SearchMode)} aria-label="Version scope">
              {MODE_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
            </Select>
            <Button type="submit" loading={search.isPending} disabled={!query.trim()}><Search className="size-4" />Search</Button>
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            {mode === "as_of" && (
              <Field label="In force as of">
                <input type="date" className="h-9 w-full rounded-md border border-line-strong px-3 text-sm"
                  value={asOf} onChange={(event) => setAsOf(event.target.value)} />
              </Field>
            )}
            <Field label="Category">
              <Select value={categoryId} onChange={(event) => setCategoryId(event.target.value)}>
                <option value="">All accessible categories</option>
                {categories.data?.map((category) => <option key={category.id} value={category.id}>{category.name}</option>)}
              </Select>
            </Field>
          </div>
        </form>
      </Card>

      {search.error && <ErrorState error={search.error} onRetry={() => search.mutate()} />}

      {result && (
        <section className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-sm text-muted">
              {result.passages.length} matching passage{result.passages.length === 1 ? "" : "s"} · {result.cache_hit ? "cached" : "live"} search
            </p>
            <div className="flex flex-wrap gap-1">{result.terms.map((term) => <Badge key={term}>{term}</Badge>)}</div>
          </div>
          {!result.passages.length ? (
            <Card>
              <EmptyState icon={<FileSearch className="size-6" />} title="No matching passages"
                description="Try a policy number, a more specific term, or broaden the version scope." />
            </Card>
          ) : result.passages.map((passage) => <PassageCard key={passage.chunk_id} passage={passage} />)}
          {!!result.policies.length && (
            <Card className="p-4">
              <p className="text-sm font-semibold">Matching policies</p>
              <div className="mt-3 flex flex-wrap gap-2">
                {result.policies.map((policy) => (
                  <Link key={policy.id} to={`/policies/${policy.id}`}>
                    <Badge tone="brand">{policy.name}{policy.policy_number ? ` · ${policy.policy_number}` : ""}</Badge>
                  </Link>
                ))}
              </div>
            </Card>
          )}
        </section>
      )}
    </div>
  );
}

function PassageCard({ passage }: { passage: SearchResponse["passages"][number] }) {
  const { source } = passage;
  const viewer = `/documents/${source.document_id}/view?${new URLSearchParams({ page: String(source.page_start), chunk: passage.chunk_id })}`;
  return (
    <Card className="p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="font-medium">{source.policy_name ?? source.document_title ?? "Untitled document"}</p>
          <p className="mt-0.5 text-xs text-muted">
            {[
              source.version_label && `Version ${source.version_label}`,
              source.section_number && `Section ${source.section_number}`,
              `Page ${source.page_start}${source.page_end !== source.page_start ? `–${source.page_end}` : ""}`,
              source.effective_from && `Effective ${formatDate(source.effective_from)}`,
            ].filter(Boolean).join(" • ")}
          </p>
        </div>
        <Badge tone="brand">Relevance {Math.round(passage.score * 100)}%</Badge>
      </div>
      <p className="mt-3 whitespace-pre-line text-sm leading-6 text-ink-soft">{passage.text}</p>
      <Link className="mt-3 inline-flex text-xs font-medium text-brand-700 hover:underline" to={viewer}>
        Open in document viewer
      </Link>
    </Card>
  );
}
