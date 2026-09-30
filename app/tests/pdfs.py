"""Generate realistic banking PDFs for tests (no binary fixtures in the repo)."""

from dataclasses import dataclass, field

import pymupdf


@dataclass
class Section:
    number: str
    title: str
    paragraphs: list[str]


@dataclass
class PolicySpec:
    title: str = "HOME LOAN CREDIT POLICY"
    header_lines: list[str] = field(default_factory=lambda: [
        "Policy No: HL-2025-01",
        "Version: 3",
        "Issued by: Credit Department",
        "Effective Date: 01/01/2025",
    ])
    sections: list[Section] = field(default_factory=lambda: [
        Section("1", "Purpose", [
            "This policy sets out the credit standards for home loans offered by the Bank.",
        ]),
        Section("2", "Eligibility", [
            "Applicants must be resident Indians aged between 21 and 65 years.",
            "Minimum net monthly income shall be Rs. 25,000.",
        ]),
        Section("5", "Loan to Value", [
            "The maximum loan to value (LTV) ratio shall be 80% for loans up to Rs. 30 lakh.",
        ]),
        Section("5.2", "LTV for High Value Loans", [
            "For loans above Rs. 75 lakh the LTV shall not exceed 75%.",
        ]),
        Section("6", "Interest Rate", [
            "The interest rate shall be linked to the repo rate with a spread of 2.50% per annum.",
        ]),
    ])
    extra_pages: int = 0


def build_policy_pdf(spec: PolicySpec | None = None) -> bytes:
    spec = spec or PolicySpec()
    doc = pymupdf.open()
    page = doc.new_page()
    y = 72
    page.insert_text((72, y), spec.title, fontsize=18, fontname="helv")
    y += 30
    for line in spec.header_lines:
        page.insert_text((72, y), line, fontsize=10, fontname="helv")
        y += 16
    y += 10
    for section in spec.sections:
        if y > 700:
            page = doc.new_page()
            y = 72
        page.insert_text((72, y), f"{section.number} {section.title}", fontsize=13, fontname="hebo")
        y += 20
        for paragraph in section.paragraphs:
            for line in _wrap(paragraph, 90):
                if y > 760:
                    page = doc.new_page()
                    y = 72
                page.insert_text((72, y), line, fontsize=10, fontname="helv")
                y += 14
            y += 6
    for n in range(spec.extra_pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"Annexure page {n + 1}", fontsize=10, fontname="helv")
    doc.set_metadata({"title": "loan_policy_final_v7", "author": "test"})
    data = doc.tobytes()
    doc.close()
    return data


def build_blank_pdf(pages: int = 1) -> bytes:
    """A PDF with no text layer (stands in for a scanned document)."""
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page()
        page.draw_rect(pymupdf.Rect(100, 100, 300, 300), fill=(0.8, 0.8, 0.8))
    data = doc.tobytes()
    doc.close()
    return data


def _wrap(text: str, width: int) -> list[str]:
    words, lines, line = text.split(), [], ""
    for word in words:
        if len(line) + len(word) + 1 > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        lines.append(line)
    return lines


def build_scanned_pdf(pages: int = 1) -> bytes:
    """Image-only pages, like a scanner produces (no text layer)."""
    doc = pymupdf.open()
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 60), 0)
    pixmap.clear_with(200)
    for _ in range(pages):
        page = doc.new_page()
        page.insert_image(pymupdf.Rect(72, 72, 500, 700), pixmap=pixmap)
    data = doc.tobytes()
    doc.close()
    return data


def build_encrypted_pdf() -> bytes:
    doc = pymupdf.open(stream=build_policy_pdf(), filetype="pdf")
    data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="owner")
    doc.close()
    return data
