import { createSlice, type PayloadAction } from "@reduxjs/toolkit";

/**
 * Bulk uploads that run in the background (serialisable state only; the File
 * objects and live requests are held by lib/uploadManager).
 */
export type QueueState =
  | "pending"     // waiting for its turn
  | "uploading"   // bytes on the way
  | "processing"  // received; being read, analysed and registered
  | "registered"  // a version of a policy in the category
  | "review"      // needs a person (duplicate content, version conflict, ...)
  | "failed"
  | "cancelled";

export interface QueueStage {
  stage: string;
  status: string;
  done_units: number;
  total_units: number | null;
  estimated_seconds_remaining: number | null;
}

export interface QueueItem {
  key: string;
  name: string;
  size: number;
  state: QueueState;
  progress: number; // 0..1 while uploading
  message: string | null;
  itemId: string | null;
  documentId: string | null;
  /** Real pipeline stages, once the file has a document to report on. */
  stages: QueueStage[] | null;
}

export interface UploadJob {
  id: string;
  title: string;
  link: string; // where the uploaded documents appear
  phase: "uploading" | "processing" | "finished";
  batchId: string | null;
  items: QueueItem[];
  startedAt: number;
}

interface UploadsState {
  jobs: UploadJob[];
  openJobId: string | null;
}

const initialState: UploadsState = { jobs: [], openJobId: null };

const uploadsSlice = createSlice({
  name: "uploads",
  initialState,
  reducers: {
    jobAdded(state, action: PayloadAction<UploadJob>) {
      state.jobs.unshift(action.payload);
      state.openJobId = action.payload.id;
    },
    jobUpdated(state, action: PayloadAction<{ id: string; patch: Partial<Omit<UploadJob, "items">> }>) {
      const job = state.jobs.find((j) => j.id === action.payload.id);
      if (job) Object.assign(job, action.payload.patch);
    },
    itemUpdated(state, action: PayloadAction<{ jobId: string; key: string; patch: Partial<QueueItem> }>) {
      const item = state.jobs.find((j) => j.id === action.payload.jobId)?.items.find((i) => i.key === action.payload.key);
      if (item) Object.assign(item, action.payload.patch);
    },
    jobOpened(state, action: PayloadAction<string | null>) {
      state.openJobId = action.payload;
    },
    jobDismissed(state, action: PayloadAction<string>) {
      state.jobs = state.jobs.filter((j) => j.id !== action.payload);
      if (state.openJobId === action.payload) state.openJobId = null;
    },
  },
});

export const { jobAdded, jobUpdated, itemUpdated, jobOpened, jobDismissed } = uploadsSlice.actions;
export default uploadsSlice.reducer;
