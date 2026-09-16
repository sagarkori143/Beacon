"""Shapes the public site may see.

Deliberately minimal. A visitor gets an organization's name and slug and nothing
else -- not its settings, not its locations, not how many documents it holds.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel


class PublicOrganizationOut(BaseModel):
    id: UUID
    name: str
    slug: str
