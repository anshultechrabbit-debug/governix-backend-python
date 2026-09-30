import logging
from functools import cached_property

import pymupdf

from app.infrastructure.ai.ocr.base import OCRProvider, OCRResult

logger = logging.getLogger(__name__)


class LocalOCR(OCRProvider):
    """Tesseract via PyMuPDF. Requires the tesseract binary and language data."""

    name = "tesseract"

    def __init__(self, language: str, dpi: int) -> None:
        self.language = language
        self.dpi = dpi

    @cached_property
    def _tessdata(self) -> str | None:
        try:
            return pymupdf.get_tessdata()
        except RuntimeError:
            logger.warning("Tesseract language data not found; OCR is unavailable.")
            return None

    def is_available(self) -> bool:
        return self._tessdata is not None

    def ocr_page(self, page: pymupdf.Page) -> OCRResult:
        textpage = page.get_textpage_ocr(
            language=self.language, dpi=self.dpi, full=True, tessdata=self._tessdata
        )
        return OCRResult(text=page.get_text(textpage=textpage), engine=self.name)
