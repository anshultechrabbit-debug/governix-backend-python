/**
 * Files dropped on one page and handled on another (e.g. new versions dropped
 * on a policy page open the upload planner). File objects cannot travel in a
 * URL, so they are kept here in memory and taken exactly once.
 */
let pending: File[] = [];

export function handOffFiles(files: File[]) {
  pending = files;
}

export function takeHandedOffFiles(): File[] {
  const files = pending;
  pending = [];
  return files;
}
