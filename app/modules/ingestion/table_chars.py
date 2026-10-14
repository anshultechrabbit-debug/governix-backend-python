"""Faster drop-ins for the two hot spots of PyMuPDF's table search, with identical results.

`make_chars` turns every character it searches into a dict through PyMuPDF's Python-level Rect,
Point and Matrix objects. On an upright page the page transform only flips and shifts the axes
(its entries are 0 and ±1), so the same values come from plain arithmetic, rounded to float32
the way MuPDF computes them: the dicts are identical, field for field, in about a quarter of the
time.

`Table.extract` tests every character of the page against every row of the table (and runs
twice per table, once for the header). Sorting the characters by their vertical middle once
makes each row's characters a slice, kept in their original order; the cell text is then built
exactly as before.

Installed only for the PyMuPDF version they were checked against (`requirements/base.txt`). A
rotated or skewed page, or a character box that is not left-to-right and top-to-bottom, goes
through PyMuPDF's own make_chars.
"""

import logging
import struct
from bisect import bisect_left

import pymupdf
import pymupdf.table as table

logger = logging.getLogger(__name__)

CHECKED_VERSION = "1.28.2"
_original = table.make_chars
_original_extract = table.Table.extract
_f32 = struct.Struct("f")


def _float32(value: float) -> float:
    """`value` rounded as MuPDF's single-precision geometry stores it."""
    return _f32.unpack(_f32.pack(value))[0]


def make_chars(page, clip=None):
    a, b, c, d, e, f = page.transformation_matrix
    if b != 0 or c != 0 or a not in (1, -1) or d not in (1, -1):
        return _original(page, clip=clip)
    ctm = page.transformation_matrix
    chars = table.CHARS._list()
    page_number = page.number + 1
    page_height = page.rect.height
    TEXTPAGE = page.get_textpage(clip=clip, flags=table.FLAGS)
    blocks = page.get_text("rawdict", textpage=TEXTPAGE)["blocks"]
    doctop_base = page_height * page.number
    for block in blocks:
        for line in block["lines"]:
            ldir = line["dir"]  # = (cosine, sine) of angle
            ldir = (round(ldir[0], 4), round(ldir[1], 4))
            m_a, m_b, m_c, m_d = ldir[0], -ldir[1], ldir[1], ldir[0]
            upright = ldir[1] == 0
            for span in sorted(line["spans"], key=lambda s: s["bbox"][0]):
                fontname = span["font"]
                fontsize = span["size"]
                span_bold = bool(span["flags"] & pymupdf.TEXT_FONT_BOLD or span["char_flags"] & 8)
                color = pymupdf.sRGB_to_pdf(span["color"])
                for char in sorted(span["chars"], key=lambda ch: ch["bbox"][0]):
                    x0, y0, x1, y1 = char["bbox"]
                    if x0 <= x1 and y0 <= y1:
                        # fz_transform_rect on an axis-aligned matrix: swap a flipped axis, map both corners.
                        if d < 0:
                            top, bottom = _float32(y1 * d + f), _float32(y0 * d + f)
                        else:
                            top, bottom = _float32(y0 * d + f), _float32(y1 * d + f)
                    else:
                        transformed = pymupdf.Rect(char["bbox"]) * ctm
                        top, bottom = transformed.y0, transformed.y1
                    ox, oy = char["origin"]
                    char_dict = {
                        "adv": x1 - x0 if upright else y1 - y0,
                        "bottom": y1,
                        "doctop": y0 + doctop_base,
                        "fontname": fontname,
                        "height": y1 - y0,
                        "matrix": (m_a, m_b, m_c, m_d, _float32(ox * a + e), _float32(oy * d + f)),
                        "ncs": "DeviceRGB",
                        "non_stroking_color": color,
                        "non_stroking_pattern": None,
                        "object_type": "char",
                        "page_number": page_number,
                        "size": fontsize if upright else y1 - y0,
                        "stroking_color": color,
                        "stroking_pattern": None,
                        "bold": span_bold,
                        "text": char["c"],
                        "top": y0,
                        "upright": upright,
                        "width": x1 - x0,
                        "x0": x0,
                        "x1": x1,
                        "y0": top,
                        "y1": bottom,
                    }
                    chars.append(char_dict)
    return TEXTPAGE


def extract(self, **kwargs) -> list:
    """`pymupdf.table.Table.extract`, selecting each row's characters from a sorted index."""
    chars = list(self._chars if self._chars is not None else table.CHARS)
    middles = [(char["top"] + char["bottom"]) / 2 for char in chars]
    order = sorted(range(len(chars)), key=middles.__getitem__)
    sorted_middles = [middles[i] for i in order]
    table_arr = []

    def char_in_bbox(char, bbox) -> bool:
        v_mid = (char["top"] + char["bottom"]) / 2
        h_mid = (char["x0"] + char["x1"]) / 2
        x0, top, x1, bottom = bbox
        return bool((h_mid >= x0) and (h_mid < x1) and (v_mid >= top) and (v_mid < bottom))

    for row in self.rows:
        arr = []
        x0, top, x1, bottom = row.bbox
        # The characters whose middle is in [top, bottom), in their original order.
        in_band = sorted(order[bisect_left(sorted_middles, top):bisect_left(sorted_middles, bottom)])
        row_chars = [chars[i] for i in in_band if x0 <= (chars[i]["x0"] + chars[i]["x1"]) / 2 < x1]

        for cell in row.cells:
            if cell is None:
                cell_text = None
            else:
                cell_chars = [char for char in row_chars if char_in_bbox(char, cell)]
                if len(cell_chars):
                    kwargs["x_shift"] = cell[0]
                    kwargs["y_shift"] = cell[1]
                    if "layout" in kwargs:
                        kwargs["layout_width"] = cell[2] - cell[0]
                        kwargs["layout_height"] = cell[3] - cell[1]
                    cell_text = table.extract_text(cell_chars, **kwargs)
                else:
                    cell_text = ""
            arr.append(cell_text)
        table_arr.append(arr)
    return table_arr


def install() -> bool:
    """Use the faster versions when PyMuPDF is the version they were checked against."""
    if pymupdf.VersionBind != CHECKED_VERSION:
        logger.info("PyMuPDF %s: table search keeps its own make_chars and extract (checked: %s)",
                    pymupdf.VersionBind, CHECKED_VERSION)
        return False
    table.make_chars = make_chars
    table.Table.extract = extract
    return True
