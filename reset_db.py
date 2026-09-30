"""Reset database script.

Deletes all data from the database, cleans storage files,
and seeds only the single superadmin user requested:
    Email: admin@gmail.com
    Password: Admin@112
    Role: master_admin
"""

import shutil
from pathlib import Path
from sqlalchemy import text
from app.core.config import get_settings
from app.core.database import create_db_engine, create_session_factory
from app.core.model_registry import import_all_models
from app.core.security import hash_password
from app.modules.auth.permissions import Role
from app.modules.users.model import User


def reset_database():
    import_all_models()
    settings = get_settings()
    engine = create_db_engine(settings)
    session_factory = create_session_factory(engine)

    print("1. Truncating all database tables...")
    tables_to_truncate = [
        "document_pages",
        "document_sections",
        "document_analyses",
        "policies",
        "policy_versions",
        "queue_jobs",
        "audit_events",
        "refresh_tokens",
        "departments",
        "users",
        "categories",
        "ingestion_stages",
        "document_relationships",
        "embedding_cache",
        "documents",
        "chunks",
        "branches",
        "upload_batches",
        "upload_batch_groups",
        "notifications",
        "upload_batch_items",
        "policy_assignments",
        "tickets",
        "ticket_messages",
        "ticket_attachments",
        "organizations",
    ]

    with engine.begin() as conn:
        truncate_sql = f"TRUNCATE TABLE {', '.join(tables_to_truncate)} RESTART IDENTITY CASCADE;"
        conn.execute(text(truncate_sql))
    print("✓ All application tables truncated successfully.")

    # 2. Clean storage directory
    print("2. Cleaning local storage files...")
    storage_path = Path(settings.LOCAL_STORAGE_PATH)
    if storage_path.exists():
        for sub_dir in ["originals", "extracted", "ocr", "pages", "temporary"]:
            target = storage_path / sub_dir
            if target.exists():
                for item in target.iterdir():
                    if item.is_file():
                        item.unlink()
                    elif item.is_dir():
                        shutil.rmtree(item)
    print("✓ Storage directories cleaned.")

    # 3. Create superadmin
    print("3. Creating superadmin user...")
    admin_email = "admin@gmail.com"
    admin_password = "Admin@112"

    with session_factory() as session:
        superadmin = User(
            email=admin_email,
            full_name="Super Admin",
            password_hash=hash_password(admin_password),
            role=Role.MASTER_ADMIN,
            is_active=True,
            organization_id=None,
            branch_id=None,
            department_id=None,
        )
        session.add(superadmin)
        session.commit()
        session.refresh(superadmin)
        print(f"✓ Created Superadmin User: ID={superadmin.id}")

    print("\n" + "=" * 50)
    print("DATABASE RESET COMPLETE!")
    print("=" * 50)
    print("Credentials:")
    print(f"  Role:     Master Admin (Superadmin)")
    print(f"  Email:    {admin_email}")
    print(f"  Password: {admin_password}")
    print("=" * 50)


if __name__ == "__main__":
    reset_database()
