"""
Local authentication service, models, and API routes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from vmcp.config import settings
from vmcp.core.services import get_jwt_service
from vmcp.server.token_info import TokenInfo, normalize_token_info
from vmcp.storage.database import get_db
from vmcp.storage.models import User

ACCESS_COOKIE_NAME = "vmcp_access_token"
REFRESH_COOKIE_NAME = "vmcp_refresh_token"
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1

router = APIRouter(prefix="/api", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=8, max_length=256)


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=50)
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=8, max_length=256)
    full_name: Optional[str] = Field(default=None, max_length=200)


class AuthUserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    username: str
    first_name: str
    last_name: str
    full_name: str
    is_active: bool
    is_verified: bool
    last_login: Optional[str]
    created_at: str
    photo_url: Optional[str] = None


class LoginResponse(BaseModel):
    access_token: str
    refresh_token: Optional[str] = None
    token_type: str = "bearer"
    expires_in: int
    user: AuthUserResponse


class WebSocketTicketResponse(BaseModel):
    ticket: str
    expires_in: int


@dataclass
class AuthTokens:
    access_token: str
    refresh_token: str
    expires_in: int


def hash_password(password: str) -> str:
    """Hash a password using scrypt with a random salt."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return "scrypt${n}${r}${p}${salt}${digest}".format(
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        salt=base64.b64encode(salt).decode("ascii"),
        digest=base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, password_hash: Optional[str]) -> bool:
    """Verify a password against a stored scrypt hash."""
    if not password_hash:
        return False
    try:
        algorithm, n_value, r_value, p_value, salt_b64, digest_b64 = password_hash.split("$", 5)
        if algorithm != "scrypt":
            return False
        salt = base64.b64decode(salt_b64.encode("ascii"))
        expected_digest = base64.b64decode(digest_b64.encode("ascii"))
        calculated_digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n_value),
            r=int(r_value),
            p=int(p_value),
        )
        return hmac.compare_digest(calculated_digest, expected_digest)
    except (ValueError, TypeError):
        return False


def split_full_name(full_name: Optional[str]) -> tuple[str, str]:
    """Split a full name into first and last components."""
    normalized = (full_name or "").strip()
    if not normalized:
        return ("Local", "User")
    parts = normalized.split()
    if len(parts) == 1:
        return (parts[0], "")
    return (parts[0], " ".join(parts[1:]))


def user_to_response(user: User) -> AuthUserResponse:
    """Convert a user model into the frontend response shape."""
    return AuthUserResponse(
        id=str(user.id),
        email=user.email,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        full_name=user.full_name,
        is_active=bool(user.is_active),
        is_verified=bool(user.is_verified),
        last_login=user.last_login.isoformat() if user.last_login else None,
        created_at=user.created_at.isoformat(),
    )


def build_tokens(user: User) -> AuthTokens:
    """Create signed access and refresh tokens for a user."""
    jwt_service = get_jwt_service()
    access_token = jwt_service.create_token(
        user.id,
        username=user.username,
        email=user.email,
        session_nonce=user.session_nonce,
        token_type="access",
        expires_in=settings.access_token_ttl_seconds,
    )
    refresh_token = jwt_service.create_token(
        user.id,
        username=user.username,
        email=user.email,
        session_nonce=user.session_nonce,
        token_type="refresh",
        expires_in=settings.refresh_token_ttl_seconds,
    )
    return AuthTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.access_token_ttl_seconds,
    )


def set_auth_cookies(request: Request, response: Response, tokens: AuthTokens) -> None:
    """Set secure HttpOnly session cookies."""
    secure_cookie = settings.auth_cookie_secure or request.url.scheme == "https"
    common_kwargs = {
        "httponly": True,
        "secure": secure_cookie,
        "samesite": "lax",
        "domain": settings.auth_cookie_domain,
        "path": "/",
    }
    response.set_cookie(
        ACCESS_COOKIE_NAME,
        tokens.access_token,
        max_age=settings.access_token_ttl_seconds,
        **common_kwargs,
    )
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        tokens.refresh_token,
        max_age=settings.refresh_token_ttl_seconds,
        **common_kwargs,
    )


def clear_auth_cookies(request: Request, response: Response) -> None:
    """Expire auth cookies."""
    secure_cookie = settings.auth_cookie_secure or request.url.scheme == "https"
    response.delete_cookie(
        ACCESS_COOKIE_NAME,
        domain=settings.auth_cookie_domain,
        path="/",
        secure=secure_cookie,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(
        REFRESH_COOKIE_NAME,
        domain=settings.auth_cookie_domain,
        path="/",
        secure=secure_cookie,
        httponly=True,
        samesite="lax",
    )


def get_refresh_token_from_request(request: Request) -> Optional[str]:
    """Extract a refresh token from the request cookies."""
    return request.cookies.get(REFRESH_COOKIE_NAME)


def resolve_request_token(request: Request) -> Optional[str]:
    """Resolve an access-equivalent token from headers, cookies, or ws ticket query parameters."""
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header.replace("Bearer", "", 1).strip()

    access_cookie = request.cookies.get(ACCESS_COOKIE_NAME)
    if access_cookie:
        return access_cookie

    ticket = request.query_params.get("ticket")
    if ticket:
        return ticket

    return None


def get_normalized_token_info(jwt_service, token: str) -> TokenInfo:
    """Get normalized token info from a configured JWT service."""
    raw_info = jwt_service.extract_token_info(token)
    normalized = normalize_token_info(raw_info, token)
    if normalized is None:
        raise ValueError(f"Invalid or incomplete token info: {raw_info}")
    return normalized


def authenticate_credentials(db: Session, username: str, password: str) -> User:
    """Authenticate a local username/email and password combination."""
    normalized_username = username.strip().lower()
    user = (
        db.query(User)
        .filter((User.username == normalized_username) | (User.email == normalized_username))
        .first()
    )
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")
    return user


def parse_refresh_user(request: Request, db: Session) -> User:
    """Validate a refresh token from the cookie jar and return the associated user."""
    refresh_token = get_refresh_token_from_request(request)
    if not refresh_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing refresh token")

    jwt_service = get_jwt_service()
    try:
        payload = jwt_service.decode_token(refresh_token, expected_types=("refresh",))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    if user is None or not user.is_active or payload.get("session_nonce") != user.session_nonce:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token is no longer valid")
    return user


def rotate_session_nonce(user: User) -> None:
    """Rotate a user's session nonce to revoke existing signed tokens."""
    user.session_nonce = secrets.token_hex(16)


def get_user_by_id(user_id: int) -> User:
    """Lookup a user by id."""
    from vmcp.storage.database import SessionLocal

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
        db.expunge(user)
        return user
    finally:
        db.close()


def get_request_token_info(request: Request) -> TokenInfo:
    """Extract and normalize the current request token."""
    token = resolve_request_token(request)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing authentication token")

    jwt_service = get_jwt_service()
    try:
        return get_normalized_token_info(jwt_service, token)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc


@router.post("/register", response_model=AuthUserResponse, status_code=status.HTTP_201_CREATED)
async def register_local_user(payload: RegisterRequest, db: Session = Depends(get_db)) -> AuthUserResponse:
    """Register a new local username/password user."""
    if not settings.allow_self_registration:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Self-registration is disabled")

    normalized_username = payload.username.strip().lower()
    normalized_email = payload.email.strip().lower()
    if "@" not in normalized_email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A valid email address is required")
    existing_user = (
        db.query(User)
        .filter((User.username == normalized_username) | (User.email == normalized_email))
        .first()
    )
    if existing_user:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A user with that username or email already exists")

    first_name, last_name = split_full_name(payload.full_name)
    user = User(
        username=normalized_username,
        email=normalized_email,
        first_name=first_name,
        last_name=last_name or "",
        password_hash=hash_password(payload.password),
        is_active=True,
        is_verified=True,
        session_nonce=secrets.token_hex(16),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user_to_response(user)


@router.post("/login", response_model=LoginResponse)
async def login_local_user(
    request: Request,
    payload: LoginRequest,
    response: Response,
    db: Session = Depends(get_db),
) -> LoginResponse:
    """Authenticate a local user and issue signed session tokens."""
    user = authenticate_credentials(db, payload.username, payload.password)
    if not user.session_nonce:
        rotate_session_nonce(user)
    user.last_login = datetime.now(timezone.utc)
    db.add(user)
    db.commit()
    db.refresh(user)

    tokens = build_tokens(user)
    set_auth_cookies(request, response, tokens)
    return LoginResponse(
        access_token=tokens.access_token,
        expires_in=tokens.expires_in,
        user=user_to_response(user),
    )


@router.post("/refresh", response_model=LoginResponse)
async def refresh_local_session(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> LoginResponse:
    """Issue a fresh access token using the refresh cookie."""
    user = parse_refresh_user(request, db)
    tokens = build_tokens(user)
    set_auth_cookies(request, response, tokens)
    return LoginResponse(
        access_token=tokens.access_token,
        expires_in=tokens.expires_in,
        user=user_to_response(user),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout_local_user(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> Response:
    """Revoke the current browser session."""
    refresh_token = get_refresh_token_from_request(request)
    if refresh_token:
        jwt_service = get_jwt_service()
        try:
            payload = jwt_service.decode_token(refresh_token, expected_types=("refresh",))
            user = db.query(User).filter(User.id == int(payload["sub"])).first()
            if user is not None:
                rotate_session_nonce(user)
                db.add(user)
                db.commit()
        except ValueError:
            pass

    clear_auth_cookies(request, response)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/userinfo", response_model=AuthUserResponse)
async def get_userinfo(token_info: TokenInfo = Depends(get_request_token_info)) -> AuthUserResponse:
    """Return the currently authenticated user."""
    user = get_user_by_id(int(token_info.user_id))
    return user_to_response(user)


@router.post("/auth/ws-ticket", response_model=WebSocketTicketResponse)
async def create_websocket_ticket(token_info: TokenInfo = Depends(get_request_token_info)) -> WebSocketTicketResponse:
    """Create a short-lived ticket suitable for browser WebSocket/MCP upgrades."""
    user = get_user_by_id(int(token_info.user_id))
    jwt_service = get_jwt_service()
    ticket = jwt_service.create_token(
        user.id,
        username=user.username,
        email=user.email,
        session_nonce=user.session_nonce,
        token_type="ws_ticket",
        expires_in=settings.websocket_ticket_ttl_seconds,
    )
    return WebSocketTicketResponse(ticket=ticket, expires_in=settings.websocket_ticket_ttl_seconds)


__all__ = [
    "ACCESS_COOKIE_NAME",
    "REFRESH_COOKIE_NAME",
    "AuthUserResponse",
    "LoginRequest",
    "LoginResponse",
    "RegisterRequest",
    "TokenInfo",
    "build_tokens",
    "clear_auth_cookies",
    "get_normalized_token_info",
    "get_request_token_info",
    "get_refresh_token_from_request",
    "hash_password",
    "resolve_request_token",
    "router",
    "set_auth_cookies",
    "verify_password",
]
