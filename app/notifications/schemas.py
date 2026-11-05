"""HTTP DTOs for push subscriptions."""
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

# The server sends HTTP requests to the subscription endpoint, so only real push
# services are accepted (prevents using the server to call arbitrary URLs).
ALLOWED_PUSH_HOSTS = (
    "fcm.googleapis.com",          # Chrome, Android, Edge on Android
    "push.services.mozilla.com",   # Firefox
    "push.apple.com",              # Safari / iPhone (web.push.apple.com)
    "notify.windows.com",          # Edge on Windows
)
BASE64URL = r"^[A-Za-z0-9_-]+={0,2}$"


def validate_endpoint(endpoint: str) -> str:
    parts = urlsplit(endpoint)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.port not in (None, 443) or parts.username or parts.password:
        raise ValueError("Unsupported push endpoint")
    if not any(host == allowed or host.endswith("." + allowed) for allowed in ALLOWED_PUSH_HOSTS):
        raise ValueError("Unsupported push service")
    return endpoint


class SubscriptionKeys(BaseModel):
    p256dh: str = Field(min_length=1, max_length=256, pattern=BASE64URL)
    auth: str = Field(min_length=1, max_length=64, pattern=BASE64URL)


class SubscriptionCreate(BaseModel):
    """The browser's ``PushSubscription.toJSON()`` (extra fields are ignored)."""
    endpoint: str = Field(min_length=1, max_length=1024)
    keys: SubscriptionKeys

    _endpoint = field_validator("endpoint")(validate_endpoint)


class SubscriptionRemove(BaseModel):
    endpoint: str = Field(min_length=1, max_length=1024)
