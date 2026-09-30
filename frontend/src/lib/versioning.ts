/**
 * First guess at how dropped files belong together. Files whose names differ
 * only by version markers ("KYC_Policy_v2.pdf", "KYC Policy v3 final.pdf")
 * are one policy, ordered by the version number in the name, then by the
 * file's modified time. The person can rearrange everything afterwards.
 */

const VERSION_TOKEN = /\b(?:v|ver|version|rev|revision|r)[\s._-]*(\d+(?:\.\d+)*)\b/i;
const NOISE = /\b(?:final|draft|copy|updated|update|latest|new|old|signed|scan(?:ned)?|clean|approved|revised)\b/gi;

/** The name with version markers, years and noise words removed: "kyc policy". */
export function familyKey(filename: string): string {
  return filename
    .replace(/\.[a-z0-9]+$/i, "")
    .replace(/[_\-.()[\]]+/g, " ")
    .replace(new RegExp(VERSION_TOKEN.source, "gi"), " ")
    .replace(/\b(?:19|20)\d{2}\b/g, " ")
    .replace(NOISE, " ")
    .replace(/\b\d+\b/g, " ")
    .toLowerCase()
    .replace(/\s+/g, " ")
    .trim();
}

/** The version number in a file name, for ordering: "Policy v4.1.pdf" -> [4, 1]. */
export function versionInName(filename: string): number[] | null {
  const match = filename.replace(/[_]+/g, " ").match(VERSION_TOKEN);
  return match ? match[1].split(".").map(Number) : null;
}

function compareVersions(a: number[] | null, b: number[] | null): number {
  if (!a || !b) return 0;
  for (let i = 0; i < Math.max(a.length, b.length); i += 1) {
    const diff = (a[i] ?? 0) - (b[i] ?? 0);
    if (diff) return diff;
  }
  return 0;
}

/** Oldest first. */
export function orderVersions<T extends { file: File }>(items: T[]): T[] {
  return [...items].sort((x, y) =>
    compareVersions(versionInName(x.file.name), versionInName(y.file.name)) || x.file.lastModified - y.file.lastModified,
  );
}

export function groupByFamily<T extends { file: File }>(items: T[]): T[][] {
  const families = new Map<string, T[]>();
  for (const item of items) {
    const key = familyKey(item.file.name) || item.file.name;
    families.set(key, [...(families.get(key) ?? []), item]);
  }
  return [...families.values()].map(orderVersions);
}

/** Clean a filename into an auto-suggested human policy name */
export function cleanPolicyName(filename: string): string {
  let name = filename.replace(/\.[a-z0-9]+$/i, "");
  // Strip leading date/numeric prefixes like "011123-", "2024-01-15_", "01_"
  name = name.replace(/^[\d]{2,8}[-_.\s]+/, "");
  // Remove version tokens like "v2", "version 3", "v1.0"
  name = name.replace(/\b(?:v|ver|version|rev|revision)[\s._-]*\d+(?:\.\d+)*\b/gi, "");
  // Replace underscores and multiple dashes with spaces
  name = name.replace(/[_-]+/g, " ");
  // Clean up extra spaces
  name = name.replace(/\s+/g, " ").trim();
  return name || filename.replace(/\.[a-z0-9]+$/i, "");
}

/** Check if a filename or cleaned title matches any existing active policy */
export function findMatchingPolicy<P extends { id: string; name: string; category_id?: string | null }>(
  filename: string,
  policies: P[],
): P | null {
  if (!policies || !policies.length) return null;
  const clean = cleanPolicyName(filename).toLowerCase().trim();
  if (!clean) return null;

  // 1. Direct or substring matches
  for (const p of policies) {
    const pClean = p.name.toLowerCase().trim();
    if (clean === pClean) return p;
    if (clean.length > 5 && pClean.length > 5 && (clean.includes(pClean) || pClean.includes(clean))) {
      return p;
    }
  }

  // 2. Word overlap matches (ignoring common stopwords)
  const stopWords = new Set(["policy", "the", "and", "for", "on", "of", "in", "to", "with", "a", "an", "bank", "banks", "document"]);
  const cleanTokens = clean.split(/[\s,.-]+/).filter((w) => w.length > 2 && !stopWords.has(w));
  if (!cleanTokens.length) return null;

  let bestMatch: P | null = null;
  let bestScore = 0;

  for (const p of policies) {
    const pClean = p.name.toLowerCase().trim();
    const pTokens = pClean.split(/[\s,.-]+/).filter((w) => w.length > 2 && !stopWords.has(w));
    if (!pTokens.length) continue;
    const common = cleanTokens.filter((t) => pTokens.includes(t)).length;
    const score = common / Math.max(cleanTokens.length, pTokens.length);
    if (score >= 0.4 && score > bestScore) {
      bestScore = score;
      bestMatch = p;
    }
  }

  return bestMatch;
}
