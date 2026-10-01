"""Per-page text and layout extraction with PyMuPDF.

Ruled tables are emitted one line per row ("Header: cell | Header: cell")
instead of line by line. Line-by-line extraction interleaves the columns of a
multi-line row, so a comment, its author and the official response to it end
up in an order that no longer says who said what.
"""

import logging
from dataclasses import dataclass, field

import pymupdf

from app.modules.documents.model import ExtractionMethod

logger = logging.getLogger(__name__)

BOLD_FLAG = 1 << 4
# Table rows use negative block numbers so later stages can tell them apart from
# prose: each row is its own paragraph and is never mistaken for a heading.
TABLE_BLOCK_BASE = -1
MIN_TABLE_ROWS = 2
# A ruled table of two rows and two columns is drawn with at least this many line or
# rectangle segments. A page with fewer cannot hold one, so its (slow) table search is
# skipped. On real documents table pages had 58 or more.
MIN_RULING_SEGMENTS = 4
HEADER_BOLD_RATIO = 0.5
# Keep ligatures/whitespace sane and drop invisible text used for tricks.
TEXT_FLAGS = (
    pymupdf.TEXT_PRESERVE_WHITESPACE
    | pymupdf.TEXT_MEDIABOX_CLIP
    | pymupdf.TEXT_DEHYPHENATE
)


@dataclass
class PageExtraction:
    page_number: int
    text: str
    lines: list[list] = field(default_factory=list)  # [block, text, size, bold, y0]
    method: ExtractionMethod = ExtractionMethod.TEXT
    width: float | None = None
    height: float | None = None
    has_images: bool = False

    @property
    def char_count(self) -> int:
        return len(self.text.strip())


def is_table_block(block: int) -> bool:
    return block <= TABLE_BLOCK_BASE


def _is_bold(span: dict) -> bool:
    return bool(span.get("flags", 0) & BOLD_FLAG) or "bold" in span.get("font", "").lower()


def _clean_cell(cell: str | None) -> str:
    return " ".join((cell or "").split())


def _bold_ratio(spans: list[tuple[pymupdf.Rect, str, bool]], rect: pymupdf.Rect) -> float:
    total = bold = 0
    for bbox, text, is_bold in spans:
        centre = pymupdf.Point((bbox.x0 + bbox.x1) / 2, (bbox.y0 + bbox.y1) / 2)
        if centre in rect:
            total += len(text)
            bold += len(text) if is_bold else 0
    return bold / total if total else 0.0


def render_rows(rows: list[list[str]], header: list[str] | None) -> list[str]:
    """One text line per table row; cells keep their column header when known."""
    rendered = []
    for row in rows:
        if header:
            cells = [f"{h}: {c}" if h and h != c else c for h, c in zip(header, row, strict=False) if c]
        else:
            cells = [c for c in row if c]
        if cells:
            rendered.append(" | ".join(cells))
    return rendered


def _ruling_segments(page: pymupdf.Page, enough: int) -> int:
    """Line and rectangle segments drawn on the page, counted up to `enough`."""
    count = 0
    for drawing in page.get_cdrawings():
        for item in drawing.get("items", ()):
            if item[0] in ("l", "re", "qu"):
                count += 1
                if count >= enough:
                    return count
    return count


def _table_rows(page: pymupdf.Page, spans) -> list[tuple[pymupdf.Rect, list[tuple[float, str]]]]:
    """Ruled tables on the page as (bbox, [(row y0, row text)]).

    Only tables drawn with ruling lines are used ("lines_strict"), so ordinary
    multi-column prose is never mistaken for a table. Table search costs about six
    times the rest of a page's extraction, so a page without ruling lines skips it.
    """
    try:
        if _ruling_segments(page, MIN_RULING_SEGMENTS) < MIN_RULING_SEGMENTS:
            return []
        tables = page.find_tables(strategy="lines_strict").tables
    except Exception:  # table detection is an enhancement; never fail extraction on it
        logger.warning("Table detection failed on page %s", page.number + 1, exc_info=True)
        return []
    found = []
    for table in tables:
        rows = [[_clean_cell(c) for c in row] for row in table.extract()]
        filled_columns = {i for row in rows for i, c in enumerate(row) if c}
        if len(rows) < MIN_TABLE_ROWS or len(filled_columns) < 2:
            continue
        header = None
        if len(table.rows) > 1:
            first = _bold_ratio(spans, pymupdf.Rect(table.rows[0].bbox))
            second = _bold_ratio(spans, pymupdf.Rect(table.rows[1].bbox))
            if first >= HEADER_BOLD_RATIO and first - second >= 0.3:
                header = rows[0]
        body = rows[1:] if header else rows
        body_rects = table.rows[1:] if header else table.rows
        lines = []
        if header:
            lines.append((table.rows[0].bbox[1], " | ".join(c for c in header if c)))
        for row, rect in zip(body, body_rects, strict=False):
            for text in render_rows([row], header):
                lines.append((rect.bbox[1], text))
        found.append((pymupdf.Rect(table.bbox), lines))
    return found


def extract_page(page: pymupdf.Page, *, ocr_min_chars: int) -> PageExtraction:
    data = page.get_text("dict", flags=TEXT_FLAGS)
    lines: list[list] = []
    text_lines: list[str] = []
    has_images = False
    spans_on_page = [
        (pymupdf.Rect(s["bbox"]), s["text"], _is_bold(s))
        for block in data.get("blocks", []) if block.get("type") != 1
        for line in block.get("lines", []) for s in line.get("spans", []) if s.get("text", "").strip()
    ]
    tables = _table_rows(page, spans_on_page)
    emitted: set[int] = set()
    next_block = TABLE_BLOCK_BASE
    for block in data.get("blocks", []):
        if block.get("type") == 1:
            has_images = True
            continue
        for line in block.get("lines", []):
            spans = [s for s in line.get("spans", []) if s.get("text", "").strip()]
            if not spans:
                continue
            bbox = pymupdf.Rect(line["bbox"])
            centre = pymupdf.Point((bbox.x0 + bbox.x1) / 2, (bbox.y0 + bbox.y1) / 2)
            inside = next((i for i, (rect, _rows) in enumerate(tables) if centre in rect), None)
            if inside is not None:
                # The table replaces its raw lines, at the position of its first line.
                if inside not in emitted:
                    emitted.add(inside)
                    for y, row_text in tables[inside][1]:
                        lines.append([next_block, row_text, 0.0, False, round(y, 1)])
                        text_lines.append(row_text)
                        next_block -= 1
                continue
            line_text = "".join(s["text"] for s in line["spans"]).strip()
            # Dominant span (most characters) decides size and weight of the line.
            dominant = max(spans, key=lambda s: len(s["text"]))
            size = round(float(dominant.get("size", 0)), 1)
            bold = _is_bold(dominant)
            lines.append([block.get("number", 0), line_text, size, bold, round(line["bbox"][1], 1)])
            text_lines.append(line_text)

    if not has_images:
        has_images = bool(page.get_images(full=False))
    result = PageExtraction(
        page_number=page.number + 1,
        text="\n".join(text_lines),
        lines=lines,
        width=page.rect.width,
        height=page.rect.height,
        has_images=has_images,
    )
    if result.char_count < ocr_min_chars:
        # Scanned pages have images and no text layer; truly blank pages have neither.
        result.method = (
            ExtractionMethod.PENDING_OCR if has_images or result.char_count else ExtractionMethod.EMPTY
        )
    return result


def lines_from_plain_text(text: str) -> list[list]:
    """Layout lines for OCR output, which carries no font information."""
    lines = []
    for block, paragraph in enumerate(text.split("\n\n")):
        for line in paragraph.splitlines():
            if line.strip():
                lines.append([block, line.strip(), 0.0, False, 0.0])
    return lines


def extract_pages(path: str, numbers: list[int], ocr_min_chars: int) -> list[dict]:
    """Extract the given pages of the PDF at `path`, as page rows (without document ids).

    Runs in a worker process for large documents: it opens its own handle on the file,
    so pages of one document are extracted on several CPU cores at once.
    """
    pdf = pymupdf.open(path, filetype="pdf")
    try:
        rows = []
        for number in numbers:
            extraction = extract_page(pdf.load_page(number - 1), ocr_min_chars=ocr_min_chars)
            rows.append({
                "page_number": number,
                "text": extraction.text,
                "char_count": extraction.char_count,
                "method": extraction.method,
                "width": extraction.width,
                "height": extraction.height,
                "lines": extraction.lines,
            })
        return rows
    finally:
        pdf.close()
        pymupdf.TOOLS.store_shrink(100)
