from app.core.config import Settings, StorageBackend
from app.infrastructure.storage.base import Storage


def create_storage(settings: Settings) -> Storage:
    if settings.STORAGE_BACKEND is StorageBackend.S3:
        from app.infrastructure.storage.s3 import S3Storage

        return S3Storage(settings)

    from app.infrastructure.storage.local import LocalStorage

    return LocalStorage(settings.LOCAL_STORAGE_PATH)
