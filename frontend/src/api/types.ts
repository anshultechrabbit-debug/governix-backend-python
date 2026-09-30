export type UUID = string;

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface Me {
  id: UUID;
  email: string;
  full_name: string;
  role: "master_admin" | "org_admin" | "branch_manager" | "department_user";
  organization_id: UUID | null;
  organization_name: string | null;
  branch_id: UUID | null;
  department_id: UUID | null;
  permissions: string[];
}

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  expires_in: number;
}

export type CategoryType =
  | "policy" | "circular" | "sop" | "guideline" | "manual" | "notice" | "process_document" | "form" | "faq"
  | "regulatory_document" | "other";

export interface Category {
  id: UUID;
  name: string;
  slug: string;
  description: string | null;
  category_type: CategoryType;
  authority_rank: number;
  keywords: string[];
  is_active: boolean;
  is_system: boolean;
  created_at: string;
  updated_at: string;
}

export interface CategorySummary extends Category {
  document_count: number;
  last_updated: string;
}

export interface StageProgress {
  stage: string;
  status: "pending" | "running" | "completed" | "skipped" | "failed" | "waiting";
  done_units: number;
  total_units: number | null;
  percent: number | null;
  started_at: string | null;
  finished_at: string | null;
  estimated_seconds_remaining: number | null;
  detail: Record<string, any>;
}

export interface Progress {
  overall_percent: number;
  stages: StageProgress[];
}

export type DocumentStatus =
  | "uploaded" | "processing" | "awaiting_confirmation" | "indexing" | "ready" | "failed" | "rejected" | "archived";

export interface DocumentRead {
  id: UUID;
  organization_id: UUID;
  branch_id: UUID | null;
  department_id: UUID | null;
  category_id: UUID | null;
  policy_id: UUID | null;
  policy_version_id: UUID | null;
  title: string | null;
  original_filename: string;
  content_type: string;
  size_bytes: number;
  file_sha256: string;
  page_count: number | null;
  status: DocumentStatus;
  error: { stage?: string; code?: string; message?: string } | null;
  uploaded_by_id: UUID | null;
  duplicate_of_id: UUID | null;
  created_at: string;
  ready_at: string | null;
}

export interface DocumentDetail extends DocumentRead {
  pdf_metadata: Record<string, any>;
  duplicate_override_reason: string | null;
  progress: Progress | null;
}

export interface Signal {
  signal: string;
  matched: boolean;
  weight: number;
  detail: string;
}

export interface Detected {
  value: string | null;
  confidence: number;
  source: string | null;
  evidence: string | null;
}

export type Decision =
  | "NEW_POLICY" | "EXISTING_POLICY_NEW_VERSION" | "EXISTING_POLICY_AMENDMENT" | "EXACT_DUPLICATE"
  | "CONTENT_DUPLICATE" | "VERSION_CONFLICT" | "POSSIBLE_MATCH_REQUIRES_REVIEW";

export interface Analysis {
  document_id: UUID;
  detected: {
    title: Detected;
    policy_number: Detected;
    document_number: Detected;
    circular_number: Detected;
    version_label: Detected;
    revision_number: Detected;
    effective_date: string | null;
    effective_date_evidence: string | null;
    issue_date: string | null;
    issuer: Detected;
    department: Detected;
    amendments: { relation_type: string; target_text: string; clauses: string[]; sentence: string }[];
    notes?: string[];
  };
  suggested_name: string | null;
  name_confidence: number;
  suggested_category_id: UUID | null;
  suggested_category_name: string | null;
  category_confidence: number;
  category_ranking: { category_id: UUID; name: string; score: number; matched: string[] }[];
  decision: Decision;
  confidence: number;
  matched_policy: null | {
    restricted?: boolean;
    message?: string;
    policy_id?: UUID;
    name?: string;
    policy_number?: string | null;
    category_id?: UUID;
    version_count?: number;
    latest_version?: { id: UUID; label: string; effective_from: string } | null;
  };
  duplicate_of_document_id: UUID | null;
  signals: Signal[];
  candidates: any[];
  amendment_targets: {
    relation_type: string;
    policy_id: UUID;
    policy_name: string;
    version_id: UUID | null;
    version_label: string | null;
    clauses: string[];
    evidence: string;
    confidence: number;
  }[];
  conflict: Record<string, any> | null;
  missing_fields: string[];
  review_status: "pending" | "confirmed" | "rejected";
  resolution: Record<string, any> | null;
  suggested_initial_version: {
    version_label: string | null;
    revision_number: string | null;
    effective_from: string | null;
    effective_date_evidence: string | null;
  } | null;
}

export interface Version {
  id: UUID;
  policy_id: UUID;
  document_id: UUID;
  version_number: number;
  version_label: string;
  revision_number: number;
  effective_from: string;
  /** Only "entered" and "detected" are business dates; the others only keep versions in order. */
  effective_date_source: EffectiveDateSource;
  effective_to: string | null;
  status: "active" | "withdrawn";
  timeline_state: "current" | "historical" | "scheduled" | "withdrawn" | null;
  version_label_auto: boolean;
  has_ai_summary: boolean;
  supersedes_version_id: UUID | null;
  superseded_by_version_id: UUID | null;
  created_by_id: UUID | null;
  confirmed_at: string | null;
  created_at: string;
  withdrawn_reason: string | null;
}

export interface VersionDetail extends Version {
  change_summary: Comparison | null;
  ai_change_summary: string | null;
}

export interface Policy {
  id: UUID;
  branch_id: UUID | null;
  department_id: UUID | null;
  category_id: UUID;
  name: string;
  policy_number: string | null;
  document_number: string | null;
  issuer: string | null;
  issuing_department: string | null;
  owner: string | null;
  description: string | null;
  status: "active" | "archived";
  created_at: string;
  display_order: number | null;
  current_version: Version | null;
  version_count: number;
}

export interface Relationship {
  id: UUID;
  source_document_id: UUID;
  source_title: string | null;
  source_policy_id: UUID | null;
  relation_type: string;
  target_policy_id: UUID;
  target_policy_name: string | null;
  target_version_id: UUID | null;
  clauses: string[];
  evidence: string | null;
  status: string;
  created_at: string;
}

export interface PolicyDetail extends Policy {
  versions: Version[];
  incoming_relationships: Relationship[];
  outgoing_relationships: Relationship[];
}

export interface SectionRef {
  number: string | null;
  title: string;
  label: string;
  page_start: number;
  page_end: number;
  content?: string;
}

export interface DiffOp {
  op: "equal" | "insert" | "delete" | "replace";
  old: string;
  new: string;
  truncated?: boolean;
}

export interface NumericChange {
  kind: string;
  old: string;
  new: string;
  old_context: string;
  new_context: string;
}

export interface Comparison {
  from_version: { id: UUID; label: string; effective_from: string };
  to_version: { id: UUID; label: string; effective_from: string };
  added: SectionRef[];
  removed: SectionRef[];
  modified: {
    old: SectionRef;
    new: SectionRef;
    title_changed: boolean;
    diff: DiffOp[] | null;
    numeric_changes: { changed: NumericChange[]; added: any[]; removed: any[] };
    similarity: number;
  }[];
  unchanged_count: number;
  stats: Record<string, number>;
  summary_lines: string[];
}

export interface Provenance {
  document_id: UUID;
  document_title: string | null;
  policy_id: UUID | null;
  policy_name: string | null;
  category_id: UUID | null;
  version_id: UUID | null;
  version_label: string | null;
  effective_from: string | null;
  effective_to: string | null;
  section_number: string | null;
  section_path: string;
  page_start: number;
  page_end: number;
}

export interface Passage {
  chunk_id: UUID;
  text: string;
  score: number;
  lanes: Record<string, number>;
  source: Provenance;
}

export interface SearchResponse {
  query: string;
  mode: string;
  as_of: string | null;
  passages: Passage[];
  policies: { id: UUID; name: string; policy_number: string | null; category_id: UUID }[];
  terms: string[];
  timings_ms: Record<string, number>;
  cache_hit: boolean;
}

export interface Source {
  number: number;
  evidence_id: string;
  kind: "passage" | "comparison";
  chunk_id: UUID | null;
  document_id: UUID | null;
  document_title: string | null;
  policy_id: UUID | null;
  policy_name: string | null;
  version_id: UUID | null;
  version_label: string | null;
  effective_from: string | null;
  effective_to: string | null;
  section_number: string | null;
  section_path: string | null;
  page_start: number | null;
  page_end: number | null;
  category: string | null;
  authority_rank: number | null;
  /** Answered from an earlier version because the version in force did not cover it. */
  previous_version: boolean;
  excerpt: string;
}

export interface Answer {
  query_id: UUID | null;
  question: string;
  status: "answered" | "no_answer";
  answer: string | null;
  /** The direct answer in plain words; absent when it could not be verified. */
  summary?: string | null;
  claims: { text: string; citations: number[] }[];
  sources: Source[];
  conflicts: { type: string; description: string; evidence_ids: string[]; citations?: number[]; resolution_hint?: string }[];
  warnings: string[];
  no_answer: { reason: string; message: string; suggestions: string[]; missing_terms: string[] } | null;
  plan: { query_class: string; mode: string; as_of: string | null; version_labels: string[]; explanation: string };
  evidence_score: number;
  model: string | null;
  usage: Record<string, number>;
  timings_ms: Record<string, number>;
  cache_hit: boolean;
}

export interface Branch { id: UUID; organization_id: UUID; name: string; code: string; is_active: boolean; created_at: string }
export interface Department extends Branch { branch_id: UUID }
export interface User {
  id: UUID; email: string; full_name: string; role: Me["role"]; organization_id: UUID | null;
  branch_id: UUID | null; department_id: UUID | null; is_active: boolean; last_login_at: string | null; created_at: string;
}
export interface Organization {
  id: UUID; name: string; slug: string; status: "active" | "suspended"; created_at: string;
  primary_color: string | null; secondary_color: string | null; contact_email: string | null;
  contact_phone: string | null; website: string | null; address: string | null; has_logo: boolean;
}
export interface AuditEvent {
  id: UUID; organization_id: UUID | null; actor_user_id: UUID | null; action: string; resource_type: string | null;
  resource_id: UUID | null; request_id: string | null; details: Record<string, any>; created_at: string;
}
export interface Dashboard {
  counts: Record<string, number>;
  recent_changes: { policy_id: UUID; policy_name: string; version_id: UUID; version_label: string; effective_from: string; effective_date_source: EffectiveDateSource; confirmed_at: string | null }[];
  recent_queries: { id: UUID; question: string; status: string | null; created_at: string }[];
  attention: { id: UUID; title: string; status: DocumentStatus; reason: string | null }[];
}

export type EffectiveDateSource = "entered" | "detected" | "upload_date" | "inferred";

export type UploadItemStatus = "awaiting_file" | "processing" | "confirmed" | "needs_review" | "failed" | "cancelled";

export interface UploadBatchItem {
  id: UUID;
  position: number;
  original_filename: string;
  document_id: UUID | null;
  document_status: string | null;
  version_label: string | null;
  effective_from: string | null;
  status: UploadItemStatus;
  message: string | null;
  policy_version_id: UUID | null;
}

export interface UploadBatchGroup {
  id: UUID;
  position: number;
  policy_id: UUID | null;
  policy_name: string | null;
  new_policy_name: string | null;
  category_id: UUID | null;
  status: "pending" | "confirmed" | "attention";
  message: string | null;
  items: UploadBatchItem[];
}

export interface UploadBatch {
  id: UUID;
  status: "uploading" | "processing" | "completed" | "attention";
  created_at: string;
  completed_at: string | null;
  created_by_id: UUID | null;
  counts: Record<string, number>;
  groups: UploadBatchGroup[];
}

export type UploadBatchSummary = Omit<UploadBatch, "groups" | "created_by_id">;

/** What a file's opening pages say about it, read while uploads are being arranged. */
export interface PolicySuggestion {
  /** False for scanned or unreadable files: their name is only known after OCR. */
  readable: boolean;
  name: string | null;
  /** What the content covers ("money laundering, customer acceptance and terrorist financing"). */
  about: string | null;
  name_source: string | null;
  name_confidence: number;
  category_id: UUID | null;
  category_name: string | null;
  version_label: string | null;
  effective_date: string | null;
  existing_policy: ExistingPolicyMatch | null;
  identical_document: {
    document_id: UUID;
    title: string | null;
    original_filename: string;
    policy_id: UUID | null;
    policy_name: string | null;
  } | null;
}

export interface ExistingPolicyMatch {
  policy_id: UUID;
  name: string;
  category_id: UUID;
  category_name: string | null;
  current_version_label: string | null;
  current_effective_from: string | null;
  version_count: number;
  reasons: string[];
  can_add_version: boolean;
}

/* ---------- Categories: policies and their versions ---------- */

export interface CategoryVersion extends Version {
  filename: string;
  title: string | null;
  content_type: string;
  size_bytes: number;
  page_count: number | null;
  document_status: DocumentStatus;
  uploaded_at: string;
  uploaded_by_id: UUID | null;
  uploaded_by_name: string | null;
}

export interface CategoryDocument {
  id: UUID;
  name: string;
  policy_number: string | null;
  description: string | null;
  status: "active" | "archived";
  branch_id: UUID | null;
  department_id: UUID | null;
  display_order: number | null;
  created_at: string;
  updated_at: string;
  version_count: number;
  current_version_id: UUID | null;
  latest_upload_at: string | null;
  versions: CategoryVersion[];
}

export interface PendingUpload {
  document_id: UUID;
  filename: string;
  title: string | null;
  status: DocumentStatus;
  size_bytes: number;
  uploaded_at: string;
  uploaded_by_name: string | null;
  error: string | null;
}

export interface VersionSummaryItem { text: string; section: string | null }

export interface VersionSummary {
  status: "ready" | "pending";
  ai_generated: true;
  model: string | null;
  generated_at: string | null;
  version_label: string;
  effective_from: string;
  effective_date_source: EffectiveDateSource;
  effective_to: string | null;
  important_changes: string[];
  change_summary: string | null;
  overview?: string | null;
  purpose?: string | null;
  key_rules?: VersionSummaryItem[];
  user_actions?: VersionSummaryItem[];
  exceptions?: VersionSummaryItem[];
  applicable_to?: string | null;
}

/* ---------- Assignments ---------- */

export interface Assignment {
  id: UUID;
  policy_id: UUID;
  policy_name: string;
  category_id: UUID;
  category_name: string | null;
  user_id: UUID;
  user_name: string;
  user_email: string;
  branch_id: UUID | null;
  assigned_by_name: string | null;
  assigned_at: string;
  removed_at: string | null;
  removed_by_name: string | null;
}

export interface AssignableUser {
  id: UUID;
  full_name: string;
  email: string;
  branch_id: UUID | null;
  department_id: UUID | null;
  assigned: boolean;
}

/* ---------- Notifications ---------- */

export interface AppNotification {
  id: UUID;
  type: string;
  title: string;
  body: string | null;
  link: string | null;
  data: Record<string, any>;
  read_at: string | null;
  created_at: string;
}

export interface Inbox { items: AppNotification[]; total: number; unread: number; limit: number; offset: number }

/* ---------- Complaints and support ---------- */

export type TicketStatus = "open" | "in_progress" | "waiting_for_response" | "resolved" | "closed";

export interface Ticket {
  id: UUID;
  organization_id: UUID;
  branch_id: UUID | null;
  kind: "complaint" | "support";
  level: "branch" | "organization" | "platform";
  category: string | null;
  subject: string;
  description: string;
  priority: "low" | "medium" | "high" | "urgent";
  status: TicketStatus;
  created_by_id: UUID | null;
  created_by_name: string | null;
  assigned_to_id: UUID | null;
  assigned_to_name: string | null;
  message_count: number;
  can_handle: boolean;
  created_at: string;
  updated_at: string;
  resolved_at: string | null;
  closed_at: string | null;
}

export interface TicketMessage {
  id: UUID;
  author_id: UUID | null;
  author_name: string | null;
  author_role: Me["role"] | null;
  body: string | null;
  status_change: TicketStatus | null;
  created_at: string;
}

export interface TicketAttachment { id: UUID; filename: string; content_type: string; size_bytes: number; uploaded_by_id: UUID | null; created_at: string }

export interface TicketDetail extends Ticket { messages: TicketMessage[]; attachments: TicketAttachment[] }

/* ---------- Uploads ---------- */

export interface UploadLimits { max_size_bytes: number; accepted_extensions: string[]; accepted_types: string[] }

export interface DuplicateResult {
  sha256: string;
  duplicate: boolean;
  restricted: boolean;
  existing: { document_id: UUID; title: string | null; original_filename: string; uploaded_at: string; policy_id: UUID | null } | null;
}
