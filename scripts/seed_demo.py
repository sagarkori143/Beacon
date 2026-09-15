"""Seed the Sagar Hotels demo tenant.

Creates the scenario the whole design exists to serve: one organization with
knowledge shared across every property, plus per-location documents that
override it on specific subjects.

    Sagar Hotels
      ├── Ginza    -- breakfast until 11:00, later checkout
      ├── Chiyoda  -- inherits the group defaults
      └── Meguro   -- has its own pet policy

The interesting assertion afterwards is not that a Ginza guest gets 11:00. It is
that a Chiyoda guest gets 10:00 from the *same* organization document, without
that document being copied three times -- and that neither can reach the other's
knowledge at all.

Run:  python -m scripts.seed_demo          (add --reset to start clean)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text

from app.core.config import Settings, get_settings
from app.core.db import UnitOfWork, dispose_engine, init_engine, unscoped_session
from app.core.enums import Role
from app.core.logging import configure_logging, get_logger
from app.core.redis import close_redis, init_redis
from app.core.security import hash_password
from app.core.tenancy import TenantContext, system_principal
from app.core.tracing import TraceContext
from app.providers.registry import build_providers
from app.repositories import organization as org_repo
from app.repositories import user as user_repo
from app.services.documents.service import DocumentService
from app.services.ingestion.pipeline import IngestionPipeline

log = get_logger(__name__)

ORG_SLUG = "sagar-hotels"
ADMIN_EMAIL = "admin@sagarhotels.example"
#: Demo credentials for a local tenant. Not a secret, and not used anywhere
#: outside this script.
DEMO_PASSWORD = "demo-password-12345"  # noqa: S105


@dataclass(frozen=True, slots=True)
class SeedDocument:
    title: str
    location_slug: str | None
    document_type: str
    body: str


# --- Organization-wide knowledge --------------------------------------------
# Stored once. Every location retrieves it; none of them holds a copy.

ORG_HANDBOOK = SeedDocument(
    title="Sagar Hotels Guest Handbook",
    location_slug=None,
    document_type="policy",
    body="""# Sagar Hotels Guest Handbook

## Breakfast Service

Breakfast is served in the main dining room from 7:00 AM to 10:00 AM every day,
including weekends and public holidays. The buffet includes Japanese and Western
options. Guests on club floors may request in-room breakfast at no extra charge
by calling the front desk before 9:00 PM the previous evening.

Children under six eat free. Children aged six to twelve are charged half price.

## Check-in and Check-out

Check-in begins at 3:00 PM. Check-out is at 11:00 AM. Late check-out until
2:00 PM may be arranged for 3,000 JPY, subject to availability on the day.

Luggage storage is available at the front desk before check-in and after
check-out at no charge.

## Cancellation Policy

Reservations may be cancelled free of charge up to 48 hours before the check-in
date. Cancellations within 48 hours are charged one night's room rate.
No-shows are charged the full value of the reservation.

Non-refundable rates, where booked, cannot be cancelled or amended.

## Pet Policy

Sagar Hotels does not accept pets, with the exception of assistance dogs, which
are welcome in all areas of every property at no charge.

## Smoking Policy

All guest rooms and indoor public areas are non-smoking. A cleaning fee of
50,000 JPY applies to smoking in a guest room. Designated outdoor smoking areas
are available at every property.

## Wi-Fi and Business Services

Wi-Fi is free throughout every property. The network name is Sagar-Guest and the
password is printed on your key card holder. Printing and scanning are available
at the front desk.
""",
)

# --- Location-specific knowledge --------------------------------------------
# Only what differs. "Breakfast Hours" overrides the organization's "Breakfast
# Service" because both reduce to the same topic key.

GINZA_SUPPLEMENT = SeedDocument(
    title="Sagar Ginza Property Guide",
    location_slug="ginza",
    document_type="policy",
    body="""# Sagar Ginza Property Guide

## Breakfast Hours

At Sagar Ginza, breakfast is served from 7:00 AM to 11:00 AM in the Ginza Grill
on the second floor. The extended hours apply every day of the week.

A Japanese breakfast set is available to order from the same counter until
10:30 AM.

## Late Check-out

Sagar Ginza offers complimentary late check-out until 1:00 PM for guests staying
three nights or more. Otherwise late check-out until 2:00 PM is 3,000 JPY.

## Parking

Valet parking is available for 4,000 JPY per night. The entrance is on the
Namiki-dori side of the building. Vehicle height is limited to 2.1 metres.

## Fitness Centre

The fitness centre on the fifth floor is open 24 hours to all guests. Towels and
water are provided. The swimming pool is open from 6:00 AM to 10:00 PM.
""",
)

MEGURO_SUPPLEMENT = SeedDocument(
    title="Sagar Meguro Property Guide",
    location_slug="meguro",
    document_type="policy",
    body="""# Sagar Meguro Property Guide

## Pet Policy

Sagar Meguro is our pet-friendly property. Dogs and cats up to 10 kg are welcome
in designated rooms on floors two and three for a cleaning fee of 3,000 JPY per
stay. Pets must not be left unattended in guest rooms and must be leashed in all
public areas.

A pet-sitting service can be arranged through the concierge with 24 hours notice.

## Garden Terrace

The garden terrace on the ground floor is open from 8:00 AM to 9:00 PM and is
available to all guests. Breakfast may be taken on the terrace in fine weather.
""",
)

SEED_DOCUMENTS = (ORG_HANDBOOK, GINZA_SUPPLEMENT, MEGURO_SUPPLEMENT)

LOCATIONS = (
    (
        "Sagar Ginza",
        "ginza",
        "Asia/Tokyo",
        {
            "address": "6-10-1 Ginza, Chuo-ku, Tokyo",
            "phone": "+81-3-5555-0101",
            "front_desk_hours": "24 hours",
            "amenities": ["fitness centre", "pool", "valet parking", "Ginza Grill"],
        },
    ),
    (
        "Sagar Chiyoda",
        "chiyoda",
        "Asia/Tokyo",
        {
            "address": "1-1-1 Marunouchi, Chiyoda-ku, Tokyo",
            "phone": "+81-3-5555-0202",
            "front_desk_hours": "24 hours",
            "amenities": ["business centre", "meeting rooms"],
        },
    ),
    (
        "Sagar Meguro",
        "meguro",
        "Asia/Tokyo",
        {
            "address": "2-2-2 Kamiosaki, Shinagawa-ku, Tokyo",
            "phone": "+81-3-5555-0303",
            "front_desk_hours": "07:00-23:00",
            "amenities": ["garden terrace", "pet friendly"],
        },
    ),
)


async def seed(settings: Settings, *, reset: bool = False) -> None:
    redis = init_redis(settings)
    providers = build_providers(settings, redis=redis)

    if reset:
        await _reset(settings)

    organization_id = await _create_organization(settings)
    tenant = TenantContext(organization_id=organization_id)
    uow = UnitOfWork(tenant, settings)

    location_ids = await _create_locations(uow, tenant)
    admin_id = await _create_users(uow, tenant, location_ids)

    admin = system_principal(organization_id, admin_id)
    service = DocumentService(settings, providers)
    pipeline = IngestionPipeline(settings=settings, providers=providers)

    for document in SEED_DOCUMENTS:
        location_id = location_ids.get(document.location_slug or "")
        result = await service.upload(
            uow,
            admin,
            data=document.body.encode("utf-8"),
            filename=f"{document.title}.md",
            content_type="text/markdown",
            title=document.title,
            location_id=location_id,
            document_type=document.document_type,
            trace=TraceContext.new(organization_id=organization_id),
            # This script runs the pipeline itself, so the job is recorded but
            # not published -- otherwise a running worker would process the same
            # version at the same time.
            enqueue=False,
        )
        # Inline, so the demo works with or without a worker running. In normal
        # operation the queue message drives exactly this code.
        await pipeline.run(
            uow,
            result.job_id,
            tenant=tenant,
            trace=TraceContext.new(organization_id=organization_id),
            worker_id="seed",
        )
        scope = document.location_slug or "organization-wide"
        print(f"  indexed {document.title!r} ({scope})")

    await providers.aclose()
    await close_redis()
    # Dispose inside this loop: closing an asyncpg connection from a *different*
    # event loop leaves its transport already torn down and raises on Windows.
    await dispose_engine()
    _print_summary(location_ids)


async def _reset(settings: Settings) -> None:
    """Delete the demo organization. Cascades take everything with it."""
    async with unscoped_session(settings) as session:
        await session.execute(
            text("DELETE FROM organizations WHERE slug = :slug"), {"slug": ORG_SLUG}
        )
    print("  reset: removed existing demo organization")


async def _create_organization(settings: Settings) -> UUID:
    """Create the tenant root.

    Runs unscoped because there is no tenant yet -- `organizations` has RLS
    enabled but not forced, so the owner role may provision tenants while the
    application role still only ever sees its own.
    """
    async with unscoped_session(settings) as session:
        existing = await org_repo.get_organization_by_slug(session, ORG_SLUG)
        if existing is not None:
            print(f"  organization {ORG_SLUG!r} already exists")
            return existing.id

        organization = await org_repo.create_organization(
            session,
            name="Sagar Hotels",
            slug=ORG_SLUG,
            settings={
                # Demonstrates the highest-priority routing rule: pin the
                # planning step to a fast local model regardless of what else
                # is configured.
                "model_pins": {},
                "allowed_providers": None,
            },
        )
        print(f"  created organization {organization.name!r}")
        return organization.id


async def _create_locations(uow: UnitOfWork, tenant: TenantContext) -> dict[str, UUID]:
    ids: dict[str, UUID] = {}
    async with uow.begin() as session:
        for name, slug, timezone, location_settings in LOCATIONS:
            existing = await org_repo.get_location_by_slug(session, tenant, slug)
            if existing is not None:
                ids[slug] = existing.id
                continue
            location = await org_repo.create_location(
                session,
                tenant,
                name=name,
                slug=slug,
                timezone=timezone,
                settings=location_settings,
            )
            ids[slug] = location.id
            print(f"  created location {name!r}")
    return ids


async def _create_users(
    uow: UnitOfWork, tenant: TenantContext, location_ids: dict[str, UUID]
) -> UUID:
    """Create one admin plus one guest-facing user per location.

    The per-location users are what make the isolation demonstrable: each one's
    token pins them to a property, and no request they make can reach another's
    documents.
    """
    accounts = [
        (ADMIN_EMAIL, "Sagar Admin", Role.ADMIN, None),
        ("ginza@sagarhotels.example", "Ginza Front Desk", Role.USER, location_ids.get("ginza")),
        (
            "chiyoda@sagarhotels.example",
            "Chiyoda Front Desk",
            Role.USER,
            location_ids.get("chiyoda"),
        ),
        ("meguro@sagarhotels.example", "Meguro Front Desk", Role.USER, location_ids.get("meguro")),
    ]

    admin_id: UUID | None = None
    async with uow.begin() as session:
        for email, full_name, role, location_id in accounts:
            existing = await user_repo.get_user_by_email(session, tenant, email)
            if existing is not None:
                if role is Role.ADMIN:
                    admin_id = existing.id
                continue
            user = await user_repo.create_user(
                session,
                tenant,
                email=email,
                password_hash=hash_password(DEMO_PASSWORD),
                role=role,
                location_id=location_id,
                full_name=full_name,
            )
            if role is Role.ADMIN:
                admin_id = user.id
            print(f"  created user {email}")

    assert admin_id is not None
    return admin_id


def _print_summary(location_ids: dict[str, UUID]) -> None:
    print(
        f"""
Demo tenant ready.

  Accounts (password: {DEMO_PASSWORD})
    {ADMIN_EMAIL:36} ADMIN, organization-wide
    ginza@sagarhotels.example            USER, Sagar Ginza
    chiyoda@sagarhotels.example          USER, Sagar Chiyoda
    meguro@sagarhotels.example           USER, Sagar Meguro

  Try this, in order:

    1. Ask the Ginza user "What time is breakfast?"      -> 7:00 to 11:00
    2. Ask the Chiyoda user the same question            -> 7:00 to 10:00
    3. Ask the Chiyoda user "Can I bring my dog?"        -> no (org policy),
       with no sign of Meguro's pet-friendly rooms.

  Step 2 reads the organization document that step 1 overrode. One stored copy,
  three different correct answers.
"""
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="delete the demo organization first")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging("WARNING", "console")
    init_engine(settings)

    asyncio.run(seed(settings, reset=args.reset))
    return 0


if __name__ == "__main__":
    sys.exit(main())
