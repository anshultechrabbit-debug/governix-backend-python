import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.exc import OperationalError

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import Settings, WorkerMode, get_settings
from app.core.database import Base, create_session_factory
from app.core.model_registry import import_all_models

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _test_database_url() -> str:
    if url := os.environ.get("TEST_DATABASE_URL"):
        return Settings(DATABASE_URL=url).DATABASE_URL
    url = make_url(get_settings().DATABASE_URL)
    return url.set(database=f"{url.database}_test").render_as_string(hide_password=False)


def alembic_config(database_url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["database_url"] = database_url
    config.attributes["configure_logger"] = False
    return config


@pytest.fixture(scope="session")
def test_database_url() -> Iterator[str]:
    url = _test_database_url()
    target = make_url(url)
    admin_engine = create_engine(
        target.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        with admin_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": target.database}
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{target.database}"'))
    except OperationalError as exc:
        pytest.skip(f"PostgreSQL not reachable for integration tests: {type(exc).__name__}")
    finally:
        admin_engine.dispose()

    config = alembic_config(url)
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    yield url


@pytest.fixture
def settings(tmp_path: Path, test_database_url: str) -> Settings:
    return Settings(
        APP_ENV="test",
        DATABASE_URL=test_database_url,
        WORKER_MODE=WorkerMode.DISABLED,
        LOCAL_STORAGE_PATH=tmp_path / "storage",
        JOB_LEASE_SECONDS=60,
        EMBEDDING_PROVIDER="local",
        LLM_PROVIDER="local",
    )


@pytest.fixture
def db_engine(settings: Settings):
    import_all_models()
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    engine = create_engine(settings.DATABASE_URL)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} CASCADE"))
    yield engine
    engine.dispose()


@pytest.fixture
def db(db_engine) -> Iterator[Session]:
    with create_session_factory(db_engine)() as session:
        yield session


@pytest.fixture
def app(settings: Settings, db_engine):
    from app.main import create_app

    return create_app(settings)


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
