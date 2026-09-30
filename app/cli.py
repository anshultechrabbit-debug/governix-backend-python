"""Operational commands.

    python -m app.cli create-master-admin --email admin@example.com --name "Platform Admin"

The password is read from GOVERNIX_ADMIN_PASSWORD or prompted for; it is never
accepted as a command-line argument (shell history, process listings).
"""

import argparse
import getpass
import os
import sys

from app.core.config import get_settings
from app.core.database import create_db_engine, create_session_factory
from app.core.exceptions import AppError
from app.core.security import MIN_PASSWORD_LENGTH


def create_master_admin(args: argparse.Namespace) -> None:
    from app.core.model_registry import import_all_models
    from app.modules.users.service import create_master_admin as create

    import_all_models()
    password = os.environ.get("GOVERNIX_ADMIN_PASSWORD") or getpass.getpass("Password: ")
    if len(password) < MIN_PASSWORD_LENGTH:
        sys.exit(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    engine = create_db_engine(get_settings())
    try:
        with create_session_factory(engine)() as session:
            user = create(session, args.email, args.name, password)
        print(f"Created master admin {user.email} ({user.id})")
    except AppError as exc:
        sys.exit(exc.message)
    finally:
        engine.dispose()


def seed_db(args: argparse.Namespace) -> None:
    from seed import main as seed_main

    seed_main()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    admin = commands.add_parser("create-master-admin", help="Bootstrap the platform administrator")
    admin.add_argument("--email", required=True)
    admin.add_argument("--name", required=True)
    admin.set_defaults(handler=create_master_admin)

    seed = commands.add_parser("seed", help="Seed the database with test organizations, branches, and users")
    seed.set_defaults(handler=seed_db)

    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
