from typing import Literal

from pydantic import BaseModel

Tier = Literal["tier_1", "tier_2"]


class LibraryCreate(BaseModel):
    name: str
    tier: Tier = "tier_1"


class LibraryRead(BaseModel):
    id: int
    name: str
    tier: str
