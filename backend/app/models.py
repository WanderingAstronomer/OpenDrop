from typing import Literal, Optional

from pydantic import BaseModel, Field

OrgType = Literal[
    "charity_store", "thrift_store", "consignment", "drop_bin", "donation_center",
    "mutual_aid", "church_drive", "other",
]


class VoteIn(BaseModel):
    vote: Literal["confirm", "deny"]
    turnstile_token: Optional[str] = None


class ImageVoteIn(BaseModel):
    vote: Literal["helpful", "unhelpful"]
    turnstile_token: Optional[str] = None


class ImageDeleteIn(BaseModel):
    """Turnstile-only body for deleting one's own still-unverified photo (DELETE-with-body)."""
    turnstile_token: Optional[str] = None


class AddressIn(BaseModel):
    # Length caps matter for more than tidiness: these fields are fed to the content-screen regexes
    # (moderation._scan), and an unbounded value is a ReDoS lever. Bound them at the API boundary.
    line: Optional[str] = Field(default=None, max_length=200)
    city: Optional[str] = Field(default=None, max_length=120)
    state: Optional[str] = Field(default=None, max_length=2)
    postal_code: Optional[str] = Field(default=None, max_length=20)


class SubmitIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    org_type: OrgType = "other"
    address: AddressIn
    # Drop-a-pin: when both are present the pin is authoritative and geocoding is skipped
    # (the address is back-filled by reverse geocoding). Only one of the two => ignored.
    lat: Optional[float] = Field(default=None, ge=-90, le=90)
    lon: Optional[float] = Field(default=None, ge=-180, le=180)
    turnstile_token: Optional[str] = None


class CorrectionIn(BaseModel):
    """A proposed pin move (photo optional). gps_corroborated is a CLIENT-computed boolean —
    'I am standing within the radius' — never coordinates; it only adds consensus weight."""
    suggested_lat: float = Field(ge=-90, le=90)
    suggested_lon: float = Field(ge=-180, le=180)
    note: Optional[str] = Field(default=None, max_length=500)
    image_id: Optional[int] = None
    gps_corroborated: bool = False
    turnstile_token: Optional[str] = None


class CorrectionVoteIn(BaseModel):
    confirm: bool
    gps_corroborated: bool = False
    turnstile_token: Optional[str] = None


class RatingItem(BaseModel):
    """One entry of a batched rating submission. value=None retracts the caller's own rating."""
    attribute: Literal["safety", "condition", "bins"]
    value: Optional[int] = Field(default=None, ge=1, le=50)


class AttributeIn(BaseModel):
    # safety/condition are 1..3 scales (poor/ok/good); bins is a count estimate (1..50).
    # The per-attribute upper bound is enforced in the router.
    #
    # Two accepted shapes, ONE Turnstile token either way:
    #   legacy single:  {attribute, value}
    #   batched form:   {ratings: [{attribute, value|null}, ...]}   (the rate-form's single Save —
    #                   one API call instead of one per tap; null value = retract that rating)
    attribute: Optional[Literal["safety", "condition", "bins"]] = None
    value: Optional[int] = Field(default=None, ge=1, le=50)
    ratings: Optional[list[RatingItem]] = Field(default=None, max_length=3)
    turnstile_token: Optional[str] = None


class AttributeClearIn(BaseModel):
    """Retract the caller's own rating for one attribute (rating deselect)."""
    turnstile_token: Optional[str] = None


# Crowd field corrections — propose a better name / type / org / address. Same engagement-tiered
# consensus as pin corrections (community.py / migration 0009), but GPS weighting is meaningless
# for a text field, so every participant counts as a flat 1.
FieldName = Literal["name", "org_type", "org_name", "address"]


class FieldCorrectionIn(BaseModel):
    field: FieldName
    # For name/org_name: the new text. For org_type: a valid OrgType key. For address: use `address`.
    value: Optional[str] = Field(default=None, max_length=200)
    address: Optional[AddressIn] = None
    note: Optional[str] = Field(default=None, max_length=500)
    turnstile_token: Optional[str] = None


class FieldCorrectionVoteIn(BaseModel):
    confirm: bool
    turnstile_token: Optional[str] = None


# --- Public reporting + operator moderation ---

class ReportIn(BaseModel):
    """Public 'this looks wrong/abusive' flag. Does NOT auto-remove a location (anti-grief); it
    files a content_reports row for an operator, and (for images) hides the photo from the default
    gallery pending review. reason is free text; it is screened like any other crowd text."""
    reason: Optional[str] = Field(default=None, max_length=500)
    turnstile_token: Optional[str] = None


class TakedownIn(BaseModel):
    """Operator hard-moderation action (hide a location / remove a photo)."""
    reason: Optional[str] = Field(default=None, max_length=500)


class ResolveReportIn(BaseModel):
    note: Optional[str] = Field(default=None, max_length=500)


class RevertActorIn(BaseModel):
    """Bulk-revert every still-applied correction authored by one actor (by submitter ip-hash)."""
    actor_ip_hash: str = Field(min_length=8, max_length=128)
    note: Optional[str] = Field(default=None, max_length=500)
