import hashlib
import secrets


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def generate_scoped_token(scope: str, identity: int, *, bytes_count: int = 32) -> str:
    return f"{scope}_{identity}.{secrets.token_urlsafe(bytes_count)}"
