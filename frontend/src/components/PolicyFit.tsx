import { FilePlus2, Layers } from "lucide-react";
import type { ExistingPolicyMatch, PolicySuggestion } from "../api/types";
import { formatDate } from "../lib/format";
import { Badge } from "./ui";

/** A file's read: "reading" while the server reads it, null when it could not be read before upload. */
export type Suggestion = PolicySuggestion | "reading" | null;

/**
 * How a file's content fits the policy it is being added to:
 *   match      the content matches that policy
 *   other      it matches a different existing policy
 *   different  it matches no existing policy: it looks like a new one
 *   unknown    not readable before upload (scanned, too large); checked after processing
 */
export type Fit = "reading" | "unknown" | "match" | "other" | "different";

export function policyFit(read: Suggestion | undefined, policyId: string): Fit {
  if (read === "reading") return "reading";
  if (!read?.readable) return "unknown";
  if (read.existing_policy?.policy_id === policyId) return "match";
  return read.existing_policy ? "other" : "different";
}

export const misplaced = (fit: Fit) => fit === "other" || fit === "different";

export function ExistingPolicyCard({ existing }: { existing: ExistingPolicyMatch }) {
  return (
    <div className="rounded-md border border-line bg-subtle/60 p-3">
      <div className="flex items-start gap-2.5">
        <Layers className="mt-0.5 size-4 shrink-0 text-brand-600" />
        <div className="min-w-0 space-y-1">
          <p className="font-medium">{existing.name}</p>
          <p className="text-xs text-muted">
            {[
              existing.category_name,
              existing.current_version_label && `current v${existing.current_version_label}${existing.current_effective_from ? ` since ${formatDate(existing.current_effective_from)}` : ""}`,
              `${existing.version_count} version${existing.version_count === 1 ? "" : "s"}`,
            ].filter(Boolean).join(" · ")}
          </p>
          {existing.reasons.length > 0 && (
            <div className="flex flex-wrap gap-1 pt-1">
              {existing.reasons.map((reason) => <Badge key={reason} tone="brand">{reason}</Badge>)}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/** A policy the document describes that does not exist yet. */
export function DetectedPolicyCard({ read }: { read: PolicySuggestion }) {
  return (
    <div className="rounded-md border border-line bg-subtle/60 p-3">
      <div className="flex items-start gap-2.5">
        <FilePlus2 className="mt-0.5 size-4 shrink-0 text-brand-600" />
        <div className="min-w-0 space-y-1">
          <p className="font-medium">{read.name ?? "Untitled document"}</p>
          {read.about && <p className="text-xs text-ink-soft">About {read.about}</p>}
          <p className="text-xs text-muted">
            {[
              read.category_name,
              read.version_label && `version ${read.version_label}`,
              read.effective_date && `effective ${formatDate(read.effective_date)}`,
            ].filter(Boolean).join(" · ") || "Read from the document"}
          </p>
          <div className="pt-1"><Badge tone="ai">Not in Governix yet</Badge></div>
        </div>
      </div>
    </div>
  );
}

/**
 * Why a file seems to belong elsewhere, from what its content is about, and what we
 * suggest: "... Its content is about transmission planning, so we suggest uploading
 * it as a new policy: National Electricity Plan."
 */
export function WhyElsewhere({ read, policyName }: { read: PolicySuggestion; policyName: string }) {
  const other = read.existing_policy;
  const about = read.about && <>Its content is about <span className="font-medium">{read.about}</span></>;
  return (
    <>
      This doesn't look like a version of <span className="font-medium">{policyName}</span>.{" "}
      {other ? (
        <>
          {about ? <>{about}, and it</> : "Its content"} matches <span className="font-medium">{other.name}</span>
          {other.reasons[0] ? ` (${other.reasons[0].toLowerCase()})` : ""}, so we suggest adding it there as a new version.
        </>
      ) : (
        <>
          {about ?? "Its content reads as a different document"}, so we suggest uploading it as a new policy
          {read.name ? <>: <span className="font-medium">{read.name}</span>.</> : "."}
        </>
      )}
    </>
  );
}

/** Where a misplaced file seems to belong, as a card. */
export function BelongsCard({ read }: { read: PolicySuggestion }) {
  return read.existing_policy ? <ExistingPolicyCard existing={read.existing_policy} /> : <DetectedPolicyCard read={read} />;
}
