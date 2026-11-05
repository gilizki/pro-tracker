"""HTTP DTOs for the friends feature.

``FriendStatus`` is the ONLY shape in which another user's nutrition data ever
leaves the server.  It deliberately has no meals / foods / times fields, and it
is used as the endpoints' ``response_model`` so FastAPI strips anything extra.
"""
import datetime as dt
from typing import List, Optional

from pydantic import BaseModel, Field, model_validator


class FriendRequestCreate(BaseModel):
    """Exactly one of ``username`` or ``invite_code`` must be given."""
    username: Optional[str] = Field(default=None, min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    invite_code: Optional[str] = Field(default=None, min_length=1, max_length=32)

    @model_validator(mode="after")
    def exactly_one_target(self):
        if (self.username is None) == (self.invite_code is None):
            raise ValueError("Provide exactly one of username or invite_code")
        return self


class InviteCode(BaseModel):
    code: str


class FriendRequestResult(BaseModel):
    status: str  # "pending" or "accepted"
    username: str


class PendingRequest(BaseModel):
    id: int
    username: str


class PendingRequests(BaseModel):
    incoming: List[PendingRequest]
    outgoing: List[PendingRequest]


class FriendStatus(BaseModel):
    user_id: int
    username: str
    date: dt.date
    total_calories: float
    total_protein: float
