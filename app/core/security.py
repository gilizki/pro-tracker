"""Password and session-token primitives isolated from application services."""
import base64
import hashlib
import hmac
import secrets


class PasswordHasher:
    _iterations = 600_000

    @classmethod
    def hash(cls, password: str) -> str:
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, cls._iterations)
        return "pbkdf2_sha256${}${}${}".format(
            cls._iterations,
            base64.b64encode(salt).decode(),
            base64.b64encode(digest).decode(),
        )

    @classmethod
    def verify(cls, password: str, encoded: str) -> bool:
        try:
            algorithm, iterations, salt_text, digest_text = encoded.split("$", 3)
            if algorithm != "pbkdf2_sha256":
                return False
            digest = hashlib.pbkdf2_hmac(
                "sha256", password.encode(), base64.b64decode(salt_text), int(iterations)
            )
            return hmac.compare_digest(digest, base64.b64decode(digest_text))
        except (ValueError, TypeError):
            return False


class TokenGenerator:
    @staticmethod
    def create() -> str:
        return secrets.token_urlsafe(32)

    @staticmethod
    def digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()
