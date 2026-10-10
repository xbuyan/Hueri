#!/usr/bin/env python3
"""Create a TenderScout user, or reset a user's password.

Public registration is closed by default (ALLOW_REGISTRATION=false), so accounts
are created by an administrator with this script:

    python scripts/create_user.py someone@example.com --name "Full Name"
    python scripts/create_user.py someone@example.com --reset-password

The password is read from a hidden prompt, never from the command line, and must
be at least 8 characters. In Docker:

    docker compose -f docker-compose.prod.yml exec api \
        python scripts/create_user.py someone@example.com --name "Full Name"

The database must already be migrated (the api container does this on start).
"""
import argparse
import asyncio
import getpass
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from email_validator import EmailNotValidError, validate_email  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import db as app_db  # noqa: E402
from app.auth import get_password_hash  # noqa: E402
from app.models import User  # noqa: E402

MIN_PASSWORD_LENGTH = 8


class UserError(Exception):
    """A problem the administrator can fix (bad email, short password, duplicate...)."""


async def create_or_reset_user(email, full_name, password, reset):
    """Create the user, or with reset=True set a new password for an existing one.

    Returns a short message describing what was done; raises UserError otherwise.
    """
    try:
        email = validate_email(email, check_deliverability=False).normalized
    except EmailNotValidError as exc:
        raise UserError(f"Invalid email address: {exc}") from exc
    if len(password) < MIN_PASSWORD_LENGTH:
        raise UserError(f"The password must be at least {MIN_PASSWORD_LENGTH} characters.")

    async with app_db.async_session() as db:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()
        if reset:
            if user is None:
                raise UserError(f"No user with email {email}. Run again without --reset-password to create one.")
            user.hashed_password = get_password_hash(password)
            await db.commit()
            return f"Password updated for {email}."
        if user is not None:
            raise UserError(f"{email} already exists. Use --reset-password to change the password.")
        db.add(User(email=email, hashed_password=get_password_hash(password), full_name=full_name))
        await db.commit()
        return f"Created user {email}."


def main():
    parser = argparse.ArgumentParser(description="Create a TenderScout user or reset a password.")
    parser.add_argument("email")
    parser.add_argument("--name", dest="full_name", default=None, help="full name shown in the app")
    parser.add_argument("--reset-password", action="store_true", help="change the password of an existing user")
    args = parser.parse_args()

    password = getpass.getpass("Password: ")
    if getpass.getpass("Repeat password: ") != password:
        print("The passwords do not match.", file=sys.stderr)
        sys.exit(1)

    async def run():
        try:
            return await create_or_reset_user(args.email, args.full_name, password, args.reset_password)
        finally:
            await app_db.engine.dispose()

    try:
        print(asyncio.run(run()))
    except UserError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
