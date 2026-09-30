import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, text

from app.core.database import Base
from app.core.model_registry import import_all_models
from app.tests.conftest import alembic_config

pytestmark = pytest.mark.integration


def test_downgrade_and_upgrade_roundtrip(test_database_url):
    config = alembic_config(test_database_url)
    command.downgrade(config, "base")
    command.upgrade(config, "head")

    engine = create_engine(test_database_url)
    with engine.connect() as conn:
        extensions = set(conn.execute(text("SELECT extname FROM pg_extension")).scalars())
        assert {"vector", "pg_trgm", "unaccent"} <= extensions
    engine.dispose()


def test_models_match_migrations(test_database_url):
    import_all_models()
    engine = create_engine(test_database_url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    engine.dispose()
    assert diff == []
