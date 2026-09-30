import pytest
from pydantic import ValidationError

from app.core.config import Settings

DB = "postgresql://u:p@localhost/db"


def test_database_url_is_normalised_to_psycopg3():
    for url in (DB, "postgresql+psycopg2://u:p@localhost/db", "postgres://u:p@localhost/db"):
        assert Settings(DATABASE_URL=url).DATABASE_URL == "postgresql+psycopg://u:p@localhost/db"


def test_local_defaults_need_no_production_infrastructure():
    settings = Settings(DATABASE_URL=DB, _env_file=None)
    assert settings.STORAGE_BACKEND == "local"
    assert settings.QUEUE_BACKEND == "local"
    assert settings.CACHE_BACKEND == "memory"


def test_s3_backend_requires_bucket_and_credentials():
    with pytest.raises(ValidationError, match="S3_ACCESS_KEY, S3_BUCKET, S3_SECRET_KEY"):
        Settings(DATABASE_URL=DB, STORAGE_BACKEND="s3", _env_file=None)


@pytest.mark.parametrize("overrides", [
    {"QUEUE_BACKEND": "celery", "WORKER_MODE": "distributed"},
    {"CACHE_BACKEND": "redis"},
])
def test_redis_backed_services_require_redis_url(overrides):
    with pytest.raises(ValidationError, match="REDIS_URL"):
        Settings(DATABASE_URL=DB, _env_file=None, **overrides)


def test_celery_cannot_run_in_process():
    with pytest.raises(ValidationError, match="in_process"):
        Settings(
            DATABASE_URL=DB, QUEUE_BACKEND="celery", REDIS_URL="redis://x", _env_file=None
        )


def test_cloud_ocr_requires_an_endpoint_and_credential():
    with pytest.raises(ValidationError, match="CLOUD_OCR_API_KEY, CLOUD_OCR_URL"):
        Settings(DATABASE_URL=DB, OCR_BACKEND="cloud", _env_file=None)


def test_secrets_are_not_exposed_in_repr():
    settings = Settings(DATABASE_URL=DB, OPENAI_API_KEY="sk-supersecretvalue123456", _env_file=None)
    assert "supersecret" not in repr(settings)
