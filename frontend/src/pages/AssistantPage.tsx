import { useQueryClient } from "@tanstack/react-query";
import { ArrowDown, Bot, ChevronDown, FileWarning, History, MessagesSquare, Plus, Send, Sparkles, X } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { ChatHistory } from "../components/ChatHistory";
import { useCategories } from "../components/domain";
import { Button, Card, EmptyState, ErrorState, Field, Select, SkeletonRows, Tabs, Textarea } from "../components/ui";
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
  const { turns, options, loading } = useAppSelector((state) => state.assistant);
  const queryClient = useQueryClient();
  const [historyOpen, setHistoryOpen] = useState(false);
  // A finished answer is saved to the chat history: show it there.
  const answered = turns.filter((turn) => turn.status === "done").length;
  useEffect(() => {
    if (answered) void queryClient.invalidateQueries({ queryKey: ["conversations"] });
  }, [answered, queryClient]);
  const categories = useCategories();
  const [view, setView] = useState<View>("ask");
  const [question, setQuestion] = useState("");
  const [wordless, setWordless] = useState(false);
  const [showOptions, setShowOptions] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);
  const pending = turns.some((turn) => turn.status === "pending");

  // Scroll once, when a question is asked or a chat is opened: the newest question at the
  // top, its answer appearing below it. The page then stays where the reader puts it.
  const lastId = turns[turns.length - 1]?.id;
  useEffect(() => {
    if (lastId) document.getElementById(`turn-${lastId}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [lastId]);

  // Offer a way down when newer content is out of view, instead of pulling the reader there.
  const [atEnd, setAtEnd] = useState(true);
  useEffect(() => {
    const end = endRef.current;
    if (!end) return;
    const observer = new IntersectionObserver(([entry]) => setAtEnd(entry.isIntersecting), { rootMargin: "0px 0px -160px 0px" });
    observer.observe(end);
    return () => observer.disconnect();
  }, [view, loading]);

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
    <div className="mx-auto grid max-w-7xl gap-6 lg:grid-cols-[16rem_minmax(0,1fr)]">
      <aside className="sticky top-4 hidden h-[calc(100vh-2rem)] overflow-hidden rounded-lg border border-line bg-surface lg:block">
        <ChatHistory />
      </aside>
      {historyOpen && (
        <div className="fixed inset-0 z-40 lg:hidden" role="dialog" aria-modal="true" aria-label="Chat history">
          <div className="absolute inset-0 bg-ink/40" onClick={() => setHistoryOpen(false)} />
          <div className="absolute inset-y-0 left-0 flex w-72 max-w-[85vw] flex-col bg-surface shadow-xl">
            <div className="flex items-center justify-between border-b border-line px-3 py-2">
              <p className="text-sm font-semibold">Chat history</p>
              <button onClick={() => setHistoryOpen(false)} className="rounded p-1 text-muted hover:bg-subtle" aria-label="Close"><X className="size-4" /></button>
            </div>
            <div className="min-h-0 flex-1"><ChatHistory onPicked={() => setHistoryOpen(false)} /></div>
          </div>
        </div>
      )}

    <div className="flex min-w-0 flex-col">
      <div className="mb-5 flex items-start justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-xl font-semibold tracking-tight"><Bot className="size-5 text-ai-600" />AI Assistant</h1>
          <p className="mt-1 text-sm text-muted">{scope} Every supported answer is cited.</p>
        </div>
        <Button variant="secondary" size="sm" className="lg:hidden" onClick={() => setHistoryOpen(true)}><MessagesSquare className="size-4" />History</Button>
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
            {loading && <Card><SkeletonRows rows={4} /></Card>}
            {!loading && !turns.length && (
              <EmptyState
                icon={<Sparkles className="size-6 text-ai-600" />}
                title="Ask about your policies and documents"
                description="Governix searches only the information you are allowed to access, validates the evidence, then cites every supported answer. If nothing supports an answer, it says so instead of guessing."
              />
            )}
            {!loading && turns.map((turn) => <Turn key={turn.id} turn={turn} />)}
            <div ref={endRef} />
          </section>

          {!atEnd && turns.length > 0 && (
            <div className="pointer-events-none sticky bottom-44 z-10 flex justify-center">
              <button
                type="button"
                onClick={() => endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" })}
                className="pointer-events-auto inline-flex items-center gap-1.5 rounded-full border border-line bg-surface px-3 py-1.5 text-xs font-medium text-ink-soft shadow-md hover:text-brand-700"
              >
                <ArrowDown className="size-3.5" />{pending ? "Answer in progress" : "Jump to latest"}
              </button>
            </div>
          )}

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
                  {turns.length > 0 && <Button type="button" variant="secondary" size="sm" disabled={pending} onClick={() => dispatch(clearConversation())}><Plus className="size-3.5" />New chat</Button>}
                  <Button type="submit" loading={pending} disabled={!question.trim()}><Send className="size-4" />Ask</Button>
                </div>
              </div>
            </form>
          </Card>
        </>
      )}
    </div>
    </div>
  );
}

function Turn({ turn }: { turn: Turn }) {
  const answer = turn.answer;
  const dispatch = useAppDispatch();
  const askAgain = () => dispatch(ask({ id: newId(), question: turn.question, options: turn.options }));
  return (
    <div id={`turn-${turn.id}`} className="scroll-mt-4 space-y-3">
      <div className="ml-auto w-fit max-w-[80%] whitespace-pre-wrap break-words rounded-lg bg-brand-600 px-4 py-3 text-sm text-white">{turn.question}</div>
      {turn.status === "pending" && <Streaming turn={turn} />}
      {turn.status === "error" && <ErrorState error={new Error(turn.error)} />}
      {turn.status === "done" && answer && (
        <Card className="overflow-hidden">
          {answer.status === "no_answer" ? <NoAnswer reason={answer.no_answer} onRetry={askAgain} /> : <Answered answer={answer} />}
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
    ? answer.warnings.filter((warning) => !warning.startsWith("The version in force today does not cover this"))
    : answer.warnings;
  const understood = answer.plan.rewritten_question && answer.plan.rewritten_question !== answer.question
    ? answer.plan.rewritten_question : null;
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
      {answer.plan.rewrite_reason === "split" && answer.plan.parts ? (
        <div className="mt-2 text-xs text-muted">
          <p>Answered as {answer.plan.parts.length} questions:</p>
          <ol className="mt-0.5 list-decimal pl-5 italic">{answer.plan.parts.map((part) => <li key={part}>{part}</li>)}</ol>
        </div>
      ) : understood && (
        <p className="mt-2 text-xs text-muted">
          {answer.plan.rewrite_reason === "translation" ? "Translated and answered as" : "Understood as"}: <span className="italic">“{understood}”</span>
        </p>
      )}
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
          {!!warnings.length && (
            <div className="rounded-md border border-warn-600/20 bg-warn-50 p-3 text-xs text-warn-600">
              <p className="mb-1 font-semibold">Please note</p>
              <ul className="list-disc space-y-0.5 pl-4">{warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>
            </div>
          )}
          {!!answer.conflicts.length && (
            <div className="rounded-md border border-warn-600/30 bg-warn-50 p-3">
              <p className="flex items-center gap-1.5 text-sm font-medium text-warn-600"><FileWarning className="size-4" />Sources disagree</p>
              <ul className="mt-1 space-y-1 text-xs text-ink-soft">
                {answer.conflicts.map((conflict, index) => (
                  <li key={index}>
                    {conflict.description} {conflict.citations && citations(conflict.citations)}
                    {conflict.resolution_hint && conflict.type === "AMENDED" && <span className="block text-muted">{conflict.resolution_hint}</span>}
                  </li>
                ))}
              </ul>
            </div>
          )}
          <p className="text-xs text-muted">{answerBasis(answer)}</p>
          <div>
            <p className="mb-3 text-xs font-semibold uppercase tracking-wide text-muted">Verified sources</p>
            <div className="space-y-3">{groups.map((group) => <SourceGroupCard key={group.key} group={group} anchor={anchor} />)}</div>
          </div>
        </div>
      )}
    </div>
  );
}

/** Where the answer comes from, in plain words, and how closely the documents matched the question. */
function answerBasis(answer: Answer) {
  const plan = answer.plan;
  const when = plan.as_of ? formatDate(plan.as_of) : null;
  const basis = plan.fallback?.used
    ? "Answered from an earlier version of the policy"
    : plan.query_class === "historical" && when ? `Answered from the policies in force on ${when}`
    : plan.query_class === "specific_version" ? "Answered from the version you asked about"
    : plan.query_class === "comparison" ? "Answered by comparing versions"
    : plan.query_class === "across_versions" ? "Answered from every version of the policy"
    : when ? `Answered from the policies in force today (${when})` : "Answered from your policies";
  const match = answer.evidence_score >= 0.6 ? "strong" : answer.evidence_score >= 0.35 ? "good" : "partial";
  return `${basis} · ${match} match with the documents`;
}

/** Points shown in the short answer when there is no plain-words summary. */
const SHORT_CLAIMS = 2;

/** Replies to a greeting or a question with no subject: not a failed search, a prompt to ask. */
const CONVERSATIONAL = ["GREETING", "NO_SUBJECT", "INSTRUCTIONS_IGNORED"];
/** The answer ran out of time or the AI service did not respond: nothing was decided about the documents. */
const TEMPORARY = ["DEADLINE_EXCEEDED", "LLM_UNAVAILABLE"];
/** The question needs one more detail before it can be searched. */
const NEEDS_DETAIL = ["NEEDS_CONTEXT", "VERSION_NOT_FOUND", "COMPARISON_TARGET_UNCLEAR"];
/** What the documents lacked: the topic is not there at all, or the closest text does not state the answer. */
const NOT_FOUND_HEADINGS: Record<string, string> = {
  KEY_TERMS_NOT_FOUND: "Not covered in your documents",
  NO_RELEVANT_DOCUMENTS: "Not covered in your documents",
  LOW_RELEVANCE: "Not covered in your documents",
  INSUFFICIENT_EVIDENCE: "Your documents don’t answer this directly",
  ANSWER_FAILED_VALIDATION: "Your documents don’t answer this directly",
  ANSWER_OFF_TOPIC: "Your documents don’t answer this directly",
  CLAIM_NOT_CONFIRMED: "Not confirmed by your documents",
};

function NoAnswer({ reason, onRetry }: { reason: Answer["no_answer"]; onRetry: () => void }) {
  if (!reason) return null;
  if (TEMPORARY.includes(reason.reason)) {
    return (
      <div className="p-5">
        <p className="font-medium text-ink">{reason.reason === "DEADLINE_EXCEEDED" ? "This took too long to answer" : "The AI service didn’t respond"}</p>
        <p className="mt-1 text-sm text-ink-soft">{reason.message}</p>
        <Button className="mt-3" size="sm" variant="secondary" onClick={onRetry}>Ask again</Button>
      </div>
    );
  }
  if (NEEDS_DETAIL.includes(reason.reason)) {
    return (
      <div className="p-5">
        <p className="font-medium text-ink">I need a little more detail</p>
        <p className="mt-1 text-sm text-ink-soft">{reason.message}</p>
      </div>
    );
  }
  if (CONVERSATIONAL.includes(reason.reason)) {
    return (
      <div className="p-5">
        <p className="flex items-start gap-2 text-[15px] leading-7 text-ink"><Sparkles className="mt-1.5 size-4 shrink-0 text-ai-600" />{reason.message}</p>
      </div>
    );
  }
  if (reason.reason === "AMBIGUOUS") {
    // Found, but several rules match with different values: ask which one, never pick.
    return (
      <div className="p-5">
        <p className="font-medium text-ink">Which one do you mean?</p>
        <p className="mt-1 text-sm text-ink-soft">{reason.message}</p>
        {!!reason.suggestions.length && (
          <div className="mt-3">
            <p className="text-xs font-medium text-muted">Matching rules:</p>
            <ul className="mt-1 list-disc space-y-0.5 pl-4 text-xs text-ink-soft">{reason.suggestions.map((suggestion) => <li key={suggestion}>{suggestion}</li>)}</ul>
          </div>
        )}
      </div>
    );
  }
  return (
    <div className="p-5">
      <p className="font-medium text-ink">{NOT_FOUND_HEADINGS[reason.reason] ?? "Not covered in your documents"}</p>
      <p className="mt-1 text-sm text-ink-soft">{reason.message}</p>
      {reason.missing_terms.length > 0 && (
        <p className="mt-2 text-xs text-muted">Not mentioned anywhere in your documents: <span className="font-medium text-ink-soft">{reason.missing_terms.join(", ")}</span></p>
      )}
      {!!reason.suggestions.length && (
        <div className="mt-3">
          <p className="text-xs font-medium text-muted">Try:</p>
          <ul className="mt-1 list-disc space-y-0.5 pl-4 text-xs text-ink-soft">{reason.suggestions.map((suggestion) => <li key={suggestion}>{suggestion}</li>)}</ul>
        </div>
      )}
    </div>
  );
}
