import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Check, CheckCircle2, ChevronDown, Copy, FileWarning, GitBranch, Link2, Lightbulb, Sparkles, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router";
import { api, ApiError } from "../api/client";
import type { Analysis, DocumentRead, Signal } from "../api/types";
import { formatDate, humanize, percent } from "../lib/format";
import { useAuth, useToast } from "../store/hooks";
import { CategoryPicker } from "./CategoryPicker";
import { Badge, Button, Card, CardHeader, Confidence, Field, Input, SuggestedBadge, Textarea } from "./ui";

type Action = "create_policy" | "add_version" | "reject";

const DECISION_TITLES: Record<string, string> = {
  NEW_POLICY: "New policy detected",
  EXISTING_POLICY_NEW_VERSION: "Existing policy found",
  EXISTING_POLICY_AMENDMENT: "Amendment detected",
  EXACT_DUPLICATE: "Duplicate document detected",
  CONTENT_DUPLICATE: "Duplicate content detected",
  VERSION_CONFLICT: "Version conflict",
  POSSIBLE_MATCH_REQUIRES_REVIEW: "Possible match — please review",
};
const WARNING_DECISIONS = ["EXACT_DUPLICATE", "CONTENT_DUPLICATE", "VERSION_CONFLICT"];

const shorten = (label: string) => (label.length > 40 ? `${label.slice(0, 37)}...` : label);

function SignalList({ signals }: { signals: Signal[] }) {
  return (
    <ul className="space-y-1.5 text-sm">
      {signals.map((s) => (
        <li key={s.signal} className="flex items-center gap-2">
          {s.matched ? <Check className="size-4 text-ok-600" /> : <X className="size-4 text-bad-600" />}
          <span className={s.matched ? "text-ink" : "text-muted"}>{s.detail}</span>
        </li>
      ))}
    </ul>
  );
}

export function AnalysisReview({ document, analysis }: { document: DocumentRead; analysis: Analysis }) {
  const { can } = useAuth();
  const toast = useToast();
  const queryClient = useQueryClient();
  const matched = analysis.matched_policy && !analysis.matched_policy.restricted ? analysis.matched_policy : null;
  const isDuplicate = ["EXACT_DUPLICATE", "CONTENT_DUPLICATE"].includes(analysis.decision);
  // A copy of a filed document is most often its reissue: a new version of the same policy.
  // A new-policy upload with a match was uploaded together with files already filed under it.
  const suggestsVersion = ["EXISTING_POLICY_NEW_VERSION", "VERSION_CONFLICT", "POSSIBLE_MATCH_REQUIRES_REVIEW", "EXACT_DUPLICATE", "CONTENT_DUPLICATE", "NEW_POLICY"].includes(analysis.decision) && matched;

  const [action, setAction] = useState<Action>(suggestsVersion ? "add_version" : "create_policy");
  const [name, setName] = useState(analysis.suggested_name ?? "");
  const [categoryId, setCategoryId] = useState(analysis.suggested_category_id ?? "");
  const [policyNumber, setPolicyNumber] = useState(analysis.detected.policy_number.value ?? "");
  const uploadReason = (document as DocumentRead & { duplicate_override_reason?: string | null }).duplicate_override_reason ?? "";
  const [reason, setReason] = useState(uploadReason);
  const [newRevision, setNewRevision] = useState(false);
  const [showMatch, setShowMatch] = useState(false);
  const [showSuggestions, setShowSuggestions] = useState(false);
  const [targets, setTargets] = useState<string[]>(analysis.amendment_targets.map((t) => t.policy_id));
  const [error, setError] = useState<string | null>(null);
  const suggestionsRef = useRef<HTMLDivElement>(null);
  const nameRef = useRef<HTMLInputElement>(null);

  // The version label and effective date are read from the document, never typed.
  // This is what the server will register, shown before anything is applied.
  const detectedLabel = analysis.detected.version_label.value;
  const autoLabel = analysis.suggested_initial_version?.version_label
    ?? (action === "create_policy" ? "1" : "…");
  const labelSource = detectedLabel
    ? "read from the document"
    : action === "create_policy" ? "first version of the new policy" : "next in the policy's history";
  const dateSource = analysis.detected.effective_date
    ? analysis.detected.effective_date_evidence
      ? `stated in the document: “${analysis.detected.effective_date_evidence}”`
      : "found in the document"
    : "not stated in the document — the upload date is used";

  // Build a deduplicated list of name suggestions from detected metadata
  const nameSuggestions = (() => {
    const seen = new Set<string>();
    const candidates: { label: string; source: string }[] = [];
    const add = (val: string | null | undefined, source: string) => {
      if (!val?.trim() || seen.has(val.trim())) return;
      seen.add(val.trim());
      candidates.push({ label: val.trim(), source });
    };
    add(analysis.suggested_name, "AI suggested");
    add(analysis.detected.title.evidence, "Detected from document");
    // Add issuer + doc-type combos as extra hints when title confidence is low
    if (analysis.name_confidence < 0.7) {
      const issuer = analysis.detected.issuer.value;
      const dept = analysis.detected.department.value;
      const docNum = analysis.detected.circular_number.value || analysis.detected.document_number.value;
      if (issuer && docNum) add(`${issuer} — ${docNum}`, "Issuer + reference");
      if (dept && docNum) add(`${dept} — ${docNum}`, "Department + reference");
    }
    return candidates;
  })();

  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (suggestionsRef.current && !suggestionsRef.current.contains(event.target as Node)) {
        setShowSuggestions(false);
      }
    }
    if (showSuggestions) {
      window.addEventListener("mousedown", handleClickOutside);
      return () => window.removeEventListener("mousedown", handleClickOutside);
    }
  }, [showSuggestions]);

  useEffect(() => {
    if (!categoryId && analysis.suggested_category_id) setCategoryId(analysis.suggested_category_id);
  }, [analysis.suggested_category_id, categoryId]);

  // A reason is asked once: not again if it was given at upload, and not for the reissue
  // (a copy added as a new version of its own policy), which the confirmation itself records.
  const reasonNeededFor = (chosen: Action) =>
    WARNING_DECISIONS.includes(analysis.decision) && !uploadReason.trim() && !(isDuplicate && chosen === "add_version");
  const needsReason = reasonNeededFor(action);
  const canConfirm = can("policies:manage");

  const confirm = useMutation({
    mutationFn: (chosen: Action) => {
      // Nothing about the version is typed: the server reads the label and the
      // effective date from the document, falling back to the running number and
      // the upload date. The reviewer only decides which action to take.
      const body: Record<string, unknown> = { action: chosen, reason: reason || undefined, version: {} };
      if (chosen === "create_policy") {
        body.policy = {
          name, category_id: categoryId, policy_number: policyNumber || undefined,
          document_number: analysis.detected.circular_number.value ?? analysis.detected.document_number.value ?? undefined,
          issuer: analysis.detected.issuer.value ?? undefined,
          issuing_department: analysis.detected.department.value ?? undefined,
        };
        body.relationships = analysis.amendment_targets
          .filter((t) => targets.includes(t.policy_id))
          .map((t) => ({ relation_type: t.relation_type, target_policy_id: t.policy_id, target_version_id: t.version_id, clauses: t.clauses, evidence: t.evidence }));
      } else if (chosen === "add_version") {
        body.policy_id = matched?.policy_id;
        if (newRevision) body.conflict_resolution = "save_as_new_revision";
      }
      return api.post<DocumentRead>(`/documents/${document.id}/confirm`, body);
    },
    onSuccess: (_doc, chosen) => {
      toast("ok", chosen === "reject" ? "Upload rejected." : "Confirmed. Indexing has started.");
      queryClient.invalidateQueries({ queryKey: ["document", document.id] });
      queryClient.invalidateQueries({ queryKey: ["analysis", document.id] });
      queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
    onError: (err) => {
      if (err instanceof ApiError && err.code === "VERSION_CONFLICT") setNewRevision(false);
      setError(err instanceof ApiError ? err.message : "Could not confirm.");
      // Someone else (or the bulk upload) may have filed it meanwhile: show its current state.
      queryClient.invalidateQueries({ queryKey: ["document", document.id] });
    },
  });

  const submit = (chosen: Action) => {
    setError(null);
    if (chosen !== "reject") {
      if (chosen === "create_policy" && (!name.trim() || !categoryId)) return setError("Name and category are required.");
      if (reasonNeededFor(chosen) && reason.trim().length < 5) {
        return setError("Please give a short reason (at least 5 characters) for going ahead despite the warning.");
      }
    }
    setAction(chosen);
    confirm.mutate(chosen);
  };

  // The effective date is optional, so its absence is not a problem to report.
  const missing = analysis.missing_fields.filter((field) => field !== "effective_date");
  const decisionTone = WARNING_DECISIONS.includes(analysis.decision) ? "warn" : analysis.decision === "POSSIBLE_MATCH_REQUIRES_REVIEW" ? "info" : "ai";
  const creating = action === "create_policy";

  if (analysis.review_status !== "pending") {
    return (
      <Card>
        <CardHeader title="Review completed" subtitle={`Resolution: ${humanize(String(analysis.resolution?.action ?? analysis.review_status))}`} />
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader
        title={<span className="flex items-center gap-2"><Sparkles className="size-4 text-ai-600" />Document analysis</span>}
        subtitle="Governix has already read the document. Check what it found and confirm it in one click."
        actions={<Badge tone={decisionTone}>{DECISION_TITLES[analysis.decision]}</Badge>}
      />
      <div className="grid gap-6 p-4 sm:p-5 lg:grid-cols-2">
        <div className="space-y-4">
          <ul className="space-y-1 text-sm text-ink-soft">
            {["File validated", "Text extracted", "Document classified", "Existing documents checked"].map((step) => (
              <li key={step} className="flex items-center gap-2"><CheckCircle2 className="size-4 text-ok-600" />{step}</li>
            ))}
          </ul>
          <Field label="Policy name" hint={
            analysis.detected.title.evidence
              ? `Detected from ${humanize(analysis.detected.title.source ?? "")}: "${analysis.detected.title.evidence}"`
              : analysis.name_confidence < 0.5
              ? "Name could not be reliably detected — please enter or pick a suggestion below"
              : "Generated from the document — please check"
          }>
            <div className="flex items-center gap-2">
              <div className="relative flex-1">
                <Input
                  ref={nameRef}
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  disabled={!creating}
                  placeholder="Name read from the document"
                  className="pr-8"
                />
                {nameSuggestions.length > 0 && creating && (
                  <button
                    type="button"
                    className="absolute inset-y-0 right-2 flex items-center text-muted hover:text-ink"
                    onClick={() => setShowSuggestions(!showSuggestions)}
                    title="Show name suggestions"
                  >
                    <ChevronDown className={`size-4 transition-transform ${showSuggestions ? "rotate-180" : ""}`} />
                  </button>
                )}
                {showSuggestions && nameSuggestions.length > 0 && (
                  <div
                    ref={suggestionsRef}
                    className="absolute left-0 right-0 top-full z-10 mt-1 rounded-md border border-line bg-surface shadow-lg"
                  >
                    <p className="flex items-center gap-1.5 border-b border-line px-3 py-1.5 text-xs font-medium text-muted">
                      <Lightbulb className="size-3" /> Suggestions based on document content
                    </p>
                    {nameSuggestions.map((s, i) => (
                      <button
                        key={i}
                        type="button"
                        className="flex w-full flex-col px-3 py-2 text-left text-sm hover:bg-subtle"
                        onClick={() => { setName(s.label); setShowSuggestions(false); }}
                      >
                        <span className="font-medium">{s.label}</span>
                        <span className="text-xs text-muted">{s.source}</span>
                      </button>
                    ))}
                  </div>
                )}
              </div>
              <SuggestedBadge />
            </div>
            {nameSuggestions.length > 0 && (
              <div className="mt-2 flex flex-wrap items-center gap-1.5">
                <span className="text-xs text-muted">
                  {creating ? "Suggestions:" : "Names found in the document:"}
                </span>
                {nameSuggestions.map((s, i) => (
                  creating ? (
                    <button
                      key={i}
                      type="button"
                      onClick={() => setName(s.label)}
                      className={`rounded-md border px-2 py-0.5 text-xs transition-colors ${
                        name === s.label
                          ? "border-primary-500 bg-primary-50 text-primary-700 font-medium"
                          : "border-line bg-surface hover:bg-subtle text-ink-soft"
                      }`}
                      title={s.source}
                    >
                      {shorten(s.label)}
                    </button>
                  ) : (
                    <span
                      key={i}
                      title={s.source}
                      className={`rounded-md border px-2 py-0.5 text-xs ${
                        name === s.label
                          ? "border-primary-500 bg-primary-50 font-medium text-primary-700"
                          : "border-line bg-surface text-ink-soft"
                      }`}
                    >
                      {shorten(s.label)}
                    </span>
                  )
                ))}
              </div>
            )}
            {!creating && (
              <p className="mt-1.5 text-xs text-muted">
                The name comes from this document. Choose &ldquo;create a new policy myself&rdquo; to name it
                something else.
              </p>
            )}
          </Field>
          <div className="text-xs text-muted">Name confidence <Confidence value={analysis.name_confidence} /></div>
          <Field label="Category" hint={analysis.category_ranking.length ? `Evidence: ${analysis.category_ranking[0].matched.slice(0, 5).join(", ")}` : undefined}>
            <div className="flex items-center gap-2">
              <div className="min-w-0 flex-1"><CategoryPicker value={categoryId} onChange={setCategoryId} disabled={!creating} /></div>
              <Confidence value={analysis.category_confidence} />
            </div>
          </Field>
          <Field label="Policy number" hint="Taken from the document. Leave empty if it states none.">
            <Input value={policyNumber} onChange={(e) => setPolicyNumber(e.target.value)} disabled={!creating} />
          </Field>
          <AutoVersionCard label={autoLabel} labelSource={labelSource} date={analysis.detected.effective_date ?? null} dateSource={dateSource} />
          {missing.length > 0 && (
            <p className="flex items-start gap-2 rounded-md bg-warn-50 p-2.5 text-xs text-warn-600">
              <AlertTriangle className="size-4 shrink-0" /> Could not detect: {missing.map(humanize).join(", ")}.
            </p>
          )}
        </div>

        <div className="space-y-4">
          {matched && !WARNING_DECISIONS.includes(analysis.decision)
            ? <RelationPrompt
                analysis={analysis} matched={matched} autoLabel={autoLabel} showMatch={showMatch}
                canConfirm={canConfirm} pending={confirm.isPending && action === "add_version"}
                onToggleMatch={() => setShowMatch(!showMatch)}
                onAgree={() => submit("add_version")}
                onCreateManually={() => { setAction("create_policy"); nameRef.current?.focus(); }}
              />
            : <DecisionPanel analysis={analysis} document={document} />}

          {analysis.amendment_targets.length > 0 && (
            <div className="rounded-md border border-line p-3">
              <p className="mb-2 flex items-center gap-1.5 text-sm font-medium"><Link2 className="size-4" />Relationships to record</p>
              {analysis.amendment_targets.map((t) => (
                <label key={t.policy_id} className="flex items-start gap-2 py-1 text-sm">
                  <input type="checkbox" className="mt-1" checked={targets.includes(t.policy_id)}
                    onChange={(e) => setTargets(e.target.checked ? [...targets, t.policy_id] : targets.filter((x) => x !== t.policy_id))} />
                  <span>
                    <Badge tone="brand">{t.relation_type}</Badge>{" "}
                    <span className="font-medium">{t.policy_name}</span>
                    {t.version_label && <span className="text-muted"> v{t.version_label}</span>}
                    {t.clauses.length > 0 && <span className="text-muted"> · clause {t.clauses.join(", ")}</span>}
                    <span className="mt-0.5 block text-xs italic text-muted">“{t.evidence}”</span>
                  </span>
                </label>
              ))}
            </div>
          )}

          {needsReason && (
            <Field label="Reason (required to proceed despite the warning)">
              <Textarea rows={2} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="e.g. Corrected reissue approved by Credit Committee" />
            </Field>
          )}
          {analysis.decision === "VERSION_CONFLICT" && matched && (
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={newRevision} onChange={(e) => setNewRevision(e.target.checked)} />
              Save as a new revision of the same version (keeps the existing one untouched)
            </label>
          )}
          {error && <p className="rounded-md bg-bad-50 px-3 py-2 text-sm text-bad-600" role="alert">{error}</p>}

          {canConfirm ? (
            <div className="flex flex-wrap gap-2 pt-1">
              {/* Creating a new policy: one button, the name and category are already filled in. */}
              {creating && (
                <Button loading={confirm.isPending && action === "create_policy"} onClick={() => submit("create_policy")}>
                  <Sparkles className="size-4" />{matched ? `Create "${name.trim() || "new policy"}" as a separate policy` : "Create this policy"}
                </Button>
              )}
              {/* A warning (duplicate, conflict) keeps the matched policy on a separate
                  panel, so its "add as a version" choice is offered down here. */}
              {matched && WARNING_DECISIONS.includes(analysis.decision) && (
                <>
                  <Button loading={confirm.isPending && action === "add_version"} onClick={() => submit("add_version")}>
                    <GitBranch className="size-4" />Add as v{autoLabel} of {matched.name}
                  </Button>
                  <Button variant="secondary" onClick={() => { setAction("create_policy"); nameRef.current?.focus(); }}>Create a separate policy</Button>
                </>
              )}
              <Button variant="ghost" className="text-bad-600" loading={confirm.isPending && action === "reject"} onClick={() => submit("reject")}>Reject upload</Button>
            </div>
          ) : (
            <p className="rounded-md bg-info-50 p-3 text-sm text-info-600">A branch manager or organization admin will review and confirm this document.</p>
          )}
        </div>
      </div>
    </Card>
  );
}

/** Read-only: what the server will register, and where each value came from. */
function AutoVersionCard({ label, labelSource, date, dateSource }: {
  label: string; labelSource: string; date: string | null; dateSource: string;
}) {
  return (
    <div className="rounded-md border border-line bg-subtle/40 p-3">
      <p className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-muted">
        <Sparkles className="size-3" />Version and effective date — detected, not typed
      </p>
      <dl className="mt-2 grid grid-cols-2 gap-3 text-sm">
        <div>
          <dt className="text-xs text-muted">Version</dt>
          <dd className="font-medium">{label === "…" ? "The next one" : `v${label}`}</dd>
          <dd className="text-xs text-muted">{labelSource}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted">Effective from</dt>
          <dd className="font-medium">{date ? formatDate(date) : "Not stated"}</dd>
          <dd className="text-xs text-muted">{dateSource}</dd>
        </div>
      </dl>
    </div>
  );
}

/** The one question the reviewer answers: is this the same policy, or a different one? */
function RelationPrompt({ analysis, matched, autoLabel, showMatch, canConfirm, pending, onToggleMatch, onAgree, onCreateManually }: {
  analysis: Analysis; matched: NonNullable<Analysis["matched_policy"]>; autoLabel: string; showMatch: boolean;
  canConfirm: boolean; pending: boolean;
  onToggleMatch: () => void; onAgree: () => void; onCreateManually: () => void;
}) {
  const detectedVersion = analysis.detected.version_label.value;
  return (
    <div className="rounded-md border-2 border-brand-500/40 bg-brand-50/60 p-4 text-sm">
      <p className="text-xs font-medium uppercase tracking-wide text-brand-700">This content relates to an existing policy</p>
      <Link to={`/policies/${matched.policy_id}`} className="mt-1 block text-base font-semibold text-brand-700 hover:underline">
        {matched.name}
      </Link>
      <dl className="mt-3 grid grid-cols-2 gap-3 text-xs">
        <div><dt className="text-muted">Policy number</dt><dd className="font-medium">{matched.policy_number ?? "—"}</dd></div>
        <div><dt className="text-muted">Match confidence</dt><dd className="font-medium"><Confidence value={analysis.confidence} /></dd></div>
        <div><dt className="text-muted">Its current version</dt><dd className="font-medium">{matched.latest_version ? `v${matched.latest_version.label} · ${formatDate(matched.latest_version.effective_from)}` : "None in force"}</dd></div>
        <div><dt className="text-muted">This document</dt><dd className="font-medium">{detectedVersion ? `v${detectedVersion}` : "No version stated"}{analysis.detected.effective_date ? ` · ${formatDate(analysis.detected.effective_date)}` : ""}</dd></div>
      </dl>
      {analysis.detected.notes?.includes("HISTORICAL_VERSION") && (
        <p className="mt-3 rounded bg-info-50 p-2 text-xs text-info-600">This is an older version; it will be inserted into the history, not made current.</p>
      )}
      <div className="mt-3 border-t border-brand-200 pt-3 text-xs">
        <p className="mb-1 font-medium">Why Governix says so</p>
        <SignalList signals={analysis.signals} />
        {showMatch && analysis.candidates.length > 1 && (
          <div className="mt-2">
            <p className="mb-1 font-medium">Other candidates</p>
            {analysis.candidates.slice(1).map((c, i) => (
              <p key={i} className="text-muted">{c.restricted ? "Restricted document" : c.name} — {percent(c.score)}</p>
            ))}
          </div>
        )}
      </div>
      <p className="mt-4 font-medium">Is this a new version of {matched.name}?</p>
      {canConfirm ? (
        <div className="mt-2 flex flex-wrap gap-2">
          <Button loading={pending} onClick={onAgree}>
            <GitBranch className="size-4" />Yes — add it as {autoLabel === "…" ? "the next version" : `v${autoLabel}`}
          </Button>
          <Button variant="secondary" onClick={onCreateManually}>No — create a new policy myself</Button>
          <Button variant="ghost" onClick={onToggleMatch}>{showMatch ? "Hide" : "Review"} match</Button>
        </div>
      ) : (
        <p className="mt-2 rounded-md bg-info-50 p-3 text-info-600">A branch manager or organization admin will review and confirm this document.</p>
      )}
    </div>
  );
}

function DecisionPanel({ analysis, document }: { analysis: Analysis; document: DocumentRead }) {
  const detectedVersion = analysis.detected.version_label.value;
  const conflict = analysis.conflict;

  if (analysis.decision === "EXACT_DUPLICATE" || analysis.decision === "CONTENT_DUPLICATE") {
    return (
      <div className="rounded-md border border-warn-600/30 bg-warn-50 p-4 text-sm">
        <p className="flex items-center gap-2 font-medium text-warn-600"><Copy className="size-4" />This content already exists.</p>
        <p className="mt-1 text-ink-soft">
          {conflict?.restricted ? conflict.message
            : conflict?.match === "sha256" ? "SHA-256: identical file."
            : conflict?.match === "text" ? "Identical text in a different file."
            : `Near-identical content (${percent(conflict?.similarity)} similar), e.g. a re-scan.`}
        </p>
        {analysis.duplicate_of_document_id && (
          <Link to={`/documents/${analysis.duplicate_of_document_id}`} className="mt-2 inline-block text-sm font-medium text-brand-700 hover:underline">View existing document</Link>
        )}
      </div>
    );
  }

  if (analysis.decision === "VERSION_CONFLICT") {
    return (
      <div className="rounded-md border border-bad-600/30 bg-bad-50 p-4 text-sm">
        <p className="flex items-center gap-2 font-medium text-bad-600"><FileWarning className="size-4" />Version conflict</p>
        <p className="mt-1 text-ink-soft">{conflict?.message}</p>
        {conflict?.existing_version && (
          <dl className="mt-3 grid grid-cols-2 gap-2 text-xs">
            <div><dt className="text-muted">Existing version</dt><dd className="font-medium">v{conflict.existing_version.label} · {formatDate(conflict.existing_version.effective_from)}</dd></div>
            <div><dt className="text-muted">Uploaded version</dt><dd className="font-medium">v{detectedVersion ?? "?"} · {formatDate(analysis.detected.effective_date)}</dd></div>
          </dl>
        )}
        <div className="mt-3 flex gap-3 text-xs">
          {conflict?.existing_version?.document_id && (
            <Link className="font-medium text-brand-700 hover:underline" to={`/documents/${conflict.existing_version.document_id}/view`}>Open existing</Link>
          )}
          <Link className="font-medium text-brand-700 hover:underline" to={`/documents/${document.id}/view`}>Open uploaded</Link>
        </div>
      </div>
    );
  }

  if (analysis.matched_policy?.restricted) {
    return <p className="rounded-md bg-info-50 p-3 text-sm text-info-600">{analysis.matched_policy.message} An administrator can link it.</p>;
  }

  return (
    <div className="rounded-md border border-line p-4 text-sm">
      <p className="font-medium">{analysis.decision === "EXISTING_POLICY_AMENDMENT" ? "This document amends existing policies" : "No existing policy matches this document"}</p>
      <p className="mt-1 text-muted">
        {analysis.decision === "EXISTING_POLICY_AMENDMENT"
          ? "It will be registered as its own document and linked to the policies it amends — the originals are not modified."
          : `A new policy will be created with version ${analysis.suggested_initial_version?.version_label ?? "1"}.`}
      </p>
    </div>
  );
}
