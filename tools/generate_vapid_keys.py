"""Prints a new VAPID key pair for Web Push. Writes no files.

Usage (from the project folder, with the venv's Python):
    python tools/generate_vapid_keys.py

Copy the printed values into environment variables (locally and on Render).
Never commit them. Generate them ONCE and keep them: new keys invalidate every
existing subscription, and users would have to tap "הפעל התראות" again.
"""
import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def main() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    private_raw = key.private_numbers().private_value.to_bytes(32, "big")
    public_raw = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    print(f"VAPID_PUBLIC_KEY={b64url(public_raw)}")
    print(f"VAPID_PRIVATE_KEY={b64url(private_raw)}")
    print("VAPID_SUBJECT=https://pro-tracker-rkcu.onrender.com")


if __name__ == "__main__":
    main()
