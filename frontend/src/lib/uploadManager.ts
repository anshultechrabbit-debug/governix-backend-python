/**
 * Bulk uploads that keep going when their dialog is closed or the page changes.
 *
 *   start    plan the batch (one policy per family of file names, versions oldest
 *            first), then send files three at a time with byte progress
 *   cancel   a queued file is skipped; a file on its way is aborted; either way
 *            the server is told, so the rest of its policy is not held up
 *   follow   after sending, poll the batch until every file is registered, needs
 *            review or failed (the server does the heavy work in the background)
 *
 * Progress lives in the uploads slice; File objects and live requests stay here.
 */
import { api, ApiError, type UploadHandle } from "../api/client";
import type { PolicySuggestion, Progress, UploadBatch, UploadBatchItem } from "../api/types";
import { store } from "../store";
import { itemUpdated, jobAdded, jobUpdated, type QueueItem, type QueueStage, type QueueState } from "../store/uploadsSlice";
import { queryClient } from "./queryClient";
import { groupByFamily } from "./versioning";

const PARALLEL_UPLOADS = 3;
const POLL_MS = 2000;
const HASH_LIMIT_BYTES = 256 * 1024 * 1024;
// Larger files are only read once they are uploaded for real (the server's limit for a read).
const READ_LIMIT_BYTES = 100 * 1024 * 1024;

export interface DraftFile {
  key: string;
  file: File;
  /** A duplicate the person chose to upload anyway (needs a reason). */
  uploadAnyway: boolean;
}

export interface StartOptions {
  title: string;
  link: string;
  files: DraftFile[];
  categoryId?: string;
  /** Add every file as a new version of this policy instead. */
  policyId?: string;
  /** Or create one new policy of this name from every file. */
  newPolicyName?: string;
  duplicateReason?: string;
}

const live = new Map<string, UploadHandle<unknown>>();
const cancelRequested = new Set<string>();
const slot = (jobId: string, key: string) => `${jobId}:${key}`;

/**
 * What a file's opening pages say, before it is uploaded: the policy name it states and
 * the existing policy it matches (preferring `policyId`, the one it is meant for).
 * Null when it cannot be read now; the pipeline reads it after upload.
 */
export function readPolicy(file: File, policyId?: string): Promise<PolicySuggestion | null> {
  if (file.size > READ_LIMIT_BYTES) return Promise.resolve(null);
  const form = new FormData();
  form.append("file", file);
  if (policyId) form.append("policy_id", policyId);
  return api.upload<PolicySuggestion>("/uploads/suggestions", form).catch(() => null);
}

/** SHA-256 of a file, for the duplicate check before upload. Very large files are left to the server. */
export async function sha256(file: File): Promise<string | null> {
  if (file.size > HASH_LIMIT_BYTES || !globalThis.crypto?.subtle) return null;
  const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function update(jobId: string, key: string, patch: Partial<QueueItem>) {
  store.dispatch(itemUpdated({ jobId, key, patch }));
}

function currentItem(jobId: string, key: string) {
  return store.getState().uploads.jobs.find((j) => j.id === jobId)?.items.find((i) => i.key === key);
}

/** Start in the background; returns the job id immediately. */
export function startUpload(options: StartOptions): string {
  const jobId = `job-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
  // Versions of one policy keep the order the person arranged, oldest first.
  const groups = options.policyId || options.newPolicyName ? [options.files] : groupByFamily(options.files);
  const ordered = groups.flat();
  store.dispatch(jobAdded({
    id: jobId, title: options.title, link: options.link, phase: "uploading", batchId: null, startedAt: Date.now(),
    items: ordered.map((d) => ({
      key: d.key, name: d.file.name, size: d.file.size, state: "pending", progress: 0, message: null,
      itemId: null, documentId: null, stages: null,
    })),
  }));
  void run(jobId, groups, options);
  return jobId;
}

async function run(jobId: string, groups: DraftFile[][], options: StartOptions) {
  let batch: UploadBatch;
  try {
    batch = await api.post<UploadBatch>("/uploads/batches", {
      groups: groups.map((group) => ({
        ...(options.policyId
          ? { policy_id: options.policyId }
          : { new_policy_name: options.newPolicyName, category_id: options.categoryId }),
        items: group.map((d) => ({ filename: d.file.name })),
      })),
    });
  } catch (err) {
    const message = err instanceof ApiError ? err.message : "The upload could not be started.";
    groups.flat().forEach((d) => update(jobId, d.key, { state: "failed", message }));
    store.dispatch(jobUpdated({ id: jobId, patch: { phase: "finished" } }));
    return;
  }
  store.dispatch(jobUpdated({ id: jobId, patch: { batchId: batch.id } }));
  const work = groups.flatMap((group, g) => group.map((draft, i) => ({ draft, itemId: batch.groups[g].items[i].id })));
  work.forEach(({ draft, itemId }) => update(jobId, draft.key, { itemId }));

  let next = 0;
  const worker = async () => {
    while (next < work.length) {
      const job = work[next];
      next += 1;
      await send(jobId, batch.id, job.draft, job.itemId, options.duplicateReason);
    }
  };
  await Promise.all(Array.from({ length: Math.min(PARALLEL_UPLOADS, work.length) }, worker));
  // Stop waiting for anything that never arrived and let the server finish.
  await api.post(`/uploads/batches/${batch.id}/start`).catch(() => undefined);
  store.dispatch(jobUpdated({ id: jobId, patch: { phase: "processing" } }));
  await follow(jobId, batch.id, new Map(work.map((w) => [w.itemId, w.draft.key])));
}

async function cancelOnServer(jobId: string, batchId: string, key: string, itemId: string) {
  try {
    const item = await api.post<UploadBatchItem>(`/uploads/batches/${batchId}/items/${itemId}/cancel`);
    update(jobId, key, { state: item.status === "cancelled" ? "cancelled" : "processing", message: item.message, progress: 0 });
  } catch {
    // Already received: it is in the pipeline and will be reported like the others.
    update(jobId, key, { state: "processing", message: "Received before it could be cancelled." });
  }
}

async function send(jobId: string, batchId: string, draft: DraftFile, itemId: string, reason?: string) {
  const id = slot(jobId, draft.key);
  if (cancelRequested.has(id)) return cancelOnServer(jobId, batchId, draft.key, itemId);
  update(jobId, draft.key, { state: "uploading", progress: 0 });
  const form = new FormData();
  form.append("file", draft.file);
  if (draft.uploadAnyway && reason) {
    form.append("allow_duplicate", "true");
    form.append("duplicate_reason", reason);
  }
  let reported = 0;
  const handle = api.uploadWithProgress<UploadBatchItem>(
    `/uploads/batches/${batchId}/items/${itemId}/file`, form,
    (fraction) => {
      if (fraction - reported >= 0.02 || fraction === 1) {
        reported = fraction;
        update(jobId, draft.key, { progress: fraction });
      }
    },
  );
  live.set(id, handle);
  try {
    const item = await handle.promise;
    update(jobId, draft.key, item.status === "failed"
      ? { state: "failed", message: item.message }
      : { state: "processing", progress: 1, documentId: item.document_id });
  } catch (err) {
    if (err instanceof ApiError && err.code === "ABORTED") await cancelOnServer(jobId, batchId, draft.key, itemId);
    else update(jobId, draft.key, { state: "failed", message: err instanceof ApiError ? err.message : "Upload failed." });
  } finally {
    live.delete(id);
  }
}

/** Cancel one file: skipped if queued, aborted if on its way. Received files cannot be recalled. */
export function cancelUpload(jobId: string, key: string) {
  const id = slot(jobId, key);
  cancelRequested.add(id);
  const item = currentItem(jobId, key);
  if (item?.state === "pending") update(jobId, key, { state: "cancelled", message: "Cancelled." });
  live.get(id)?.abort();
}

const SERVER_STATES: Record<string, QueueState> = {
  confirmed: "registered", needs_review: "review", failed: "failed", cancelled: "cancelled", processing: "processing",
};

/** The pipeline's real stage rows for one document, or null if it is not readable yet. */
async function readStages(documentId: string): Promise<QueueStage[] | null> {
  try {
    const progress = await api.get<Progress>(`/documents/${documentId}/progress`);
    return progress.stages.map(({ stage, status, done_units, total_units, estimated_seconds_remaining }) => ({
      stage, status, done_units, total_units, estimated_seconds_remaining,
    }));
  } catch {
    return null;
  }
}

async function follow(jobId: string, batchId: string, keys: Map<string, string>) {
  let errors = 0;
  for (;;) {
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
    let batch: UploadBatch;
    try {
      batch = await api.get<UploadBatch>(`/uploads/batches/${batchId}`);
      errors = 0;
    } catch {
      if ((errors += 1) > 10) break;
      continue;
    }
    const working: Promise<void>[] = [];
    for (const item of batch.groups.flatMap((g) => g.items)) {
      const key = keys.get(item.id);
      const state = SERVER_STATES[item.status];
      if (key && state) update(jobId, key, { state, message: item.message, documentId: item.document_id });
      // Real per-stage progress (reading, chunking, embedding) from the same poll cycle.
      if (key && item.document_id && (state === "processing" || state === "registered")) {
        working.push(
          readStages(item.document_id).then((stages) => {
            if (stages) update(jobId, key, { stages });
          }),
        );
      }
    }
    await Promise.all(working);
    if (batch.status === "completed" || batch.status === "attention") break;
  }
  store.dispatch(jobUpdated({ id: jobId, patch: { phase: "finished" } }));
  for (const key of ["category-documents", "category-pending", "category-overview", "category", "policies", "policy", "dashboard"]) {
    void queryClient.invalidateQueries({ queryKey: [key] });
  }
}
