from app.core.config import OCRBackend, Settings
from app.infrastructure.ai.ocr.base import OCRProvider


def create_ocr(settings: Settings) -> OCRProvider:
    if settings.OCR_BACKEND is OCRBackend.CLOUD:
        from app.infrastructure.ai.ocr.cloud import CloudOCR

        return CloudOCR(
            settings.CLOUD_OCR_URL,
            settings.CLOUD_OCR_API_KEY.get_secret_value() if settings.CLOUD_OCR_API_KEY else "",
            settings.CLOUD_OCR_TIMEOUT_SECONDS,
        )
    # "local" and "worker" use the same engine; "worker" only means OCR jobs
    # are consumed by dedicated worker processes in production.
    from app.infrastructure.ai.ocr.local import LocalOCR

    return LocalOCR(settings.OCR_LANGUAGE, settings.OCR_DPI)
