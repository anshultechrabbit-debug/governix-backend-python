import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle, ArrowDown, ArrowUp, CheckCircle2, Copy, FileText, FolderPlus, GripVertical, Layers, Loader2, Trash2,
  UploadCloud, X, XCircle,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type DragEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";
import { api, ApiError } from "../api/client";
import type { DocumentDetail, ExistingPolicyMatch, Page, Policy, PolicySuggestion, UploadBatch } from "../api/types";
import { CategoryPicker } from "../components/CategoryPicker";
import { BelongsCard, ExistingPolicyCard, misplaced, policyFit, WhyElsewhere, type Suggestion } from "../components/PolicyFit";
import { useBranches } from "../components/domain";
import { Badge, Button, Card, Field, Input, Modal, PageHeader, Select, SuggestedBadge, Textarea } from "../components/ui";
import { cn, formatBytes, formatDate } from "../lib/format";
import { takeHandedOffFiles } from "../lib/handoff";
import { readPolicy } from "../lib/uploadManager";
import { groupByFamily } from "../lib/versioning";
import { useAuth, useToast } from "../store/hooks";

// A new policy's name is null until the person types one: the name read from the document is used.
type Target = { kind: "new"; name: string | null; categoryId: string } | { kind: "existing"; policyId: string };
// The version label and effective date are not asked for: the server reads both
// from the document, falling back to the upload order you arrange here.
interface PlannedFile { key: string; file: File }
interface PlannedGroup { key: string; target: Target; items: PlannedFile[] }
type SendState = "queued" | "sending" | "sent" | "failed";
type Mode = "arrange" | "review";

const DRAG_TYPE = "application/x-governix-file";
const PARALLEL_UPLOADS = 3;
let sequence = 0;
const nextKey = () => `k${(sequence += 1)}`;

interface DuplicateInfo { document_id?: string; title?: string | null; original_filename?: string; uploaded_at?: string }
interface ReviewResult { key: string; file: File; documentId?: string; error?: string; duplicate?: DuplicateInfo }

export function UploadPage() {
  const { me, can } = useAuth();
  const toast = useToast();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const presetCategory = params.get("category") ?? "";
  const presetPolicy = params.get("policy") ?? "";
  const canArrange = can("policies:manage");

  const [mode, setMode] = useState<Mode>(canArrange ? "arrange" : "review");
  const [groups, setGroups] = useState<PlannedGroup[]>([]);
  const [defaultCategory, setDefaultCategory] = useState(presetCategory);
  const [branchId, setBranchId] = useState("");
  const [dragKey, setDragKey] = useState<string | null>(null);
  const [overTarget, setOverTarget] = useState<string | null>(null);
  const [sending, setSending] = useState<Record<string, SendState>>({});
  const [busy, setBusy] = useState(false);
  const [results, setResults] = useState<ReviewResult[]>([]);
  const [duplicate, setDuplicate] = useState<ReviewResult | null>(null);
  const [reason, setReason] = useState("");
  const [suggestions, setSuggestions] = useState<Record<string, Suggestion>>({});
  // Popups already answered: "<group>:<policy id>" or "<file>:identical".
  const [answered, setAnswered] = useState<Set<string>>(new Set());
  const inputRef = useRef<HTMLInputElement>(null);
  const readQueue = useRef<PlannedFile[]>([]);
  const readers = useRef(0);
  const requested = useRef(new Set<string>());
  const arranged = useRef(new Set<string>());

  const branches = useBranches();
  const canChooseScope = me?.role === "org_admin" || me?.role === "branch_manager";
  const policies = useQuery({
    queryKey: ["policies", "picker"],
    queryFn: () => api.get<Page<Policy>>("/policies?status=active&limit=200"),
    enabled: canArrange,
  });

  const files = useMemo(() => groups.flatMap((g) => g.items), [groups]);
  const isPreset = (group: PlannedGroup) => group.target.kind === "existing" && group.target.policyId === presetPolicy;
  const newGroup = (items: PlannedFile[], target?: Target): PlannedGroup => ({
    key: nextKey(),
    target: target ?? (presetPolicy ? { kind: "existing", policyId: presetPolicy } : { kind: "new", name: null, categoryId: defaultCategory }),
    items,
  });

  /* ---------- adding, moving, removing ---------- */

  function addFiles(list: File[], intoGroup?: string, beforeKey?: string) {
    const pdfs = list.filter((file) => file.name.toLowerCase().endsWith(".pdf"));
    if (pdfs.length < list.length) toast("bad", `${list.length - pdfs.length} file(s) skipped: only PDF documents are supported.`);
    if (!pdfs.length) return;
    const planned = pdfs.map((file) => ({ key: nextKey(), file }));
    setResults([]);
    setGroups((current) => {
      if (intoGroup) {
        return current.map((group) => group.key === intoGroup ? { ...group, items: insertBefore(group.items, planned, beforeKey) } : group);
      }
      if (presetPolicy || mode === "review") {
        // One destination: all versions of the chosen policy, or a plain list to review.
        const home = current.findIndex((group) => !presetPolicy || isPreset(group));
        return home < 0 ? [...current, newGroup(planned)]
          : current.map((group, index) => index === home ? { ...group, items: [...group.items, ...planned] } : group);
      }
      return [...current, ...groupByFamily(planned).map((family) => newGroup(family))];
    });
  }

  function moveItem(itemKey: string, toGroup: string | "new", beforeKey?: string, target?: Target) {
    setGroups((current) => {
      let moved: PlannedFile | undefined;
      const without = current.map((group) => {
        const found = group.items.find((item) => item.key === itemKey);
        if (!found) return group;
        moved = found;
        return { ...group, items: group.items.filter((item) => item.key !== itemKey) };
      });
      if (!moved) return current;
      const placed = toGroup === "new"
        ? [...without, newGroup([moved], target ?? { kind: "new", name: null, categoryId: defaultCategory })]
        : without.map((group) => group.key === toGroup ? { ...group, items: insertBefore(group.items, [moved!], beforeKey) } : group);
      return placed.filter((group) => group.items.length > 0);
    });
  }

  function shift(groupKey: string, itemKey: string, delta: -1 | 1) {
    setGroups((current) => current.map((group) => {
      if (group.key !== groupKey) return group;
      const index = group.items.findIndex((item) => item.key === itemKey);
      const target = index + delta;
      if (index < 0 || target < 0 || target >= group.items.length) return group;
      const items = [...group.items];
      [items[index], items[target]] = [items[target], items[index]];
      return { ...group, items };
    }));
  }

  const updateTarget = (groupKey: string, target: Target) =>
    setGroups((current) => current.map((group) => group.key === groupKey ? { ...group, target } : group));
  const removeItem = (itemKey: string) =>
    setGroups((current) => current.map((group) => ({ ...group, items: group.items.filter((item) => item.key !== itemKey) }))
      .filter((group) => group.items.length > 0));
  const removeGroup = (groupKey: string) => setGroups((current) => current.filter((group) => group.key !== groupKey));
  // A file that belongs to another existing policy joins that policy's group, or starts one.
  function moveToPolicy(itemKey: string, policyId: string) {
    const home = groups.find((group) => group.target.kind === "existing" && group.target.policyId === policyId);
    if (home) moveItem(itemKey, home.key);
    else moveItem(itemKey, "new", undefined, { kind: "existing", policyId });
  }

  // Read each arranged file's opening pages once: the policy name it states and
  // the existing policy it most likely belongs to.
  useEffect(() => {
    arranged.current = new Set(files.map((item) => item.key));
    if (mode !== "arrange") return;
    const unread = files.filter((item) => !requested.current.has(item.key));
    if (!unread.length) return;
    unread.forEach((item) => requested.current.add(item.key));
    setSuggestions((current) => ({ ...current, ...Object.fromEntries(unread.map((item) => [item.key, "reading"])) }));
    readQueue.current.push(...unread);
    const reader = async () => {
      readers.current += 1;
      for (let item = readQueue.current.shift(); item; item = readQueue.current.shift()) {
        if (!arranged.current.has(item.key)) continue; // removed before it was read
        const key = item.key;
        const read = await readPolicy(item.file, presetPolicy || undefined);
        setSuggestions((current) => ({ ...current, [key]: read }));
      }
      readers.current -= 1;
    };
    for (let i = readers.current; i < PARALLEL_UPLOADS; i += 1) void reader();
  }, [files, mode, presetPolicy]);

  const readGroups = useMemo(
    () => Object.fromEntries(groups.map((group) => [group.key, readGroup(group, suggestions)])),
    [groups, suggestions],
  );
  const answer = (key: string) => setAnswered((current) => new Set(current).add(key));

  // The first thing to ask about that has not been answered yet: a file already
  // uploaded, a file that does not fit the policy it is added to, a policy that exists.
  const prompt = useMemo(() => {
    if (mode !== "arrange" || busy) return null;
    for (const group of groups) {
      for (const item of group.items) {
        const read = suggestions[item.key];
        const identical = read && read !== "reading" ? read.identical_document : null;
        if (identical && !answered.has(`${item.key}:identical`)) return { kind: "identical" as const, group, item, identical };
      }
      const policyId = group.target.kind === "existing" ? group.target.policyId : "";
      for (const item of policyId ? group.items : []) {
        const read = suggestions[item.key];
        if (misplaced(policyFit(read, policyId)) && !answered.has(`${item.key}:${policyId}`)) {
          return { kind: "misplaced" as const, group, item, read: read as PolicySuggestion, policyId };
        }
      }
      const existing = readGroups[group.key]?.existing;
      if (group.target.kind === "new" && existing && !answered.has(`${group.key}:${existing.policy_id}`)) {
        return { kind: "existing" as const, group, existing };
      }
    }
    return null;
  }, [answered, busy, groups, mode, readGroups, suggestions]);
  function policyName(policyId: string) {
    const listed = policies.data?.items.find((p) => p.id === policyId)?.name;
    if (listed) return listed;
    for (const read of Object.values(suggestions)) {
      if (read && read !== "reading" && read.existing_policy?.policy_id === policyId) return read.existing_policy.name;
    }
    return "the chosen policy";
  }

  // Files dropped on another page (e.g. a policy's version drop zone).
  useEffect(() => {
    const handed = takeHandedOffFiles();
    if (handed.length) addFiles(handed);
  }, []);

  /* ---------- drag and drop ---------- */

  const carriesFiles = (event: DragEvent) => Array.from(event.dataTransfer.types).includes("Files");
  function allow(event: DragEvent, target: string) {
    if (!dragKey && !carriesFiles(event)) return;
    event.preventDefault();
    event.stopPropagation();
    event.dataTransfer.dropEffect = dragKey ? "move" : "copy";
    if (overTarget !== target) setOverTarget(target);
  }
  function drop(event: DragEvent, group?: string | "new", beforeKey?: string) {
    event.preventDefault();
    event.stopPropagation();
    setOverTarget(null);
    const itemKey = event.dataTransfer.getData(DRAG_TYPE) || dragKey;
    setDragKey(null);
    if (itemKey) {
      if (group) moveItem(itemKey, group, beforeKey);
      return;
    }
    const dropped = Array.from(event.dataTransfer.files);
    if (group === "new") {
      const planned = dropped.filter((f) => f.name.toLowerCase().endsWith(".pdf"));
      if (planned.length) setGroups((current) => [...current, newGroup(planned.map((file) => ({ key: nextKey(), file })), { kind: "new", name: null, categoryId: defaultCategory })]);
    } else {
      addFiles(dropped, group, beforeKey);
    }
  }

  /* ---------- sending ---------- */

  const problems = useMemo(() => {
    if (mode !== "arrange") return [];
    return groups.flatMap((group, index) =>
      group.target.kind === "existing" && !group.target.policyId ? [`Choose the existing policy for policy ${index + 1}.`] : []);
  }, [groups, mode]);

  async function startArranged() {
    if (problems.length) return toast("bad", problems[0]);
    // Files not read yet are named by the pipeline instead; leave the bandwidth to the upload.
    const unread = readQueue.current.splice(0);
    if (unread.length) setSuggestions((current) => ({ ...current, ...Object.fromEntries(unread.map((item) => [item.key, null])) }));
    setBusy(true);
    try {
      const batch = await api.post<UploadBatch>("/uploads/batches", {
        branch_id: branchId || undefined,
        groups: groups.map((group) => ({
          ...(group.target.kind === "existing"
            ? { policy_id: group.target.policyId }
            : {
                new_policy_name: group.target.name?.trim() || readGroups[group.key].name || undefined,
                category_id: group.target.categoryId || readGroups[group.key].categoryId || undefined,
              }),
          items: group.items.map((item) => ({ filename: item.file.name })),
        })),
      });
      const jobs = groups.flatMap((group, g) => group.items.map((item, i) => ({ item, id: batch.groups[g].items[i].id })));
      setSending(Object.fromEntries(jobs.map((job) => [job.item.key, "queued" as SendState])));
      let next = 0;
      const worker = async () => {
        while (next < jobs.length) {
          const job = jobs[next];
          next += 1;
          setSending((state) => ({ ...state, [job.item.key]: "sending" }));
          const form = new FormData();
          form.append("file", job.item.file);
          try {
            const sent = await api.upload<{ status: string }>(`/uploads/batches/${batch.id}/items/${job.id}/file`, form);
            setSending((state) => ({ ...state, [job.item.key]: sent.status === "failed" ? "failed" : "sent" }));
          } catch {
            setSending((state) => ({ ...state, [job.item.key]: "failed" }));
          }
        }
      };
      await Promise.all(Array.from({ length: Math.min(PARALLEL_UPLOADS, jobs.length) }, worker));
      await api.post(`/uploads/batches/${batch.id}/start`);
      navigate(`/uploads/${batch.id}`);
    } catch (err) {
      toast("bad", err instanceof ApiError ? err.message : "The upload could not be started.");
      setSending({});
    } finally {
      setBusy(false);
    }
  }

  async function uploadForReview(item: PlannedFile, allowDuplicate = false): Promise<ReviewResult> {
    const form = new FormData();
    form.append("file", item.file);
    if (branchId) form.append("branch_id", branchId);
    if (defaultCategory) form.append("category_id", defaultCategory);
    if (allowDuplicate) {
      form.append("allow_duplicate", "true");
      form.append("duplicate_reason", reason);
    }
    try {
      const document = await api.upload<DocumentDetail>("/documents", form);
      return { key: item.key, file: item.file, documentId: document.id };
    } catch (err) {
      if (err instanceof ApiError && err.code === "DUPLICATE_DOCUMENT") {
        return { key: item.key, file: item.file, error: err.message, duplicate: (err.details as { existing?: DuplicateInfo })?.existing ?? {} };
      }
      return { key: item.key, file: item.file, error: err instanceof ApiError ? err.message : "Upload failed." };
    }
  }

  async function startReview() {
    setBusy(true);
    const done: ReviewResult[] = [];
    for (const item of files) {
      setSending((state) => ({ ...state, [item.key]: "sending" }));
      const result = await uploadForReview(item);
      done.push(result);
      setSending((state) => ({ ...state, [item.key]: result.documentId ? "sent" : "failed" }));
    }
    setBusy(false);
    if (done.length === 1 && done[0].documentId) return navigate(`/documents/${done[0].documentId}`);
    setResults(done);
    setGroups([]);
    setSending({});
  }

  async function uploadDuplicateAnyway() {
    if (!duplicate) return;
    setBusy(true);
    const result = await uploadForReview({ key: duplicate.key, file: duplicate.file }, true);
    setBusy(false);
    setDuplicate(null);
    setReason("");
    setResults((current) => current.map((r) => r.key === result.key ? result : r));
    if (result.error) toast("bad", result.error);
  }

  /* ---------- rendering ---------- */

  const presetPolicyName = policies.data?.items.find((p) => p.id === presetPolicy)?.name;
  const sendingNow = Object.keys(sending).length > 0;
  const sentCount = Object.values(sending).filter((state) => state === "sent" || state === "failed").length;
  const readingCount = mode === "arrange" ? files.filter((item) => suggestions[item.key] === "reading").length : 0;

  return (
    <>
      <PageHeader
        title={presetPolicyName ? `Upload new versions of ${presetPolicyName}` : "Upload documents"}
        subtitle="Drop one or many PDFs. Arrange them into policies and put each policy's versions in order, oldest first."
        actions={canArrange && <Link to="/uploads"><Button variant="secondary">Recent uploads</Button></Link>}
      />
      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_20rem]">
        <div className="min-w-0 space-y-4">
          <div
            onDragOver={(e) => allow(e, "root")}
            onDragLeave={() => setOverTarget(null)}
            onDrop={(e) => drop(e)}
            onClick={() => inputRef.current?.click()}
            className={cn(
              "flex cursor-pointer flex-col items-center justify-center rounded-lg border-2 border-dashed bg-surface px-6 py-10 text-center transition-colors",
              overTarget === "root" ? "border-brand-500 bg-brand-50" : "border-line-strong hover:border-brand-500 hover:bg-subtle",
            )}
          >
            <UploadCloud className="mb-3 size-10 text-brand-500" />
            <p className="font-medium">Drag &amp; drop PDFs here</p>
            <p className="mt-1 text-sm text-muted">or click to choose files · several at once for a bulk upload</p>
            <input ref={inputRef} type="file" accept="application/pdf,.pdf" multiple className="hidden"
              onChange={(e) => { addFiles(Array.from(e.target.files ?? [])); e.target.value = ""; }} />
          </div>

          {mode === "arrange" && groups.map((group, index) => (
            <GroupCard
              key={group.key}
              group={group}
              index={index}
              read={readGroups[group.key]}
              suggestions={suggestions}
              policies={policies.data?.items ?? []}
              lockedPolicy={Boolean(presetPolicy) && isPreset(group)}
              overTarget={overTarget}
              sending={sending}
              disabled={busy}
              onTarget={(target) => updateTarget(group.key, target)}
              onAddToExisting={(policyId) => updateTarget(group.key, { kind: "existing", policyId })}
              onRemove={() => removeGroup(group.key)}
              onRemoveItem={removeItem}
              onShift={(itemKey, delta) => shift(group.key, itemKey, delta)}
              onDragStart={(itemKey) => setDragKey(itemKey)}
              onDragEnd={() => { setDragKey(null); setOverTarget(null); }}
              allow={allow}
              drop={drop}
            />
          ))}

          {mode === "arrange" && groups.length > 0 && !presetPolicy && (
            <div
              onDragOver={(e) => allow(e, "new")}
              onDragLeave={() => setOverTarget(null)}
              onDrop={(e) => drop(e, "new")}
              className={cn(
                "flex items-center justify-center gap-2 rounded-lg border-2 border-dashed px-4 py-5 text-sm text-muted transition-colors",
                overTarget === "new" ? "border-brand-500 bg-brand-50 text-brand-700" : "border-line",
              )}
            >
              <FolderPlus className="size-4" />Drag a file here to make it a separate policy
            </div>
          )}

          {mode === "review" && files.length > 0 && (
            <Card>
              <ul className="divide-y divide-line">
                {files.map((item) => (
                  <li key={item.key} className="flex items-center gap-3 px-4 py-3 text-sm">
                    <FileText className="size-4 text-brand-500" />
                    <span className="min-w-0 flex-1 truncate">{item.file.name}</span>
                    <span className="text-xs text-muted">{formatBytes(item.file.size)}</span>
                    <SendBadge state={sending[item.key]} />
                    {!busy && <button className="rounded p-1 text-muted hover:bg-subtle" aria-label={`Remove ${item.file.name}`} onClick={() => removeItem(item.key)}><X className="size-4" /></button>}
                  </li>
                ))}
              </ul>
            </Card>
          )}

          {results.length > 0 && (
            <Card>
              <div className="border-b border-line px-4 py-3 text-sm font-medium">Uploaded for review</div>
              <ul className="divide-y divide-line">
                {results.map((result) => (
                  <li key={result.key} className="flex flex-wrap items-center gap-3 px-4 py-3 text-sm">
                    {result.documentId ? <CheckCircle2 className="size-4 text-ok-600" /> : <XCircle className="size-4 text-bad-600" />}
                    <span className="min-w-0 flex-1 truncate">{result.file.name}</span>
                    {result.documentId && <Link to={`/documents/${result.documentId}`}><Button size="sm" variant="secondary">Review</Button></Link>}
                    {result.error && <span className="text-xs text-bad-600">{result.error}</span>}
                    {result.duplicate && <Button size="sm" variant="ghost" onClick={() => setDuplicate(result)}>Upload anyway…</Button>}
                  </li>
                ))}
              </ul>
            </Card>
          )}
        </div>

        <aside className="space-y-4">
          <Card className="space-y-4 p-4">
            {canArrange && (
              <fieldset className="space-y-2">
                <legend className="mb-1 text-sm font-medium">How to register</legend>
                <ModeOption checked={mode === "arrange"} onChange={() => setMode("arrange")} title="Arrange policies and versions"
                  description="Registered automatically in the order you arrange. Anything Governix flags waits for review." />
                <ModeOption checked={mode === "review"} onChange={() => { setMode("review"); }} title="Review each document myself"
                  description="Governix suggests the policy and version for each file; you confirm them one by one." />
              </fieldset>
            )}
            {!presetPolicy && (
              <Field label={mode === "arrange" ? "Category for new policies" : "Category"} hint="Leave empty to use the category Governix detects.">
                <CategoryPicker value={defaultCategory} emptyLabel="Detect from the document"
                  onChange={(id) => {
                    setDefaultCategory(id);
                    setGroups((current) => current.map((group) => group.target.kind === "new" && !group.target.categoryId
                      ? { ...group, target: { ...group.target, categoryId: id } } : group));
                  }} />
              </Field>
            )}
            {canChooseScope && !presetPolicy && (
              <>
                <Field label="Visible to" hint="Every document states its scope. Branch documents never leave their branch.">
                  <Select value={branchId} onChange={(e) => setBranchId(e.target.value)}>
                    <option value="">{me?.role === "org_admin" ? "Whole organization (global)" : "My branch"}</option>
                    {branches.data?.items.map((b) => <option key={b.id} value={b.id}>{b.name} (branch)</option>)}
                  </Select>
                </Field>
              </>
            )}
            <div className="border-t border-line pt-4">
              <p className="text-sm">
                <span className="font-medium">{files.length}</span> file{files.length === 1 ? "" : "s"}
                {mode === "arrange" && <> · <span className="font-medium">{groups.length}</span> polic{groups.length === 1 ? "y" : "ies"}</>}
              </p>
              {readingCount > 0 && !sendingNow && (
                <p className="mt-1 flex items-center gap-1.5 text-xs text-muted">
                  <Loader2 className="size-3.5 animate-spin" />Reading {readingCount} document{readingCount === 1 ? "" : "s"}…
                </p>
              )}
              {sendingNow && (
                <p className="mt-1 flex items-center gap-1.5 text-xs text-muted">
                  <Loader2 className="size-3.5 animate-spin" />Sending {sentCount}/{Object.keys(sending).length}…
                </p>
              )}
              <Button className="mt-3 w-full" disabled={!files.length} loading={busy}
                onClick={() => (mode === "arrange" ? startArranged() : startReview())}>
                {mode === "arrange" ? "Upload and register" : "Upload for review"}
              </Button>
              {mode === "arrange" && <p className="mt-2 text-xs text-muted">Policy names are read from each document as you add it; versions and effective dates too. Your arrangement only sets the order.</p>}
            </div>
          </Card>
        </aside>
      </div>

      <Modal
        open={prompt?.kind === "misplaced"}
        onClose={() => prompt?.kind === "misplaced" && answer(`${prompt.item.key}:${prompt.policyId}`)}
        title="This file may be in the wrong policy"
        footer={prompt?.kind === "misplaced" && <>
          <Button variant="ghost" onClick={() => { answer(`${prompt.item.key}:${prompt.policyId}`); removeItem(prompt.item.key); }}>Remove</Button>
          <Button variant="secondary" onClick={() => answer(`${prompt.item.key}:${prompt.policyId}`)}>Add here anyway</Button>
          {prompt.read.existing_policy ? (
            prompt.read.existing_policy.can_add_version && (
              <Button onClick={() => { answer(`${prompt.item.key}:${prompt.policyId}`); moveToPolicy(prompt.item.key, prompt.read.existing_policy!.policy_id); }}>
                Move to that policy
              </Button>
            )
          ) : (
            <Button onClick={() => {
              answer(`${prompt.item.key}:${prompt.policyId}`);
              moveItem(prompt.item.key, "new", undefined, { kind: "new", name: null, categoryId: defaultCategory });
            }}>Upload as a new policy</Button>
          )}
        </>}
      >
        {prompt?.kind === "misplaced" && (
          <div className="space-y-3 text-sm">
            <p className="text-ink-soft">
              <span className="font-medium text-ink">{prompt.item.file.name}</span>:{" "}
              <WhyElsewhere read={prompt.read} policyName={policyName(prompt.policyId)} />
            </p>
            <BelongsCard read={prompt.read} />
            {prompt.read.existing_policy && !prompt.read.existing_policy.can_add_version && (
              <p className="flex gap-2 rounded-md bg-warn-50 p-2.5 text-xs text-warn-600">
                <AlertTriangle className="size-4 shrink-0" />That policy is outside the scope you manage, so the file cannot be moved there from here.
              </p>
            )}
            <p className="text-xs text-muted">Add it here anyway only if it really is a version of {policyName(prompt.policyId)}.</p>
          </div>
        )}
      </Modal>

      <Modal
        open={prompt?.kind === "existing"}
        onClose={() => prompt?.kind === "existing" && answer(`${prompt.group.key}:${prompt.existing.policy_id}`)}
        title="This policy already exists"
        footer={prompt?.kind === "existing" && <>
          <Button variant="secondary" onClick={() => answer(`${prompt.group.key}:${prompt.existing.policy_id}`)}>Keep as a new policy</Button>
          <Button disabled={!prompt.existing.can_add_version} onClick={() => {
            answer(`${prompt.group.key}:${prompt.existing.policy_id}`);
            updateTarget(prompt.group.key, { kind: "existing", policyId: prompt.existing.policy_id });
          }}>Add as new version</Button>
        </>}
      >
        {prompt?.kind === "existing" && (
          <div className="space-y-4 text-sm">
            <p className="text-ink-soft">
              {prompt.group.items.length === 1
                ? <><span className="font-medium text-ink">{prompt.group.items[0].file.name}</span> looks like a new version of a policy you already have.</>
                : <>These {prompt.group.items.length} files look like new versions of a policy you already have.</>}
            </p>
            <ExistingPolicyCard existing={prompt.existing} />
            {prompt.existing.can_add_version ? (
              <p className="text-xs text-muted">Adding it as a new version keeps the policy's history in one place. Keep it as a new policy only if it is a different policy that happens to look similar.</p>
            ) : (
              <p className="flex gap-2 rounded-md bg-warn-50 p-2.5 text-xs text-warn-600">
                <AlertTriangle className="size-4 shrink-0" />This policy is outside the scope you manage, so you cannot add versions to it. Ask an organization admin, or keep this as a separate policy.
              </p>
            )}
          </div>
        )}
      </Modal>

      <Modal
        open={prompt?.kind === "identical"}
        onClose={() => prompt?.kind === "identical" && answer(`${prompt.item.key}:identical`)}
        title="This file is already uploaded"
        footer={prompt?.kind === "identical" && <>
          <Link to={`/documents/${prompt.identical.document_id}`} target="_blank"><Button variant="secondary">View existing</Button></Link>
          <Button variant="ghost" onClick={() => answer(`${prompt.item.key}:identical`)}>Keep it</Button>
          <Button onClick={() => { answer(`${prompt.item.key}:identical`); removeItem(prompt.item.key); }}>Remove from upload</Button>
        </>}
      >
        {prompt?.kind === "identical" && (
          <div className="space-y-3 text-sm">
            <p className="flex gap-2.5 text-ink-soft">
              <Copy className="mt-0.5 size-4 shrink-0 text-brand-600" />
              <span>
                <span className="font-medium text-ink">{prompt.item.file.name}</span> is exactly the same file as{" "}
                {prompt.identical.policy_name
                  ? <>a version of <span className="font-medium text-ink">{prompt.identical.policy_name}</span></>
                  : <span className="font-medium text-ink">{prompt.identical.title ?? prompt.identical.original_filename}</span>}.
              </span>
            </p>
            <p className="text-xs text-muted">A bulk upload does not register the same file twice, so it would be skipped.</p>
          </div>
        )}
      </Modal>

      <Modal
        open={Boolean(duplicate)}
        onClose={() => setDuplicate(null)}
        title="Duplicate document detected"
        footer={<>
          <Button variant="secondary" onClick={() => setDuplicate(null)}>Cancel</Button>
          {duplicate?.duplicate?.document_id && <Button variant="secondary" onClick={() => navigate(`/documents/${duplicate.duplicate!.document_id}`)}>View existing</Button>}
          <Button disabled={reason.trim().length < 5} loading={busy} onClick={uploadDuplicateAnyway}>Upload anyway</Button>
        </>}
      >
        <p className="text-sm text-ink-soft">{duplicate?.error}</p>
        {duplicate?.duplicate?.uploaded_at && (
          <p className="mt-2 text-xs text-muted">Existing: {duplicate.duplicate.title ?? duplicate.duplicate.original_filename} · uploaded {formatDate(duplicate.duplicate.uploaded_at)}</p>
        )}
        <div className="mt-4">
          <Field label="Reason for uploading anyway (recorded in the audit log)">
            <Textarea rows={2} value={reason} onChange={(e) => setReason(e.target.value)} />
          </Field>
        </div>
      </Modal>
    </>
  );
}

function insertBefore<T extends { key: string }>(list: T[], items: T[], beforeKey?: string) {
  const index = beforeKey ? list.findIndex((item) => item.key === beforeKey) : -1;
  return index < 0 ? [...list, ...items] : [...list.slice(0, index), ...items, ...list.slice(index)];
}

interface GroupRead {
  name: string | null;
  categoryId: string | null;
  categoryName: string | null;
  existing: ExistingPolicyMatch | null;
  reading: boolean;
}

/** What the group's files say about the policy; the newest file speaks first, as when it is registered. */
function readGroup(group: PlannedGroup, suggestions: Record<string, Suggestion>): GroupRead {
  const read = [...group.items].reverse().map((item) => suggestions[item.key])
    .filter((s): s is PolicySuggestion => Boolean(s) && s !== "reading");
  const named = read.find((s) => s.name);
  const categorized = read.find((s) => s.category_id);
  return {
    name: named?.name ?? null,
    categoryId: categorized?.category_id ?? null,
    categoryName: categorized?.category_name ?? null,
    existing: read.find((s) => s.existing_policy)?.existing_policy ?? null,
    reading: group.items.some((item) => suggestions[item.key] === "reading"),
  };
}

/** "Bank's Policy on Record Retention" and "BANKS POLICY ON RECORD RETENTION" are the same name. */
const sameName = (a: string, b: string) => {
  const norm = (value: string) => value.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  return norm(a) === norm(b);
};

function groupLabel(group: PlannedGroup, index: number, policies: Policy[], read?: GroupRead) {
  if (group.target.kind === "existing") {
    const policyId = group.target.policyId;
    return policies.find((p) => p.id === policyId)?.name ?? `Policy ${index + 1}`;
  }
  return group.target.name?.trim() || read?.name || `Policy ${index + 1} (new)`;
}

/** One line on what was read from a file before upload. */
function ReadLine({ read }: { read: Suggestion | undefined }) {
  if (read === "reading") return <><Loader2 className="size-3 animate-spin" />Reading the document…</>;
  if (!read) return <>Version and effective date are read from the document after upload.</>;
  if (!read.readable) return <>Scanned or unreadable before upload: name, version and date are read once it is processed.</>;
  const parts = [
    read.version_label ? `Version ${read.version_label}` : "No version stated",
    read.effective_date ? `effective ${formatDate(read.effective_date)}` : "no effective date stated",
  ];
  return <>{parts.join(" · ")} · read from the document</>;
}

function ModeOption({ checked, onChange, title, description }: { checked: boolean; onChange: () => void; title: string; description: string }) {
  return (
    <label className={cn("flex cursor-pointer gap-2.5 rounded-md border p-2.5", checked ? "border-brand-500 bg-brand-50" : "border-line hover:bg-subtle")}>
      <input type="radio" className="mt-1" checked={checked} onChange={onChange} />
      <span><span className="block text-sm font-medium">{title}</span><span className="block text-xs text-muted">{description}</span></span>
    </label>
  );
}

function SendBadge({ state }: { state?: SendState }) {
  if (!state) return null;
  if (state === "sending") return <Badge tone="info"><Loader2 className="size-3 animate-spin" />Sending</Badge>;
  if (state === "sent") return <Badge tone="ok">Sent</Badge>;
  if (state === "failed") return <Badge tone="bad">Not sent</Badge>;
  return <Badge>Queued</Badge>;
}

function GroupCard({
  group, index, read, suggestions, policies, lockedPolicy, overTarget, sending, disabled,
  onTarget, onAddToExisting, onRemove, onRemoveItem, onShift, onDragStart, onDragEnd, allow, drop,
}: {
  group: PlannedGroup; index: number; read: GroupRead; suggestions: Record<string, Suggestion>;
  policies: Policy[]; lockedPolicy: boolean; overTarget: string | null;
  sending: Record<string, SendState>; disabled: boolean;
  onTarget: (target: Target) => void; onAddToExisting: (policyId: string) => void; onRemove: () => void;
  onRemoveItem: (key: string) => void; onShift: (key: string, delta: -1 | 1) => void;
  onDragStart: (itemKey: string) => void; onDragEnd: () => void;
  allow: (event: DragEvent, target: string) => void; drop: (event: DragEvent, group?: string, beforeKey?: string) => void;
}) {
  const target = group.target;
  const endZone = `${group.key}:end`;
  // A typed name that is already taken, else the policy the content matched.
  const typed = target.kind === "new" && target.name?.trim() ? policies.find((p) => sameName(p.name, target.name!)) : undefined;
  const clash = typed
    ? { id: typed.id, name: typed.name, version: typed.current_version?.version_label ?? null, canAdd: true }
    : read.existing && { id: read.existing.policy_id, name: read.existing.name, version: read.existing.current_version_label, canAdd: read.existing.can_add_version };
  const matchedMissing = target.kind === "existing" && read.existing && !policies.some((p) => p.id === read.existing!.policy_id);
  return (
    <Card className="overflow-hidden">
      <div className="flex flex-wrap items-start gap-3 border-b border-line bg-subtle/60 px-4 py-3">
        <Layers className="mt-2 size-4 text-brand-600" />
        <div className="min-w-0 flex-1 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-semibold">Policy {index + 1}</span>
            {!lockedPolicy && (
              <div className="inline-flex rounded-md border border-line bg-surface p-0.5 text-xs" role="radiogroup" aria-label="Policy destination">
                {(["new", "existing"] as const).map((kind) => (
                  <button key={kind} type="button" role="radio" aria-checked={target.kind === kind} disabled={disabled}
                    onClick={() => onTarget(kind === "new" ? { kind: "new", name: null, categoryId: "" } : { kind: "existing", policyId: "" })}
                    className={cn("rounded px-2.5 py-1 font-medium", target.kind === kind ? "bg-brand-600 text-white" : "text-ink-soft hover:bg-subtle")}>
                    {kind === "new" ? "New policy" : "Existing policy"}
                  </button>
                ))}
              </div>
            )}
          </div>
          {target.kind === "new" ? (
            <>
              <div className="grid gap-2 sm:grid-cols-2">
                <div className="relative">
                  <Input value={target.name ?? read.name ?? ""} disabled={disabled} aria-label="Policy name"
                    className={cn(target.name === null && read.name && "pr-28")}
                    placeholder={read.reading ? "Reading the name from the document…" : "Policy name (read from the document after upload)"}
                    onChange={(e) => onTarget({ ...target, name: e.target.value })}
                    // Emptied: go back to the name read from the document.
                    onBlur={(e) => !e.target.value.trim() && onTarget({ ...target, name: null })} />
                  {read.reading && target.name === null && !read.name && (
                    <Loader2 className="absolute right-2.5 top-1/2 size-4 -translate-y-1/2 animate-spin text-muted" aria-hidden />
                  )}
                  {target.name === null && read.name && (
                    <span className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2"><SuggestedBadge label="From document" /></span>
                  )}
                </div>
                <CategoryPicker value={target.categoryId}
                  emptyLabel={read.categoryName ? `Category: ${read.categoryName} (detected)` : "Category: detect from the document"}
                  onChange={(categoryId) => onTarget({ ...target, categoryId })} />
              </div>
              {clash && (
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md bg-warn-50 px-3 py-2 text-xs text-warn-600">
                  <AlertTriangle className="size-4 shrink-0" />
                  <span className="min-w-0 flex-1">
                    <span className="font-medium">{clash.name}</span> already exists{clash.version ? ` (current v${clash.version})` : ""}.
                    {typed ? " Pick another name, or add these files to it." : " These files look like new versions of it."}
                  </span>
                  {clash.canAdd && !disabled && (
                    <button type="button" className="font-medium underline underline-offset-2 hover:opacity-80" onClick={() => onAddToExisting(clash.id)}>
                      Add as new version instead
                    </button>
                  )}
                </div>
              )}
            </>
          ) : (
            <Select value={target.policyId} disabled={disabled || lockedPolicy} onChange={(e) => onTarget({ kind: "existing", policyId: e.target.value })} aria-label="Existing policy">
              <option value="">Choose the policy these are versions of…</option>
              {matchedMissing && <option value={read.existing!.policy_id}>{read.existing!.name}</option>}
              {policies.map((policy) => (
                <option key={policy.id} value={policy.id}>
                  {policy.name}{policy.current_version ? ` · current v${policy.current_version.version_label}` : ""}
                </option>
              ))}
            </Select>
          )}
        </div>
        {!disabled && <button type="button" onClick={onRemove} className="rounded p-1.5 text-muted hover:bg-surface" aria-label={`Remove policy ${index + 1}`}><Trash2 className="size-4" /></button>}
      </div>

      <ol className="divide-y divide-line" aria-label={`Versions of ${groupLabel(group, index, policies, read)}, oldest first`}>
        {group.items.map((item, position) => {
          const zone = `${group.key}:${item.key}`;
          const last = position === group.items.length - 1;
          const itemRead = suggestions[item.key];
          const identical = itemRead && itemRead !== "reading" && itemRead.identical_document;
          const elsewhere = target.kind === "existing" && target.policyId && misplaced(policyFit(itemRead, target.policyId));
          return (
            <li
              key={item.key}
              draggable={!disabled}
              onDragStart={(e) => { e.dataTransfer.setData(DRAG_TYPE, item.key); e.dataTransfer.effectAllowed = "move"; onDragStart(item.key); }}
              onDragEnd={onDragEnd}
              onDragOver={(e) => allow(e, zone)}
              onDrop={(e) => drop(e, group.key, item.key)}
              className={cn("px-3 py-2.5 transition-colors", overTarget === zone && "border-t-2 border-t-brand-500 bg-brand-50/60")}
            >
              <div className="flex flex-wrap items-center gap-2">
                <GripVertical className={cn("size-4 shrink-0 text-muted", !disabled && "cursor-grab")} aria-hidden />
                <Badge tone={last ? "brand" : "neutral"} className="w-16 justify-center">{group.items.length === 1 ? "Version" : last ? "Latest" : position === 0 ? "Oldest" : `#${position + 1}`}</Badge>
                <FileText className="size-4 shrink-0 text-brand-500" />
                <span className="min-w-0 flex-1 truncate text-sm" title={item.file.name}>{item.file.name}</span>
                {identical && <Badge tone="warn">Already uploaded</Badge>}
                {elsewhere && <Badge tone="warn">Looks like another policy</Badge>}
                <span className="text-xs text-muted">{formatBytes(item.file.size)}</span>
                <SendBadge state={sending[item.key]} />
                {!disabled && (
                  <div className="flex items-center gap-0.5">
                    <button type="button" className="rounded p-1 text-muted hover:bg-subtle disabled:opacity-30" disabled={position === 0} onClick={() => onShift(item.key, -1)} aria-label="Move earlier"><ArrowUp className="size-4" /></button>
                    <button type="button" className="rounded p-1 text-muted hover:bg-subtle disabled:opacity-30" disabled={last} onClick={() => onShift(item.key, 1)} aria-label="Move later"><ArrowDown className="size-4" /></button>
                    <button type="button" className="rounded p-1 text-muted hover:bg-subtle" onClick={() => onRemoveItem(item.key)} aria-label={`Remove ${item.file.name}`}><X className="size-4" /></button>
                  </div>
                )}
              </div>
              <p className="mt-1 flex items-center gap-1.5 pl-6 text-xs text-muted" title="Drag the file onto another policy to move it.">
                <ReadLine read={itemRead} />
              </p>
            </li>
          );
        })}
      </ol>
      <div
        onDragOver={(e) => allow(e, endZone)}
        onDrop={(e) => drop(e, group.key)}
        className={cn("px-4 py-2.5 text-center text-xs text-muted transition-colors", overTarget === endZone ? "bg-brand-50 text-brand-700" : "bg-subtle/40")}
      >
        Drop files or drag versions here to add them as the latest version
      </div>
    </Card>
  );
}
