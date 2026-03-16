"""
Google OAuth2 and generic OpenID Connect login flows.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy.orm import Session

from vmcp.config import settings
from vmcp.server.auth_service import (
    build_tokens,
    rotate_session_nonce,
    set_auth_cookies,
    split_full_name,
)
from vmcp.storage.database import get_db
from vmcp.storage.models import OAuthLoginState, User, UserOAuthAccount

router = APIRouter(prefix="/api/auth/oauth", tags=["auth"])

OAUTH_STATE_COOKIE_NAME = "vmcp_oauth_state"
_DISCOVERY_CACHE: dict[str, tuple[dict[str, Any], datetime]] = {}
_JWKS_CACHE: dict[str, tuple[dict[str, Any], datetime]] = {}


class OAuthProviderInfo(BaseModel):
    id: str
    display_name: str
    enabled: bool


class OAuthProviderListResponse(BaseModel):
    providers: list[OAuthProviderInfo]


@dataclass
class OAuthProviderConfig:
    provider: str
    display_name: str
    client_id: str
    client_secret: str
    discovery_url: str
    scope: str


@dataclass
class ExternalIdentity:
    provider: str
    issuer: str
    subject: str
    email: str
    email_verified: bool
    first_name: str
    last_name: str
    full_name: str
    picture_url: Optional[str]
    claims: dict[str, Any]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_secure_cookie(request: Request) -> bool:
    return settings.auth_cookie_secure or request.url.scheme == "https"


def _common_cookie_kwargs(request: Request) -> dict[str, Any]:
    return {
        "httponly": True,
        "secure": _is_secure_cookie(request),
        "samesite": "lax",
        "domain": settings.auth_cookie_domain,
        "path": "/",
    }


def _set_oauth_state_cookie(request: Request, response: Response, state: str) -> None:
    response.set_cookie(
        OAUTH_STATE_COOKIE_NAME,
        state,
        max_age=settings.oauth_state_ttl_seconds,
        **_common_cookie_kwargs(request),
    )


def _clear_oauth_state_cookie(request: Request, response: Response) -> None:
    response.delete_cookie(
        OAUTH_STATE_COOKIE_NAME,
        domain=settings.auth_cookie_domain,
        path="/",
        secure=_is_secure_cookie(request),
        httponly=True,
        samesite="lax",
    )


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _bool_claim(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    if isinstance(value, int):
        return value != 0
    return False


def _normalize_return_to(return_to: Optional[str]) -> str:
    if not return_to:
        return settings.oauth_callback_frontend_path
    parsed = urlsplit(return_to)
    if parsed.scheme or parsed.netloc or return_to.startswith("//"):
        return settings.oauth_callback_frontend_path
    if not return_to.startswith("/"):
        return settings.oauth_callback_frontend_path
    if return_to == "/login" or return_to.startswith("/app/"):
        return return_to
    return settings.oauth_callback_frontend_path


def _normalize_username(value: Optional[str]) -> str:
    candidate = (value or "").strip().lower()
    candidate = re.sub(r"[^a-z0-9_]+", "_", candidate)
    candidate = re.sub(r"_+", "_", candidate).strip("_")
    if not candidate:
        candidate = "user"
    if len(candidate) < 3:
        candidate = f"{candidate}_user"
    return candidate[:50]


def _generate_unique_username(db: Session, *candidates: Optional[str]) -> str:
    for candidate in candidates:
        normalized = _normalize_username(candidate)
        if not db.query(User).filter(User.username == normalized).first():
            return normalized

    base = _normalize_username(f"user_{secrets.token_hex(4)}")
    if not db.query(User).filter(User.username == base).first():
        return base

    while True:
        fallback = _normalize_username(f"user_{secrets.token_hex(6)}")
        if not db.query(User).filter(User.username == fallback).first():
            return fallback


def _provider_config(provider: str) -> OAuthProviderConfig:
    if provider == "google":
        if not settings.google_oauth_client_id or not settings.google_oauth_client_secret:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Google login is not configured")
        return OAuthProviderConfig(
            provider="google",
            display_name="Google",
            client_id=settings.google_oauth_client_id,
            client_secret=settings.google_oauth_client_secret,
            discovery_url=settings.google_oauth_discovery_url,
            scope=settings.google_oauth_scope,
        )
    if provider == "oidc":
        if not settings.oidc_client_id or not settings.oidc_client_secret or not settings.oidc_discovery_url:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="OIDC login is not configured")
        return OAuthProviderConfig(
            provider="oidc",
            display_name=settings.oidc_provider_name,
            client_id=settings.oidc_client_id,
            client_secret=settings.oidc_client_secret,
            discovery_url=settings.oidc_discovery_url,
            scope=settings.oidc_scope,
        )
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown OAuth provider")


async def _fetch_json_cached(cache: dict[str, tuple[dict[str, Any], datetime]], url: str) -> dict[str, Any]:
    cached = cache.get(url)
    now = _utcnow()
    if cached and cached[1] > now:
        return cached[0]
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()
    cache[url] = (payload, now + timedelta(minutes=10))
    return payload


async def _get_discovery_document(config: OAuthProviderConfig) -> dict[str, Any]:
    return await _fetch_json_cached(_DISCOVERY_CACHE, config.discovery_url)


async def _get_jwks(jwks_uri: str) -> dict[str, Any]:
    return await _fetch_json_cached(_JWKS_CACHE, jwks_uri)


def _callback_url(provider: str) -> str:
    return f"{settings.base_url.rstrip('/')}/api/auth/oauth/{provider}/callback"


def _build_authorization_url(
    authorization_endpoint: str,
    *,
    client_id: str,
    redirect_uri: str,
    scope: str,
    state: str,
    nonce: str,
    code_challenge: str,
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{authorization_endpoint}?{urlencode(params)}"


async def _exchange_code_for_tokens(
    config: OAuthProviderConfig,
    token_endpoint: str,
    *,
    code: str,
    code_verifier: str,
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _callback_url(config.provider),
                "client_id": config.client_id,
                "client_secret": config.client_secret,
                "code_verifier": code_verifier,
            },
            headers={"Accept": "application/json"},
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text[:300]
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"OAuth token exchange failed: {detail}") from exc
        return response.json()


async def _verify_id_token(
    config: OAuthProviderConfig,
    discovery: dict[str, Any],
    *,
    id_token: str,
    nonce: str,
) -> dict[str, Any]:
    jwks_uri = discovery.get("jwks_uri")
    if not jwks_uri:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="OIDC discovery metadata is incomplete")

    header = jwt.get_unverified_header(id_token)
    kid = header.get("kid")
    jwks = await _get_jwks(jwks_uri)
    keys = jwks.get("keys", [])
    key = next((item for item in keys if item.get("kid") == kid), None)
    if key is None and len(keys) == 1:
        key = keys[0]
    if key is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unable to verify provider identity token")

    try:
        claims = jwt.decode(
            id_token,
            key,
            algorithms=[key.get("alg", "RS256"), "RS256"],
            audience=config.client_id,
            options={"verify_iss": False},
        )
    except JWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid provider identity token") from exc

    expected_issuer = str(discovery.get("issuer", "")).rstrip("/")
    received_issuer = str(claims.get("iss", "")).rstrip("/")
    valid_issuers = {expected_issuer}
    if config.provider == "google":
        valid_issuers.add("accounts.google.com")
        valid_issuers.add("https://accounts.google.com")
    if received_issuer not in valid_issuers:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unexpected identity token issuer")

    if claims.get("nonce") != nonce:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid identity token nonce")
    return claims


async def _fetch_userinfo(discovery: dict[str, Any], access_token: Optional[str]) -> dict[str, Any]:
    userinfo_endpoint = discovery.get("userinfo_endpoint")
    if not access_token or not userinfo_endpoint:
        return {}
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            userinfo_endpoint,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
        if response.is_success:
            return response.json()
    return {}


def _identity_from_claims(provider: str, claims: dict[str, Any]) -> ExternalIdentity:
    email = str(claims.get("email") or "").strip().lower()
    full_name = str(claims.get("name") or "").strip()
    first_name = str(claims.get("given_name") or "").strip()
    last_name = str(claims.get("family_name") or "").strip()
    if full_name and (not first_name and not last_name):
        first_name, last_name = split_full_name(full_name)
    if not full_name:
        full_name = " ".join(part for part in [first_name, last_name] if part).strip()
    if not full_name and email:
        full_name = email.split("@", 1)[0]

    return ExternalIdentity(
        provider=provider,
        issuer=str(claims.get("iss") or ""),
        subject=str(claims.get("sub") or ""),
        email=email,
        email_verified=_bool_claim(claims.get("email_verified")),
        first_name=first_name or "OAuth",
        last_name=last_name or "User",
        full_name=full_name or "OAuth User",
        picture_url=str(claims.get("picture")) if claims.get("picture") else None,
        claims=claims,
    )


def _upsert_oauth_account(db: Session, user: User, identity: ExternalIdentity) -> UserOAuthAccount:
    account = (
        db.query(UserOAuthAccount)
        .filter(
            UserOAuthAccount.provider == identity.provider,
            UserOAuthAccount.issuer == identity.issuer,
            UserOAuthAccount.subject == identity.subject,
        )
        .first()
    )
    if account is None:
        account = UserOAuthAccount(
            user=user,
            provider=identity.provider,
            issuer=identity.issuer,
            subject=identity.subject,
        )
    account.email = identity.email or None
    account.email_verified = identity.email_verified
    account.picture_url = identity.picture_url
    account.claims_json = identity.claims
    account.last_login_at = _utcnow()
    db.add(account)
    return account


def resolve_oauth_user(
    db: Session,
    *,
    identity: ExternalIdentity,
    requested_username: Optional[str],
) -> User:
    linked_account = (
        db.query(UserOAuthAccount)
        .filter(
            UserOAuthAccount.provider == identity.provider,
            UserOAuthAccount.issuer == identity.issuer,
            UserOAuthAccount.subject == identity.subject,
        )
        .first()
    )
    now = _utcnow()

    if linked_account is not None:
        user = linked_account.user
        if not user.is_active:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User account is disabled")
        user.last_login = now
        if not user.session_nonce:
            rotate_session_nonce(user)
        _upsert_oauth_account(db, user, identity)
        db.add(user)
        return user

    if not identity.email:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The identity provider did not return an email address",
        )

    existing_user = db.query(User).filter(User.email == identity.email).first()
    if existing_user is not None:
        if not identity.email_verified:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot link an OAuth account unless the provider verified the email address",
            )
        if not existing_user.is_active:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User account is disabled")
        existing_user.last_login = now
        if not existing_user.session_nonce:
            rotate_session_nonce(existing_user)
        _upsert_oauth_account(db, existing_user, identity)
        db.add(existing_user)
        return existing_user

    if not identity.email_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot create a new account from an unverified provider email address",
        )

    username = _generate_unique_username(
        db,
        requested_username,
        identity.claims.get("preferred_username"),
        identity.email.split("@", 1)[0],
        identity.full_name.replace(" ", "_"),
    )
    first_name, last_name = split_full_name(identity.full_name)
    user = User(
        username=username,
        email=identity.email,
        first_name=first_name or identity.first_name or "OAuth",
        last_name=last_name or identity.last_name or "User",
        is_active=True,
        is_verified=True,
        session_nonce=secrets.token_hex(16),
        last_login=now,
    )
    db.add(user)
    db.flush()
    _upsert_oauth_account(db, user, identity)
    return user


def _redirect_with_error(
    request: Request,
    *,
    return_to: str,
    error_code: str,
) -> RedirectResponse:
    separator = "&" if "?" in return_to else "?"
    response = RedirectResponse(f"{return_to}{separator}error={error_code}", status_code=status.HTTP_302_FOUND)
    _clear_oauth_state_cookie(request, response)
    return response


@router.get("/providers", response_model=OAuthProviderListResponse)
async def list_oauth_providers() -> OAuthProviderListResponse:
    """Return the enabled interactive auth providers for the login screen."""
    providers = [
        OAuthProviderInfo(
            id="google",
            display_name="Google",
            enabled=bool(settings.google_oauth_client_id and settings.google_oauth_client_secret),
        ),
        OAuthProviderInfo(
            id="oidc",
            display_name=settings.oidc_provider_name,
            enabled=bool(settings.oidc_client_id and settings.oidc_client_secret and settings.oidc_discovery_url),
        ),
    ]
    return OAuthProviderListResponse(providers=providers)


@router.get("/{provider}/start")
async def start_oauth_login(
    provider: str,
    request: Request,
    db: Session = Depends(get_db),
    mode: str = Query(default="login", pattern="^(login|register)$"),
    username: Optional[str] = Query(default=None, min_length=3, max_length=50),
    return_to: Optional[str] = Query(default=None),
) -> Response:
    """Start a Google/OIDC login flow and redirect to the provider."""
    del mode  # mode is used by the frontend for UX only; account creation still requires verified provider identity
    config = _provider_config(provider)
    discovery = await _get_discovery_document(config)
    authorization_endpoint = discovery.get("authorization_endpoint")
    if not authorization_endpoint:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="OIDC discovery metadata is incomplete")

    raw_state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    code_verifier = _b64url(secrets.token_bytes(48))
    code_challenge = _b64url(hashlib.sha256(code_verifier.encode("ascii")).digest())

    state_record = OAuthLoginState(
        provider=provider,
        state_hash=_sha256_hex(raw_state),
        code_verifier=code_verifier,
        nonce=nonce,
        requested_username=_normalize_username(username) if username else None,
        return_to=_normalize_return_to(return_to),
        expires_at=_utcnow() + timedelta(seconds=settings.oauth_state_ttl_seconds),
    )
    db.add(state_record)
    db.commit()

    authorization_url = _build_authorization_url(
        authorization_endpoint,
        client_id=config.client_id,
        redirect_uri=_callback_url(provider),
        scope=config.scope,
        state=raw_state,
        nonce=nonce,
        code_challenge=code_challenge,
    )

    redirect = RedirectResponse(authorization_url, status_code=status.HTTP_302_FOUND)
    _set_oauth_state_cookie(request, redirect, raw_state)
    return redirect


@router.get("/{provider}/callback")
async def oauth_callback(
    provider: str,
    request: Request,
    db: Session = Depends(get_db),
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
) -> Response:
    """Handle a Google/OIDC callback, establish a local session, and redirect back to the SPA."""
    default_return_to = settings.oauth_callback_frontend_path
    if error:
        return _redirect_with_error(request, return_to=default_return_to, error_code="oauth_access_denied")

    if not code or not state:
        return _redirect_with_error(request, return_to=default_return_to, error_code="oauth_invalid_callback")

    cookie_state = request.cookies.get(OAUTH_STATE_COOKIE_NAME)
    if not cookie_state or cookie_state != state:
        return _redirect_with_error(request, return_to=default_return_to, error_code="oauth_invalid_state")

    state_hash = _sha256_hex(state)
    state_record = (
        db.query(OAuthLoginState)
        .filter(OAuthLoginState.provider == provider, OAuthLoginState.state_hash == state_hash)
        .first()
    )
    if (
        state_record is None
        or state_record.used_at is not None
        or state_record.expires_at.replace(tzinfo=timezone.utc) < _utcnow()
    ):
        return _redirect_with_error(request, return_to=default_return_to, error_code="oauth_invalid_state")

    return_to = _normalize_return_to(state_record.return_to)
    state_record.used_at = _utcnow()
    db.add(state_record)
    db.commit()

    try:
        config = _provider_config(provider)
        discovery = await _get_discovery_document(config)
        token_payload = await _exchange_code_for_tokens(
            config,
            discovery.get("token_endpoint", ""),
            code=code,
            code_verifier=state_record.code_verifier,
        )
        id_token = token_payload.get("id_token")
        if not id_token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Provider did not return an identity token")
        verified_claims = await _verify_id_token(
            config,
            discovery,
            id_token=id_token,
            nonce=state_record.nonce,
        )
        userinfo_claims = await _fetch_userinfo(discovery, token_payload.get("access_token"))
        merged_claims = {**userinfo_claims, **verified_claims}
        identity = _identity_from_claims(provider, merged_claims)
        user = resolve_oauth_user(db, identity=identity, requested_username=state_record.requested_username)
        db.commit()
        db.refresh(user)
    except HTTPException as exc:
        db.rollback()
        return _redirect_with_error(request, return_to=return_to, error_code=exc.detail.replace(" ", "_").lower())
    except Exception:
        db.rollback()
        return _redirect_with_error(request, return_to=return_to, error_code="oauth_login_failed")

    tokens = build_tokens(user)
    separator = "&" if "?" in return_to else "?"
    redirect = RedirectResponse(
        f"{return_to}{separator}provider={provider}",
        status_code=status.HTTP_302_FOUND,
    )
    set_auth_cookies(request, redirect, tokens)
    _clear_oauth_state_cookie(request, redirect)
    return redirect
