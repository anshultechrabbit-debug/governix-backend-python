from abc import ABC, abstractmethod
from dataclasses import dataclass

import pymupdf


@dataclass(frozen=True)
class OCRResult:
    text: str
    engine: str


class OCRProvider(ABC):
    name: str

    @abstractmethod
    def is_available(self) -> bool:
        """Whether the engine can run here (binaries, credentials)."""

    @abstractmethod
    def ocr_page(self, page: pymupdf.Page) -> OCRResult: ...
