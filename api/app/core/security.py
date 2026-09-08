import secrets
import base64
import hashlib
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPBearer,
)

from app.core.config import settings


PASSWORD_ITERATIONS = 600_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(digest).decode()}"


def verify_password(password: str, encoded: str | None) -> bool:
    if not encoded:
        return False
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), base64.urlsafe_b64decode(salt), int(iterations)
        )
        return secrets.compare_digest(actual, base64.urlsafe_b64decode(expected))
    except (ValueError, TypeError):
        return False


basic_security = HTTPBasic(auto_error=False)
bearer_security = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class APIPrincipal:
    name: str
    kind: str


def _same(left: str, right: str) -> bool:
    return secrets.compare_digest(left.encode(), right.encode())


def _admin(credentials: HTTPBasicCredentials | None) -> APIPrincipal | None:
    if credentials is None:
        return None
    if _same(credentials.username, settings.admin_username) and _same(
        credentials.password,
        settings.admin_password,
    ):
        return APIPrincipal(name=credentials.username, kind="admin")
    return None


def _service(credentials: HTTPAuthorizationCredentials | None) -> APIPrincipal | None:
    if credentials is None or credentials.scheme.lower() != "bearer":
        return None
    if settings.service_api_token and _same(
        credentials.credentials,
        settings.service_api_token,
    ):
        return APIPrincipal(name="telegram-bot", kind="service")
    return None


def require_admin(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(basic_security),
) -> APIPrincipal:
    principal = _admin(credentials)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    request.state.principal = principal
    return principal


def require_api_access(
    request: Request,
    basic: HTTPBasicCredentials | None = Depends(basic_security),
    bearer: HTTPAuthorizationCredentials | None = Depends(bearer_security),
) -> APIPrincipal:
    principal = _admin(basic) or _service(bearer)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API authentication required",
            headers={"WWW-Authenticate": 'Basic realm="vpn-api", Bearer'},
        )
    request.state.principal = principal
    return principal
