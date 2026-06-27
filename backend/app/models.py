from typing import Literal, Optional

from pydantic import BaseModel, Field

OrgType = Literal[
    "charity_store", "thrift_store", "drop_bin", "donation_center",
    "mutual_aid", "church_drive", "other",
]


class VoteIn(BaseModel):
    vote: Literal["confirm", "deny"]
    turnstile_token: Optional[str] = None


class AddressIn(BaseModel):
    line: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = Field(default=None, max_length=2)
    postal_code: Optional[str] = None


class SubmitIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    org_type: OrgType = "other"
    address: AddressIn
    turnstile_token: Optional[str] = None
