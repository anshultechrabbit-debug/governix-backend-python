import { Bot, ChevronDown, FileWarning, History, RotateCcw, Send, Sparkles } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { useCategories } from "../components/domain";
import { Badge, Button, Card, EmptyState, ErrorState, Field, Select, Tabs, Textarea } from "../components/ui";
import { groupSources, SourceGroupCard } from "../components/domain";
import { PassageSearch } from "../components/PassageSearch";
import { cn, formatDate } from "../lib/format";
import { newId } from "../lib/id";
import { ask, clearConversation, setOptions, type AnswerMode, type AnswerStage, type Turn } from "../store/assistantSlice";
import type { Answer } from "../api/types";
import { useAppDispatch, useAppSelector, useAuth } from "../store/hooks";

type View = "ask" | "search";

const MODE_LABELS: Record<AnswerMode, string> = {
  auto: "Automatically choose",
  current: "Current policies",
  historical: "As of a date",
  version: "Specific version",
  compare: "Compare versions",
};

export function AssistantPage() {
  const dispatch = useAppDispatch();
  const { me } = useAuth();
  const { turns, options } = useAppSelector((state) => state.assistant);
  const categories = useCategories();
  const [view, setView] = useState<View>("ask");
  const [question, setQuestion] = useState("");
  const [wordless, setWordless] = useState(false);
  const [showOptions, setShowOptions] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);
  const pending = turns.some((turn) => turn.status === "pending");

  const streamed = turns.reduce((total, turn) => total + (turn.streamedClaims?.length ?? 0), 0);
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns.length, pending, streamed]);

  function submit(event: FormEvent) {
    event.preventDefault();
    const value = question.trim();
    if (!value || pending) return;
    // Emoji or punctuation alone has nothing to search for.
    if (!/[\p{L}\p{N}]/u.test(value)) return setWordless(true);
    dispatch(ask({ id: newId(), question: value, options }));
    setQuestion("");
  }

  function updateOptions(next: Partial<typeof options>) {
    dispatch(setOptions({ ...options, ...next }));
  }

  const scope = me?.role === "department_user"
    ? "Governix answers only from the policies assigned to you, latest version first."
    : me?.role === "branch_manager"
      ? "Governix answers from global policies and your own branch's policies, latest version first."
      : "Governix answers from your organization's policies, latest version first.";

  return (
    <div className="mx-auto flex max-w-5xl flex-col">
      <div className="mb-5">
        <h1 className="flex items-center gap-2 text-xl font-semibold tracking-tight"><Bot className="size-5 text-ai-600" />AI Assistant</h1>
        <p className="mt-1 text-sm text-muted">{scope} Every supported answer is cited.</p>
      </div>

      <Tabs<View> value={view} onChange={setView} tabs={[
        { id: "ask", label: "Ask Governix" },
        { id: "search", label: "Search passages" },
      ]} />

      {view === "search" ? (
        <div className="mt-5">
          <PassageSearch initialQuery={question.trim()} />
        </div>
      ) : (
        <>
          <section className="min-h-[22rem] space-y-5 pt-5">
            {!turns.length && (
              <EmptyState
                icon={<Sparkles className="size-6 text-ai-600" />}
                title="Ask about your policies and documents"
                description="Governix searches only the information you are allowed to access, validates the evidence, then cites every supported answer. If nothing supports an answer, it says so instead of guessing."
              />
            )}
            {turns.map((turn) => <Turn key={turn.id} turn={turn} />)}
            <div ref={endRef} />
          </section>

          <Card className="sticky bottom-4 mt-5 overflow-hidden shadow-lg">
            <form onSubmit={submit} className="p-3">
              <Textarea
                rows={3}
                value={question}
                maxLength={2000}
                placeholder="Ask a question about an approved policy or procedure…"
                onChange={(event) => { setQuestion(event.target.value); setWordless(false); }}
                onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) submit(event); }}
                aria-label="Question for AI Assistant"
              />
              <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
                <div className="relative">
                  <Button type="button" variant="ghost" size="sm" onClick={() => setShowOptions((shown) => !shown)} aria-expanded={showOptions}>
                    {MODE_LABELS[options.mode]} <ChevronDown className="size-3.5" />
                  </Button>
                  {showOptions && (
                    <div className="absolute bottom-10 left-0 z-10 w-80 rounded-lg border border-line bg-surface p-3 shadow-lg">
                      <div className="space-y-3">
                        <Field label="Answer using"><Select value={options.mode} onChange={(event) => updateOptions({ mode: event.target.value as AnswerMode })}>
                          {Object.entries(MODE_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                        </Select></Field>
                        {options.mode === "historical" && <Field label="Effective as of"><input type="date" className="h-9 w-full rounded-md border border-line-strong px-3 text-sm" value={options.as_of ?? ""} onChange={(event) => updateOptions({ as_of: event.target.value || undefined })} /></Field>}
                        <Field label="Limit to category (optional)"><Select value={(options as typeof options & { category_ids?: string[] }).category_ids?.[0] ?? ""} onChange={(event) => updateOptions({ category_ids: event.target.value ? [event.target.value] : [] } as Partial<typeof options>)}>
                          <option value="">All accessible categories</option>
                          {categories.data?.map((category) => <option key={category.id} value={category.id}>{category.name}</option>)}
                        </Select></Field>
                      </div>
                    </div>
                  )}
                </div>
                {wordless
                  ? <span className="text-xs text-warn-600" role="alert">Type your question in words, e.g. the policy or topic and what you want to know.</span>
                  : <span className="text-xs text-muted">{question.length}/2000 · Enter to ask, Shift + Enter for a new line</span>}
                <div className="flex gap-2">
                  {turns.length > 0 && <Button type="button" variant="secondary" size="sm" onClick={() => dispatch(clearConversation())}><RotateCcw className="size-3.5" />Clear</Button>}
                  <Button type="submit" loading={pending} disabled={!question.trim()}><Send className="size-4" />Ask</Button>
                </div>
              </div>
            </form>
          </Card>
        </>
      )}
    </div>
  );
}

function Turn({ turn }: { turn: Turn }) {
  const answer = turn.answer;
  return (
    <div className="space-y-3">
      <div className="ml-auto w-fit max-w-[80%] whitespace-pre-wrap break-words rounded-lg bg-brand-600 px-4 py-3 text-sm text-white">{turn.question}</div>
      {turn.status === "pending" && <Streaming turn={turn} />}
      {turn.status === "error" && <ErrorState error={new Error(turn.error)} />}
      {turn.status === "done" && answer && (
        <Card className="overflow-hidden">
          {answer.status === "no_answer" ? <NoAnswer reason={answer.no_answer} /> : <Answered answer={answer} />}
        </Card>
      )}
    </div>
  );
}

const STAGE_LABELS: Record<AnswerStage, string> = {
  searching: "Searching the documents you can access…",
  reading: "Reading the most relevant passages…",
  writing: "Writing a grounded answer…",
};

/** The answer while it is being written: only claims that already passed validation are shown. */
function Streaming({ turn }: { turn: Turn }) {
  const claims = turn.streamedClaims ?? [];
  const sources = turn.streamedSources ?? [];
  const label = turn.stage === "reading" && turn.passages
    ? `Reading ${turn.passages} relevant passages…`
    : STAGE_LABELS[turn.stage ?? "searching"];
  return (
    <Card className="overflow-hidden">
      <div className="p-5">
        <p className="inline-flex items-center gap-2 text-sm text-muted" aria-live="polite">
          <Sparkles className="size-4 animate-pulse text-ai-600" />{claims.length ? "Writing a grounded answer…" : label}
        </p>
        {!!claims.length && (
          <div className="mt-3 space-y-3 text-sm leading-6 text-ink">
            {claims.map((claim, index) => <p key={`${claim.text}-${index}`}>{claim.text} {claim.citations.map((citation) => <span key={citation} className="ml-0.5 text-xs font-semibold text-brand-700">[{citation}]</span>)}</p>)}
          </div>
        )}
        {!!sources.length && <p className="mt-3 text-xs text-muted">{sources.length} verified source{sources.length === 1 ? "" : "s"} so far</p>}
      </div>
    </Card>
  );
}

function Answered({ answer }: { answer: Answer }) {
  const [open, setOpen] = useState(false);
  // Spec §31/§55: when the version in force did not answer it, the older version
  // used must be named explicitly rather than blended into the answer.
  const older = [...new Set(answer.sources.filter((source) => source.previous_version)
    .map((source) => `${source.policy_name ?? source.document_title} v${source.version_label}`))];
  // The banner already says this; the matching backend warning would repeat it.
  const warnings = older.length
    ? answer.warnings.filter((warning) => !warning.startsWith("The version currently in force does not cover this"))
    : answer.warnings;
  const disagree = answer.conflicts.some((conflict) => conflict.citations?.length);
  // The short answer: the verified plain-words summary, else the first points as written.
  const lead = answer.summary ? null : answer.claims.slice(0, SHORT_CLAIMS);
  const more = answer.summary ? answer.claims.length : answer.claims.length - SHORT_CLAIMS;
  const notes = warnings.length + answer.conflicts.length;
  const groups = groupSources(answer.sources);
  const anchor = (number: number) => `source-${answer.query_id}-${number}`;

  function cite(number: number) {
    setOpen(true);
    requestAnimationFrame(() => document.getElementById(anchor(number))?.scrollIntoView({ behavior: "smooth", block: "center" }));
  }
  const citations = (numbers: number[]) => numbers.map((n) => (
    <button key={n} type="button" onClick={() => cite(n)} className="ml-0.5 align-baseline text-xs font-semibold text-brand-700 hover:underline">[{n}]</button>
  ));

  return (
    <div className="p-5">
      <p className="text-sm font-medium text-ai-600">Grounded answer</p>
      {!!older.length && (
        <p className="mt-2 flex items-start gap-1.5 text-xs text-warn-600">
          <History className="mt-px size-3.5 shrink-0" />
          <span>From an earlier version, because the version in force does not cover this: <span className="font-medium">{older.join(", ")}</span></span>
        </p>
      )}

      <div className="mt-2 space-y-2 text-[15px] leading-7 text-ink">
        {answer.summary
          ? <p>{answer.summary}</p>
          : lead?.map((claim, index) => <p key={`${claim.text}-${index}`}>{claim.text} {citations(claim.citations)}</p>)}
      </div>
      {disagree && (
        <p className="mt-2 flex items-center gap-1.5 text-xs font-medium text-warn-600"><FileWarning className="size-3.5" />Sources disagree on part of this. See details.</p>
      )}

      <div className="mt-3 flex flex-wrap items-center gap-1.5">
        {groups.map((group) => (
          <button
            key={group.key}
            type="button"
            onClick={() => cite(group.sources[0].number)}
            className="inline-flex max-w-full items-center gap-1.5 rounded-full border border-line bg-subtle/60 px-2.5 py-0.5 text-xs text-ink-soft hover:border-brand-500 hover:text-brand-700"
            title={group.title}
          >
            <span className="truncate">{group.title}</span>
            <span className="shrink-0 text-muted">
              {group.sources.length > 1 ? `${group.sources.length} passages` : group.sources[0].page_start ? `p. ${group.sources[0].page_start}` : ""}
            </span>
          </button>
        ))}
      </div>

      <button
        type="button"
        onClick={() => setOpen((shown) => !shown)}
        aria-expanded={open}
        className="mt-4 inline-flex items-center gap-1 text-sm font-medium text-brand-700 hover:underline"
      >
        {open ? "Hide details" : "Show details"}
        <ChevronDown className={cn("size-4 transition-transform", open && "rotate-180")} />
        {!open && (more > 0 || notes > 0) && (
          <span className="ml-1 font-normal text-muted">
            ({[more > 0 && `${more} cited point${more === 1 ? "" : "s"}`, notes > 0 && `${notes} note${notes === 1 ? "" : "s"}`].filter(Boolean).join(", ")})
          </span>
        )}
      </button>

      {open && (
        <div className="mt-4 space-y-4 border-t border-line pt-4">
          {!!answer.claims.length && (
            <div>
              <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">What the sources say</p>
              <div className="space-y-2 text-sm leading-6 text-ink-soft">
                {answer.claims.map((claim, index) => <p key={`${claim.text}-${index}`}>{claim.text} {citations(claim.citations)}</p>)}
              </div>
            </div>
          )}
          {!!warnings.length && <div className="rounded-md border border-warn-600/20 bg-warn-50 p-3 text-xs text-warn-600">{warnings.map((warning) => <p key={warning}>{warning}</p>)}</div>}
          {!!answer.conflicts.length && <div className="rounded-md border border-warn-600/30 bg-warn-50 p-3"><p className="flex items-center gap-1.5 text-sm font-medium text-warn-600"><FileWarning className="size-4" />Potential source conflict</p>{answer.conflicts.map((conflict, index) => <p key={index} className="mt-1 text-xs text-ink-soft">{conflict.description} {conflict.citations && citations(conflict.citations)}</p>)}</div>}
          <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted"><span>{answer.plan.explanation}</span><span>Evidence score: {Math.round(answer.evidence_score * 100)}%</span>{answer.plan.as_of && <span>As of {formatDate(answer.plan.as_of)}</span>}{answer.cache_hit && <span>Cached result</span>}</div>
          <div>
            <p className="mb-3 text-xs font-semibold uppercase tracking-wide text-muted">Verified sources</p>
            <div className="space-y-3">{groups.map((group) => <SourceGroupCard key={group.key} group={group} anchor={anchor} />)}</div>
          </div>
        </div>
      )}
    </div>
  );
}

/** Points shown in the short answer when there is no plain-words summary. */
const SHORT_CLAIMS = 2;

function NoAnswer({ reason }: { reason: Answer["no_answer"] }) {
  if (!reason) return null;
  return <div className="p-5"><p className="font-medium">No verified answer available</p><p className="mt-1 text-sm text-muted">{reason.message}</p>{reason.missing_terms.length > 0 && <p className="mt-2 text-xs text-muted">Terms not found: {reason.missing_terms.join(", ")}</p>}<div className="mt-4 flex flex-wrap gap-2">{reason.suggestions.map((suggestion) => <Badge key={suggestion} tone="neutral">{suggestion}</Badge>)}</div></div>;
}
