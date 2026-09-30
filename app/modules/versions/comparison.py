"""Deterministic comparison of two document versions, section by section.

The exact diff always comes from here; an LLM may only *summarise* this output.
Sections are aligned by number (5.2 <-> 5.2), then by normalised title, then
by fuzzy title similarity. Original text is always returned alongside the diff.
"""

import difflib
import re
from dataclasses import dataclass

from app.modules.citations.numerics import numeric_changes
from app.modules.ingestion.analysis.metadata import normalize_title

FUZZY_TITLE_RATIO = 0.75
CONTENT_MATCH_RATIO = 0.6
MAX_DIFF_TOKENS = 4000


@dataclass
class SectionView:
    number: str | None
    title: str
    content: str
    page_start: int
    page_end: int
    content_hash: str
    level: int

    @property
    def label(self) -> str:
        return f"{self.number} {self.title}".strip() if self.number else self.title


def merge_continuations(sections: list[SectionView]) -> list[SectionView]:
    """Oversized sections are stored in parts ("... (continued)"); compare them whole."""
    merged: list[SectionView] = []
    for section in sections:
        base_title = section.title.removesuffix(" (continued)")
        if merged and section.title.endswith("(continued)") and merged[-1].title == base_title and merged[-1].number == section.number:
            last = merged[-1]
            merged[-1] = SectionView(
                last.number, last.title, f"{last.content}\n\n{section.content}", last.page_start,
                section.page_end, last.content_hash + section.content_hash, last.level,
            )
        else:
            merged.append(section)
    return merged


def _align(old: list[SectionView], new: list[SectionView]) -> tuple[list[tuple[SectionView, SectionView]], list[SectionView], list[SectionView]]:
    pairs: list[tuple[SectionView, SectionView]] = []
    old_left, new_left = list(old), list(new)

    def take(match) -> None:
        for o in list(old_left):
            for n in new_left:
                if match(o, n):
                    pairs.append((o, n))
                    old_left.remove(o)
                    new_left.remove(n)
                    break

    take(lambda o, n: o.number is not None and o.number == n.number)
    take(lambda o, n: o.level == 0 and n.level == 0)  # front matter
    take(lambda o, n: normalize_title(o.title) == normalize_title(n.title))
    take(lambda o, n: _titles_nest(o.title, n.title) and _content_ratio(o, n) >= CONTENT_MATCH_RATIO)
    take(lambda o, n: difflib.SequenceMatcher(None, normalize_title(o.title), normalize_title(n.title)).ratio() >= FUZZY_TITLE_RATIO)
    return pairs, old_left, new_left


def _titles_nest(a: str, b: str) -> bool:
    a, b = normalize_title(a), normalize_title(b)
    return bool(a and b) and (a in b or b in a)


def _content_ratio(o: SectionView, n: SectionView) -> float:
    return difflib.SequenceMatcher(None, o.content, n.content, autojunk=False).quick_ratio()


def word_diff(old: str, new: str) -> list[dict]:
    """Word-level operations: equal / insert / delete / replace, with both texts."""
    old_tokens, new_tokens = re.findall(r"\S+|\s+", old), re.findall(r"\S+|\s+", new)
    if len(old_tokens) + len(new_tokens) > MAX_DIFF_TOKENS * 2:
        return [{"op": "replace", "old": old, "new": new, "truncated": True}]
    ops = []
    matcher = difflib.SequenceMatcher(None, old_tokens, new_tokens, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        ops.append({"op": tag, "old": "".join(old_tokens[i1:i2]), "new": "".join(new_tokens[j1:j2])})
    return ops


def _section_json(section: SectionView, include_content: bool) -> dict:
    data = {
        "number": section.number,
        "title": section.title,
        "label": section.label,
        "page_start": section.page_start,
        "page_end": section.page_end,
    }
    if include_content:
        data["content"] = section.content
    return data


def compare_sections(old: list[SectionView], new: list[SectionView], *, include_content: bool = True) -> dict:
    old, new = merge_continuations(old), merge_continuations(new)
    pairs, removed, added = _align(old, new)
    modified, unchanged = [], []
    for o, n in pairs:
        if o.content_hash == n.content_hash and o.title == n.title:
            unchanged.append({"old": _section_json(o, False), "new": _section_json(n, False)})
            continue
        numbers = numeric_changes(o.content, n.content)
        modified.append({
            "old": _section_json(o, include_content),
            "new": _section_json(n, include_content),
            "title_changed": normalize_title(o.title) != normalize_title(n.title),
            "diff": word_diff(o.content, n.content) if include_content else None,
            "numeric_changes": numbers,
            "similarity": round(difflib.SequenceMatcher(None, o.content, n.content, autojunk=False).quick_ratio(), 3),
        })
    order = {id(s): i for i, s in enumerate(new)}
    modified.sort(key=lambda m: m["new"]["page_start"])
    result = {
        "added": [_section_json(s, include_content) for s in sorted(added, key=lambda s: order[id(s)])],
        "removed": [_section_json(s, include_content) for s in removed],
        "modified": modified,
        "unchanged_count": len(unchanged),
        "stats": {
            "added": len(added),
            "removed": len(removed),
            "modified": len(modified),
            "unchanged": len(unchanged),
            "numeric_changes": sum(len(m["numeric_changes"]["changed"]) for m in modified),
        },
    }
    result["summary_lines"] = summary_lines(result)
    return result


def summary_lines(result: dict) -> list[str]:
    """Plain, deterministic summary ("Changed: 5.2 LTV (75% -> 70%)")."""
    lines = []
    for item in result["modified"]:
        label = item["new"]["label"]
        values = [f"{c['old']} → {c['new']}" for c in item["numeric_changes"]["changed"]]
        lines.append(f"Changed: {label}" + (f" ({', '.join(values)})" if values else ""))
    lines += [f"Added: {s['label']}" for s in result["added"]]
    lines += [f"Removed: {s['label']}" for s in result["removed"]]
    return lines
