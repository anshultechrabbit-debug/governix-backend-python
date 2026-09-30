"""Vendor-neutral HTTP adapter for managed OCR services."""

import base64
import json
import logging
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pymupdf

from app.infrastructure.ai.ocr.base import OCRProvider, OCRResult


logger = logging.getLogger(__name__)


class CloudOCR(OCRProvider):
    """Call a secured OCR gateway with one rendered page at a time.

    A gateway avoids tying business code to a cloud vendor. It must accept
    ``{"image_base64": ..., "mime_type": "image/png"}`` and return a JSON
    object containing non-empty ``text``. Sending individual pages maintains
    the ingestion pipeline's bounded memory and retry semantics.
    """

    name = "cloud"

    def __init__(self, url: str | None, api_key: str, timeout_seconds: float) -> None:
        self.url = url
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def is_available(self) -> bool:
        return bool(self.url and self.api_key)

    def ocr_page(self, page: pymupdf.Page) -> OCRResult:
        if not self.is_available():
            raise RuntimeError("Cloud OCR is not configured.")
        png = page.get_pixmap(dpi=300, alpha=False).tobytes("png")
        payload = json.dumps({
            "image_base64": base64.b64encode(png).decode("ascii"),
            "mime_type": "image/png",
        }).encode("utf-8")
        request = Request(
            self.url,
            data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310 - configured endpoint
                body = json.loads(response.read())
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            logger.warning("Cloud OCR request failed: %s", type(exc).__name__)
            raise RuntimeError("Cloud OCR request failed.") from exc
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str):
            raise RuntimeError("Cloud OCR returned an invalid response.")
        return OCRResult(text=text, engine=self.name)
