import secrets
from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPBearer,
)

from app.core.config import settings


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
    credentials: HTTPBasicCredentials | None = Depends(basic_security),
) -> APIPrincipal:
    principal = _admin(credentials)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return principal


def require_api_access(
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
    return principal
