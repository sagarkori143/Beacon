"""Create the first platform operator.

Deliberately a local command rather than an endpoint. An API that mints the
first all-powerful account is an open door until somebody remembers to close it,
and "remember to disable the bootstrap route after deploying" is not a security
control. Running this requires shell access to the deployment, which is the
right bar.

Subsequent operators are created through ``POST /platform/operators``.

    python -m scripts.create_owner --email you@example.com
    python -m scripts.create_owner --email you@example.com --password '...'

With no --password, one is generated and printed once.
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.db import dispose_engine, init_engine, unscoped_session
from app.core.errors import ConflictError
from app.core.logging import configure_logging
from app.core.security import normalize_email
from app.models.platform import PlatformUser
from app.services.platform.service import PlatformService

MIN_PASSWORD_LENGTH = 12


async def create(settings: Settings, email: str, password: str | None, name: str | None) -> int:
    generated = password is None
    secret = password or secrets.token_urlsafe(18)

    if len(secret) < MIN_PASSWORD_LENGTH:
        print(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.", file=sys.stderr)
        return 2

    try:
        operator = await PlatformService(settings).create_operator(
            email=email, password=secret, full_name=name
        )
    except ConflictError as exc:
        print(f"{exc.message}", file=sys.stderr)
        return 1

    print(
        f"""
Platform operator created.

  email     {operator.email}
  password  {secret if generated else "(as supplied)"}
  id        {operator.id}
"""
    )
    if generated:
        print("  Store the password now -- it is kept only as a hash.\n")

    print(
        """  Sign in:
    POST /api/v1/platform/auth/login   {"email": "...", "password": "..."}

  Then:
    POST /api/v1/platform/organizations                      create a tenant
    POST /api/v1/platform/organizations/{id}/locations       add a location
    POST /api/v1/platform/organizations/{id}/users           add an ADMIN or USER
"""
    )
    return 0


async def show_existing(settings: Settings) -> None:
    async with unscoped_session(settings) as session:
        result = await session.execute(select(PlatformUser).order_by(PlatformUser.email))
        operators = result.scalars().all()

    if not operators:
        print("No platform operators exist yet.")
        return
    print(f"{len(operators)} platform operator(s):")
    for operator in operators:
        state = "active" if operator.is_active else "disabled"
        print(f"  {operator.email:40} {state}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", help="operator's email address")
    parser.add_argument("--password", help="omit to generate one")
    parser.add_argument("--name", help="display name")
    parser.add_argument("--list", action="store_true", help="list existing operators")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging("WARNING", "console")
    init_engine(settings)

    async def run() -> int:
        try:
            if args.list:
                await show_existing(settings)
                return 0
            if not args.email:
                parser.error("--email is required (or use --list)")
            return await create(settings, normalize_email(args.email), args.password, args.name)
        finally:
            await dispose_engine()

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
