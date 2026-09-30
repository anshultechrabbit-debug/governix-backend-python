"""Delete every document and policy, and everything that belongs to them.

Deleted: documents (pages, sections, analyses, relationships, chunks, ingestion
progress), policies (versions, assignments), bulk uploads, the embedding cache,
their pipeline jobs, notifications linking to them, and the stored files.

Kept: organisations, users, branches, departments, categories, tickets and the
audit log. (reset_db.py, by contrast, wipes everything.)

Runs in one transaction: if any step fails, nothing is deleted.
"""

import shutil
from pathlib import Path

from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import create_db_engine
from app.core.model_registry import import_all_models

# Children before parents, so no row is ever left pointing at a deleted one.
STEPS = [
    ("pipeline jobs", """DELETE FROM queue_jobs WHERE task_name LIKE 'ingestion.%'
        OR payload ?| array['document_id', 'policy_id', 'version_id', 'batch_id', 'group_id', 'item_id']"""),
    ("notifications about documents", """DELETE FROM notifications
        WHERE link LIKE '/documents%' OR link LIKE '/policies%' OR link LIKE '/uploads%'
        OR data ?| array['document_id', 'policy_id', 'version_id', 'batch_id']"""),
    ("upload batch items", "DELETE FROM upload_batch_items"),
    ("upload batch groups", "DELETE FROM upload_batch_groups"),
    ("upload batches", "DELETE FROM upload_batches"),
    ("policy assignments", "DELETE FROM policy_assignments"),
    ("document relationships", "DELETE FROM document_relationships"),
    ("chunks", "DELETE FROM chunks"),
    ("embedding cache", "DELETE FROM embedding_cache"),
    ("document analyses", "DELETE FROM document_analyses"),
    ("ingestion stages", "DELETE FROM ingestion_stages"),
    ("document sections", "DELETE FROM document_sections"),
    ("document pages", "DELETE FROM document_pages"),
    # Documents and versions point at each other: unlink first.
    ("unlink documents from versions", "UPDATE documents SET policy_version_id = NULL, policy_id = NULL, duplicate_of_id = NULL"),
    ("policy versions", "UPDATE policy_versions SET supersedes_version_id = NULL; DELETE FROM policy_versions"),
    ("documents", "DELETE FROM documents"),
    ("policies", "DELETE FROM policies"),
]
STORAGE_DIRS = ["originals", "extracted", "ocr", "pages", "temporary"]


def delete_documents() -> None:
    import_all_models()
    settings = get_settings()
    engine = create_db_engine(settings)
    with engine.begin() as conn:
        for label, sql in STEPS:
            count = sum(conn.execute(text(statement)).rowcount for statement in sql.split(";") if statement.strip())
            print(f"  {label:32} {count:>8}")
    print("Database: done.")

    storage = Path(settings.LOCAL_STORAGE_PATH)
    removed = 0
    for name in STORAGE_DIRS:
        folder = storage / name
        for item in folder.iterdir() if folder.exists() else []:
            shutil.rmtree(item) if item.is_dir() else item.unlink()
            removed += 1
    print(f"Storage: removed {removed} entries under {storage.resolve()}.")


if __name__ == "__main__":
    delete_documents()
