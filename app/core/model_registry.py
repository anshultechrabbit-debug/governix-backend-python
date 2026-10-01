"""Imports every module that defines ORM models so Base.metadata is complete.

Alembic autogenerate and the test schema both rely on this. Add each new
module's models here.
"""


def import_all_models() -> None:
    import app.infrastructure.queue.models  # noqa: F401
    import app.modules.ai_usage.model  # noqa: F401
    import app.modules.assignments.model  # noqa: F401
    import app.modules.audit.model  # noqa: F401
    import app.modules.auth.model  # noqa: F401
    import app.modules.branches.model  # noqa: F401
    import app.modules.categories.model  # noqa: F401
    import app.modules.departments.model  # noqa: F401
    import app.modules.documents.model  # noqa: F401
    import app.modules.ingestion.model  # noqa: F401
    import app.modules.notifications.model  # noqa: F401
    import app.modules.organizations.model  # noqa: F401
    import app.modules.policies.model  # noqa: F401
    import app.modules.rag.model  # noqa: F401
    import app.modules.search.model  # noqa: F401
    import app.modules.tickets.model  # noqa: F401
    import app.modules.uploads.model  # noqa: F401
    import app.modules.users.model  # noqa: F401
