import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, ArrowDown, ArrowRight, ArrowUp, CheckCircle2, FileText, Loader2, UploadCloud, X, XCircle } from "lucide-react";
import { useMemo, useRef, useState, type DragEvent } from "react";
import { Link, useLocation, useNavigate } from "react-router";
import { api } from "../api/client";
import type { DuplicateResult, PolicySuggestion, UploadLimits } from "../api/types";
import { cn, formatBytes, formatDate } from "../lib/format";
import { cancelUpload, readPolicy, sha256, startUpload, type DraftFile, type StartOptions } from "../lib/uploadManager";
import { misplaced, policyFit, WhyElsewhere, type Suggestion } from "./PolicyFit";
import { StageTrail } from "./domain";
import { familyKey, orderVersions } from "../lib/versioning";
import { useAppDispatch, useAppSelector } from "../store/hooks";
import { jobDismissed, jobOpened, type QueueItem, type UploadJob } from "../store/uploadsSlice";
import { Badge, Button, Field, Modal, ProgressBar, Textarea } from "./ui";

type CheckState = "checking" | "ready" | "invalid" | "duplicate";

interface Draft {
  key: string;
  file: File;
  state: CheckState;
  message: string | null;
  sha: string | null;
  duplicate: DuplicateResult | null;
  uploadAnyway: boolean;
  /** Adding versions: what the content says, so a file meant for another policy is caught. */
  read: Suggestion;
  /** For a file that does not fit the policy: keep it here, or send it where it belongs. */
  choice: "here" | "elsewhere" | null;
}

const PARALLEL_READS = 3;

let sequence = 0;
const nextKey = () => `f${(sequence += 1)}`;

function useUploadLimits() {
  return useQuery({
    queryKey: ["upload-limits"],
    queryFn: () => api.get<UploadLimits>("/documents/upload-limits"),
    staleTime: 600_000,
  });
}

/**
 * Upload in Bulk (spec §7-8). Files are checked before anything is sent: type,
 * size, emptiness, the same file twice, and a duplicate of an existing document
 * (a warning, never a silent overwrite). Once started the upload runs in the
 * background; this dialog then shows its live queue.
 */
export function BulkUploadModal({
  open, onClose, title, link, categoryId, policy,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  link: string;
  categoryId?: string;
  /** Add the files as new versions of this policy. */
  policy?: { id: string; name: string } | null;
}) {
  const limits = useUploadLimits();
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [reason, setReason] = useState("");
  const [over, setOver] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  const patch = (key: string, change: Partial<Draft>) =>
    setDrafts((current) => current.map((d) => (d.key === key ? { ...d, ...change } : d)));

  function validate(file: File): string | null {
    const accepted = limits.data?.accepted_extensions ?? [".pdf"];
    if (!accepted.some((ext) => file.name.toLowerCase().endsWith(ext))) return "Only PDF documents are supported.";
    if (file.size === 0) return "The file is empty.";
    const max = limits.data?.max_size_bytes;
    if (max && file.size > max) return `Larger than the ${formatBytes(max)} limit.`;
    return null;
  }

  async function addFiles(list: File[]) {
    const added: Draft[] = list.map((file) => {
      const problem = validate(file);
      return {
        key: nextKey(), file, state: problem ? "invalid" : "checking", message: problem, sha: null, duplicate: null,
        uploadAnyway: false, read: policy && !problem ? "reading" : null, choice: null,
      };
    });
    // New files arrive oldest first (by the version in their names), after those already listed.
    setDrafts((current) => [...current, ...orderVersions(added)]);
    const checking = added.filter((d) => d.state === "checking");
    if (policy) void readAll(checking, policy.id);
    const hashes = new Map<string, string>();
    for (const draft of checking) {
      // One at a time: hashing reads the whole file into memory.
      const sha = await sha256(draft.file).catch(() => null);
      if (sha) hashes.set(draft.key, sha);
      patch(draft.key, { sha });
    }
    const known = new Set(drafts.filter((d) => d.sha && d.state !== "invalid").map((d) => d.sha));
    const results = new Map<string, DuplicateResult>();
    const unique = [...new Set(hashes.values())].filter((sha) => !known.has(sha));
    if (unique.length) {
      try {
        for (const r of await api.post<DuplicateResult[]>("/documents/duplicates", { sha256: unique })) results.set(r.sha256, r);
      } catch {
        // The server checks again on upload; a failed pre-check is not a blocker.
      }
    }
    const seen = new Set(known);
    for (const draft of checking) {
      const sha = hashes.get(draft.key);
      if (sha && seen.has(sha)) {
        patch(draft.key, { state: "invalid", message: "The same file is already in this upload." });
        continue;
      }
      if (sha) seen.add(sha);
      const match = sha ? results.get(sha) : undefined;
      if (match?.duplicate) {
        patch(draft.key, {
          state: "duplicate", duplicate: match,
          message: match.restricted
            ? "An identical document exists in an area you cannot access."
            : `Identical to "${match.existing?.title ?? match.existing?.original_filename}" uploaded ${formatDate(match.existing?.uploaded_at)}.`,
        });
      } else {
        patch(draft.key, { state: "ready", message: sha ? null : "Too large to pre-check; duplicates are checked on upload." });
      }
    }
  }

  async function readAll(list: Draft[], policyId: string) {
    let next = 0;
    const reader = async () => {
      while (next < list.length) {
        const draft = list[next];
        next += 1;
        patch(draft.key, { read: await readPolicy(draft.file, policyId) });
      }
    };
    await Promise.all(Array.from({ length: Math.min(PARALLEL_READS, list.length) }, reader));
  }

  // Adding versions: the order the files are listed in is the order they become versions.
  function shift(key: string, delta: -1 | 1) {
    setDrafts((current) => {
      const from = current.findIndex((d) => d.key === key);
      const to = from + delta;
      if (from < 0 || to < 0 || to >= current.length) return current;
      const next = [...current];
      [next[from], next[to]] = [next[to], next[from]];
      return next;
    });
  }
  const staying = policy ? drafts.filter((d) => d.state !== "invalid" && d.choice !== "elsewhere") : [];

  const families = useMemo(() => {
    const counts = new Map<string, number>();
    for (const d of drafts) if (d.state !== "invalid") counts.set(familyKey(d.file.name), (counts.get(familyKey(d.file.name)) ?? 0) + 1);
    return counts;
  }, [drafts]);
  const sendable = drafts.filter((d) => d.state === "ready" || (d.state === "duplicate" && d.uploadAnyway && !d.duplicate?.restricted));
  const needsReason = sendable.some((d) => d.state === "duplicate");
  const fitOf = (d: Draft) => (policy ? policyFit(d.read, policy.id) : "unknown");
  const checking = drafts.some((d) => d.state === "checking" || (d.state !== "invalid" && d.read === "reading"));
  const undecided = sendable.filter((d) => misplaced(fitOf(d)) && !d.choice).length;
  const canStart = sendable.length > 0 && !checking && !undecided && (!needsReason || reason.trim().length >= 5);

  function start() {
    // A file sent elsewhere goes to the policy it matches, or becomes the new policy it describes.
    const jobs = new Map<string, { options: Omit<StartOptions, "files">; files: DraftFile[] }>();
    for (const d of sendable) {
      const read = d.read as PolicySuggestion | null;
      const other = read?.existing_policy;
      const [key, options]: [string, Omit<StartOptions, "files">] = d.choice !== "elsewhere" || !read
        ? ["here", { title, link, categoryId: policy ? undefined : categoryId, policyId: policy?.id }]
        : other
          ? [other.policy_id, { title: `New versions of ${other.name}`, link: `/policies/${other.policy_id}?tab=versions`, policyId: other.policy_id }]
          : [`new:${read.name}`, { title: `New policy: ${read.name ?? d.file.name}`, link: "/policies", newPolicyName: read.name ?? undefined, categoryId: read.category_id ?? undefined }];
      const job = jobs.get(key) ?? { options: { ...options, duplicateReason: needsReason ? reason.trim() : undefined }, files: [] };
      job.files.push({ key: d.key, file: d.file, uploadAnyway: d.state === "duplicate" });
      jobs.set(key, job);
    }
    for (const job of jobs.values()) startUpload({ ...job.options, files: job.files });
    setDrafts([]);
    setReason("");
    onClose();
  }

  function close() {
    setDrafts([]);
    setReason("");
    onClose();
  }

  function onDrop(event: DragEvent) {
    event.preventDefault();
    setOver(false);
    void addFiles(Array.from(event.dataTransfer.files));
  }

  return (
    <Modal
      open={open}
      onClose={close}
      wide
      title={policy ? `Add versions to ${policy.name}` : `Upload in bulk to ${title}`}
      footer={<>
        <p className="mr-auto self-center text-xs text-muted">
          {undecided
            ? `${undecided} file${undecided === 1 ? " doesn't" : "s don't"} look like ${policy?.name}: choose where ${undecided === 1 ? "it goes" : "they go"}`
            : drafts.length ? `${sendable.length} of ${drafts.length} ready` : "Uploads continue in the background after you start them."}
        </p>
        <Button variant="secondary" onClick={close}>Cancel</Button>
        <Button disabled={!canStart} onClick={start}>
          <UploadCloud className="size-4" />Upload {sendable.length || ""} document{sendable.length === 1 ? "" : "s"}
        </Button>
      </>}
    >
      <div
        onDragOver={(e) => { e.preventDefault(); setOver(true); }}
        onDragLeave={() => setOver(false)}
        onDrop={onDrop}
        className={cn(
          "flex flex-col items-center justify-center rounded-lg border-2 border-dashed px-6 py-10 text-center transition-colors",
          over ? "border-brand-500 bg-brand-50" : "border-line-strong",
        )}
      >
        <UploadCloud className="mb-3 size-10 text-brand-500" />
        <p className="font-medium">Drag &amp; drop documents here</p>
        <p className="my-2 text-sm text-muted">or</p>
        <Button variant="secondary" onClick={() => input.current?.click()}>Browse files</Button>
        <p className="mt-3 text-xs text-muted">
          PDF only{limits.data ? ` · up to ${formatBytes(limits.data.max_size_bytes)} each` : ""} · select several at once
          {policy
            ? " · each file is read first: one that looks like another policy is pointed out before anything is uploaded"
            : " · files named alike (e.g. v1, v2) become versions of one policy"}
        </p>
        <input ref={input} type="file" multiple accept="application/pdf,.pdf" className="hidden"
          onChange={(e) => { void addFiles(Array.from(e.target.files ?? [])); e.target.value = ""; }} />
      </div>

      {policy && staying.length > 1 && (
        <p className="mt-4 text-xs text-muted">
          Listed oldest first: use the arrows to change the order. The last file becomes the latest version of {policy.name},
          unless the documents state their own effective dates.
        </p>
      )}
      {drafts.length > 0 && (
        <div className={cn("overflow-hidden rounded-md border border-line", policy && staying.length > 1 ? "mt-2" : "mt-4")}>
          <table className="w-full text-left text-sm">
            <thead className="border-b border-line bg-subtle/60 text-xs uppercase tracking-wide text-muted">
              <tr><th className="px-3 py-2 font-medium">Document</th><th className="px-3 py-2 font-medium">Size</th><th className="px-3 py-2 font-medium">Status</th><th className="w-10" /></tr>
            </thead>
            <tbody className="divide-y divide-line">
              {drafts.map((d, index) => {
                const family = families.get(familyKey(d.file.name)) ?? 0;
                const place = staying.indexOf(d);
                return (
                  <tr key={d.key} className="align-top">
                    <td className="px-3 py-2">
                      <div className="flex items-center gap-2">
                        {policy && place >= 0 && (
                          <Badge tone={place === staying.length - 1 ? "brand" : "neutral"} className="w-16 shrink-0 justify-center">
                            {staying.length === 1 ? "New" : place === staying.length - 1 ? "Latest" : place === 0 ? "Oldest" : `#${place + 1}`}
                          </Badge>
                        )}
                        <FileText className="size-4 shrink-0 text-brand-500" /><span className="break-all">{d.file.name}</span>
                      </div>
                      {!policy && family > 1 && d.state !== "invalid" && <p className="mt-0.5 pl-6 text-xs text-muted">One of {family} versions of the same document</p>}
                      {d.message && <p className={cn("mt-0.5 pl-6 text-xs", d.state === "invalid" ? "text-bad-600" : d.state === "duplicate" ? "text-warn-600" : "text-muted")}>{d.message}</p>}
                      {policy && d.state !== "invalid" && (
                        <FitNote draft={d} fit={fitOf(d)} policyName={policy.name} onChoose={(choice) => patch(d.key, { choice })} />
                      )}
                      {d.state === "duplicate" && !d.duplicate?.restricted && (
                        <label className="mt-1 flex items-center gap-2 pl-6 text-xs">
                          <input type="checkbox" checked={d.uploadAnyway} onChange={(e) => patch(d.key, { uploadAnyway: e.target.checked })} />
                          Upload anyway
                        </label>
                      )}
                    </td>
                    <td className="whitespace-nowrap px-3 py-2 text-ink-soft">{formatBytes(d.file.size)}</td>
                    <td className="px-3 py-2"><CheckBadge state={d.state === "ready" && d.read === "reading" ? "checking" : d.state} /></td>
                    <td className="px-2 py-2">
                      <div className="flex items-center gap-0.5">
                        {policy && drafts.length > 1 && (
                          <>
                            <button type="button" className="rounded p-1 text-muted hover:bg-subtle disabled:opacity-30" disabled={index === 0}
                              onClick={() => shift(d.key, -1)} aria-label={`Move ${d.file.name} earlier`} title="Older"><ArrowUp className="size-4" /></button>
                            <button type="button" className="rounded p-1 text-muted hover:bg-subtle disabled:opacity-30" disabled={index === drafts.length - 1}
                              onClick={() => shift(d.key, 1)} aria-label={`Move ${d.file.name} later`} title="Newer"><ArrowDown className="size-4" /></button>
                          </>
                        )}
                        <button type="button" className="rounded p-1 text-muted hover:bg-subtle" aria-label={`Remove ${d.file.name}`}
                          onClick={() => setDrafts((current) => current.filter((x) => x.key !== d.key))}><X className="size-4" /></button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {needsReason && (
        <div className="mt-4">
          <Field label="Reason for uploading duplicates *" hint="Recorded in the audit log.">
            <Textarea rows={2} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="e.g. Re-issued signed copy for the audit file" />
          </Field>
        </div>
      )}
    </Modal>
  );
}

/** Whether the content fits the policy; for a file that does not, where it seems to belong. */
function FitNote({ draft, fit, policyName, onChoose }: {
  draft: Draft; fit: ReturnType<typeof policyFit>; policyName: string; onChoose: (choice: Draft["choice"]) => void;
}) {
  const read = draft.read as PolicySuggestion;
  if (fit === "reading") {
    return <p className="mt-0.5 flex items-center gap-1.5 pl-6 text-xs text-muted"><Loader2 className="size-3 animate-spin" />Reading the document…</p>;
  }
  if (fit === "unknown") {
    return <p className="mt-0.5 pl-6 text-xs text-muted">Could not be read before upload; it is matched against this policy once processed.</p>;
  }
  if (fit === "match") {
    return (
      <p className="mt-0.5 flex items-center gap-1.5 pl-6 text-xs text-ok-600">
        <CheckCircle2 className="size-3.5" />Matches this policy{read.version_label ? ` · version ${read.version_label}` : ""}
      </p>
    );
  }
  const other = read.existing_policy;
  const elsewhere = other ? `Add to ${other.name}` : "Upload as new policy";
  if (draft.choice) {
    return (
      <p className="mt-0.5 flex flex-wrap items-center gap-1.5 pl-6 text-xs text-ink-soft">
        <ArrowRight className="size-3.5 text-brand-600" />
        {draft.choice === "here"
          ? <>Kept in {policyName}, although it looks like {other ? other.name : read.name ?? "a different policy"}.</>
          : other ? <>Will be added to {other.name} as a new version.</> : <>Will be uploaded as a new policy{read.name ? `: ${read.name}` : ""}.</>}
        <button type="button" className="font-medium text-brand-700 hover:underline" onClick={() => onChoose(null)}>Change</button>
      </p>
    );
  }
  return (
    <div className="ml-6 mt-1.5 space-y-2 rounded-md bg-warn-50 p-2.5 text-xs text-warn-600">
      <p className="flex gap-1.5">
        <AlertTriangle className="size-4 shrink-0" />
        <span><WhyElsewhere read={read} policyName={policyName} /></span>
      </p>
      <div className="flex flex-wrap gap-2 pl-5">
        {(!other || other.can_add_version) && <Button size="sm" onClick={() => onChoose("elsewhere")}>{elsewhere}</Button>}
        <Button size="sm" variant="secondary" onClick={() => onChoose("here")}>Keep in this policy</Button>
      </div>
    </div>
  );
}

function CheckBadge({ state }: { state: CheckState }) {
  if (state === "checking") return <Badge tone="info"><Loader2 className="size-3 animate-spin" />Checking</Badge>;
  if (state === "ready") return <Badge tone="ok">Ready</Badge>;
  if (state === "duplicate") return <Badge tone="warn">Duplicate</Badge>;
  return <Badge tone="bad">Not allowed</Badge>;
}

/* ---------- a running or finished upload ---------- */

const STATE_LABELS: Record<QueueItem["state"], [string, "neutral" | "info" | "ok" | "warn" | "bad"]> = {
  pending: ["Pending", "neutral"],
  uploading: ["Uploading", "info"],
  processing: ["Processing", "info"],
  registered: ["Completed", "ok"],
  review: ["Needs review", "warn"],
  failed: ["Failed", "bad"],
  cancelled: ["Cancelled", "neutral"],
};

function jobCounts(job: UploadJob) {
  const done = job.items.filter((i) => ["registered", "review", "failed", "cancelled"].includes(i.state)).length;
  const sent = job.items.filter((i) => i.state !== "pending" && i.state !== "uploading").length;
  const bytes = job.items.reduce((sum, i) => sum + i.size, 0) || 1;
  const uploaded = job.items.reduce((sum, i) => sum + i.size * (i.state === "uploading" ? i.progress : i.state === "pending" ? 0 : 1), 0);
  return { done, sent, total: job.items.length, fraction: uploaded / bytes };
}

export function UploadQueue({ job }: { job: UploadJob }) {
  return (
    <div className="overflow-hidden rounded-md border border-line">
      <table className="w-full text-left text-sm">
        <thead className="border-b border-line bg-subtle/60 text-xs uppercase tracking-wide text-muted">
          <tr>{["Document", "Size", "Status", "Progress", ""].map((h, i) => <th key={i} className="px-3 py-2 font-medium">{h}</th>)}</tr>
        </thead>
        <tbody className="divide-y divide-line">
          {job.items.map((item) => {
            const [label, tone] = STATE_LABELS[item.state];
            const cancellable = item.state === "pending" || item.state === "uploading";
            return (
              <tr key={item.key} className="align-top">
                <td className="px-3 py-2">
                  <div className="flex items-center gap-2"><StateIcon state={item.state} /><span className="break-all">{item.name}</span></div>
                  {item.message && <p className={cn("mt-0.5 pl-6 text-xs", item.state === "failed" ? "text-bad-600" : "text-ink-soft")}>{item.message}</p>}
                  {item.state === "review" && item.documentId && (
                    <Link to={`/documents/${item.documentId}`} className="pl-6 text-xs font-medium text-brand-700 hover:underline">Review</Link>
                  )}
                </td>
                <td className="whitespace-nowrap px-3 py-2 text-ink-soft">{formatBytes(item.size)}</td>
                <td className="px-3 py-2"><Badge tone={tone}>{label}</Badge></td>
                <td className="w-56 px-3 py-2">
                  {item.state === "uploading" ? (
                    <div className="flex items-center gap-2"><ProgressBar value={item.progress * 100} /><span className="w-9 text-right text-xs tabular-nums text-muted">{Math.round(item.progress * 100)}%</span></div>
                  ) : item.stages?.length ? <StageTrail stages={item.stages} />
                    : item.state === "pending" || item.state === "cancelled" ? <span className="text-muted">—</span>
                    : item.state === "processing" ? <span className="text-xs text-muted">Starting…</span>
                    : <span className="text-xs text-muted">Saved</span>}
                </td>
                <td className="px-2 py-2">
                  {cancellable && (
                    <button type="button" className="rounded p-1 text-muted hover:bg-subtle" aria-label={`Cancel ${item.name}`}
                      title="Cancel this upload" onClick={() => cancelUpload(job.id, item.key)}><X className="size-4" /></button>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function StateIcon({ state }: { state: QueueItem["state"] }) {
  if (state === "registered") return <CheckCircle2 className="size-4 shrink-0 text-ok-600" />;
  if (state === "review") return <AlertTriangle className="size-4 shrink-0 text-warn-600" />;
  if (state === "failed") return <XCircle className="size-4 shrink-0 text-bad-600" />;
  if (state === "cancelled") return <XCircle className="size-4 shrink-0 text-muted" />;
  if (state === "pending") return <FileText className="size-4 shrink-0 text-muted" />;
  return <Loader2 className="size-4 shrink-0 animate-spin text-info-600" />;
}

/** The job whose queue is open, shown over any page; it opens by itself with a summary when the job finishes. */
export function UploadJobModal() {
  const dispatch = useAppDispatch();
  const navigate = useNavigate();
  const location = useLocation();
  const job = useAppSelector((s) => s.uploads.jobs.find((j) => j.id === s.uploads.openJobId));
  if (!job) return null;
  const { done, total } = jobCounts(job);
  const finished = job.phase === "finished";
  const close = () => dispatch(jobOpened(null));
  const here = `${location.pathname}${location.search}` === job.link || location.pathname === job.link.split("?")[0];
  const review = job.items.find((i) => i.state === "review" && i.documentId);
  return (
    <Modal
      open
      wide
      onClose={close}
      title={finished ? "Upload finished" : job.title}
      footer={<>
        <p className="mr-auto self-center text-xs text-muted">
          {finished ? `${done} of ${total} settled` : "You can close this window; the upload continues in the background."}
        </p>
        {job.batchId && <Link to={`/uploads/${job.batchId}`} onClick={close}><Button variant="secondary">Details</Button></Link>}
        {finished && review && (
          <Button variant="secondary" onClick={() => { close(); navigate(`/documents/${review.documentId}`); }}>Review</Button>
        )}
        {finished && !here
          ? <Button onClick={() => { close(); navigate(job.link); }}>Open</Button>
          : !finished && <Link to={job.link} onClick={close}><Button variant="secondary">Open</Button></Link>}
        {(!finished || here) && <Button onClick={close}>{finished ? "Done" : "Close"}</Button>}
      </>}
    >
      {finished && <UploadSummary job={job} />}
      <UploadQueue job={job} />
    </Modal>
  );
}

/** One line on how an upload ended, above its queue. */
function UploadSummary({ job }: { job: UploadJob }) {
  const count = (state: QueueItem["state"]) => job.items.filter((i) => i.state === state).length;
  const [registered, review, failed] = [count("registered"), count("review"), count("failed")];
  const total = job.items.filter((i) => i.state !== "cancelled").length;
  const clean = registered === total;
  return (
    <div className={cn("mb-4 flex items-start gap-3 rounded-md p-3 text-sm", clean ? "bg-ok-50 text-ok-600" : "bg-warn-50 text-warn-600")}>
      {clean ? <CheckCircle2 className="mt-0.5 size-5 shrink-0" /> : <AlertTriangle className="mt-0.5 size-5 shrink-0" />}
      <div>
        <p className="font-medium">
          {clean
            ? `${total === 1 ? "The document is" : `All ${total} documents are`} uploaded and registered.`
            : [registered && `${registered} registered`, review && `${review} need${review === 1 ? "s" : ""} your review`, failed && `${failed} failed`]
              .filter(Boolean).join(" · ")}
        </p>
        <p className="mt-0.5 text-xs opacity-90">{job.title}</p>
      </div>
    </div>
  );
}

/** Sidebar: uploads in progress (and just finished), each reopening its queue. */
export function UploadActivity() {
  const dispatch = useAppDispatch();
  const jobs = useAppSelector((s) => s.uploads.jobs);
  if (!jobs.length) return null;
  return (
    <div className="mb-3 space-y-2">
      <p className="px-2 text-[11px] font-semibold uppercase tracking-wider text-muted">Uploads</p>
      {jobs.slice(0, 3).map((job) => {
        const { done, total, fraction } = jobCounts(job);
        const finished = job.phase === "finished";
        return (
          <div key={job.id} className="rounded-md bg-subtle px-2 py-2">
            <div className="flex items-center gap-2">
              <button type="button" onClick={() => dispatch(jobOpened(job.id))} className="min-w-0 flex-1 text-left">
                <p className="truncate text-xs font-medium text-ink">{job.title}</p>
                <p className="text-[11px] text-muted">
                  {finished ? `Done · ${done}/${total}` : job.phase === "processing" ? `Processing · ${done}/${total}` : `Uploading · ${Math.round(fraction * 100)}%`}
                </p>
              </button>
              {finished && (
                <button type="button" onClick={() => dispatch(jobDismissed(job.id))} className="rounded p-0.5 text-muted hover:bg-line-strong/40" aria-label="Dismiss">
                  <X className="size-3.5" />
                </button>
              )}
            </div>
            {!finished && (
              <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-line-strong/30">
                <div className="h-full rounded-full bg-brand-500 transition-all" style={{ width: `${Math.round((job.phase === "processing" ? done / (total || 1) : fraction) * 100)}%` }} />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
