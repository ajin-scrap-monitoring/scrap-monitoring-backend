"""Reset a DB account password through a hidden terminal prompt."""

import argparse
import asyncio
from getpass import getpass

from sqlalchemy.exc import SQLAlchemyError

from src.app.core.passwords import validate_new_password
from src.app.db.session import close_db, get_sessionmaker
from src.app.services.session_service import session_service


async def reset(username: str, password: str) -> bool:
    try:
        async with get_sessionmaker()() as db:
            return await session_service.reset_password(db, username, password)
    finally:
        await close_db()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reset an existing account password and revoke sessions"
    )
    parser.add_argument("command", choices=["reset-password"])
    parser.add_argument("username")
    args = parser.parse_args()
    password = getpass("New password: ")
    confirmation = getpass("Confirm password: ")
    if password != confirmation:
        parser.exit(1, "Passwords do not match.\n")
    try:
        validate_new_password(password)
        found = asyncio.run(reset(args.username, password))
    except ValueError:
        parser.exit(1, "Password must contain between 12 and 1024 characters.\n")
    except SQLAlchemyError:
        parser.exit(1, "Account storage unavailable; reset not completed.\n")
    if not found:
        parser.exit(1, "Account not found.\n")
    print("Password updated; previous sessions revoked.")


if __name__ == "__main__":
    main()
