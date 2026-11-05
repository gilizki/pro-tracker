"""Web Push delivery (thin wrapper around pywebpush).

Keys come from environment variables and are never stored in the repository:
  VAPID_PUBLIC_KEY   base64url, given to browsers when they subscribe
  VAPID_PRIVATE_KEY  base64url (raw 32 bytes), signs every push
  VAPID_SUBJECT      contact for the push services, e.g. https://pro-tracker-rkcu.onrender.com
If any of them is missing, sending is disabled and the enable button is not shown.
Generate a pair with:  python tools/generate_vapid_keys.py
"""
import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

SENT = "sent"
GONE = "gone"      # the subscription expired or is invalid: delete it
FAILED = "failed"  # temporary or unknown error: keep it

GONE_STATUS_CODES = (404, 410)
REQUEST_TIMEOUT_SECONDS = 10


class WebPushSender:
    @staticmethod
    def _config() -> dict:
        return {name: os.getenv(name, "").strip() for name in ("VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_SUBJECT")}

    @property
    def enabled(self) -> bool:
        return all(self._config().values())

    @property
    def public_key(self) -> Optional[str]:
        return self._config()["VAPID_PUBLIC_KEY"] if self.enabled else None

    def send(self, endpoint: str, p256dh: str, auth: str, payload: dict, ttl: int) -> str:
        config = self._config()
        if not all(config.values()):
            return FAILED
        from pywebpush import WebPushException, webpush  # imported lazily: only needed when sending

        try:
            webpush(
                subscription_info={"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}},
                data=json.dumps(payload, ensure_ascii=False),
                vapid_private_key=config["VAPID_PRIVATE_KEY"],
                vapid_claims={"sub": config["VAPID_SUBJECT"]},  # a fresh dict each time: pywebpush adds "aud" to it
                ttl=ttl,
                timeout=REQUEST_TIMEOUT_SECONDS,
                headers={"Urgency": "normal"},
            )
            return SENT
        except WebPushException as error:
            status = getattr(error.response, "status_code", None)
            if status in GONE_STATUS_CODES:
                return GONE
            logger.warning("Web push failed (status %s)", status)
            return FAILED
        except Exception:  # network errors, bad keys...: never break the caller
            logger.exception("Web push failed")
            return FAILED
