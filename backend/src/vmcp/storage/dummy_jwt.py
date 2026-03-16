"""
JWT service for local username/password authentication.

This module keeps the legacy filename for compatibility while replacing the
old dummy implementation with real signed tokens.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional

from jose import JWTError, ExpiredSignatureError, jwt

from vmcp.config import settings


class LocalJWTService:
    """Issue and validate signed JWTs for local authentication."""

    default_token_types = ("access", "ws_ticket")

    def __init__(self) -> None:
        if not settings.jwt_secret_key:
            raise RuntimeError("VMCP_JWT_SECRET_KEY must be configured")
        self.secret_key = settings.jwt_secret_key
        self.algorithm = settings.jwt_algorithm

    def _decode(self, token: str, expected_types: Iterable[str]) -> Dict[str, Any]:
        try:
            payload = jwt.decode(token, self.secret_key, algorithms=[self.algorithm])
        except ExpiredSignatureError as exc:
            raise ValueError("Token has expired") from exc
        except JWTError as exc:
            raise ValueError("Invalid token") from exc

        token_type = payload.get("type")
        if token_type not in set(expected_types):
            raise ValueError("Invalid token type")

        if not payload.get("sub") or not payload.get("username"):
            raise ValueError("Token payload is missing required claims")

        return payload

    def decode_token(self, token: str, expected_types: Iterable[str] | None = None) -> Dict[str, Any]:
        """Decode a token and validate its type."""
        return self._decode(token, expected_types or self.default_token_types)

    def extract_token_info(self, token: str) -> Optional[Dict[str, Any]]:
        """
        Extract token information for request authentication.

        Access tokens and short-lived WebSocket tickets are both accepted here.
        """
        payload = self.decode_token(token)

        from vmcp.storage.database import SessionLocal
        from vmcp.storage.models import User

        db = SessionLocal()
        try:
            user = db.query(User).filter(User.id == int(payload["sub"])).first()
            if user is None or not user.is_active:
                raise ValueError("User not found or inactive")
            if payload.get("session_nonce") != user.session_nonce:
                raise ValueError("Token session is no longer valid")

            return {
                "user_id": user.id,
                "username": user.username,
                "email": user.email,
                "client_id": payload.get("client_id"),
                "client_name": payload.get("client_name"),
                "token_type": payload.get("type"),
            }
        finally:
            db.close()

    def validate_token(self, token: str) -> bool:
        """Return True when the token can be decoded and resolved to an active user."""
        try:
            self.extract_token_info(token)
            return True
        except ValueError:
            return False

    def create_token(
        self,
        user_id: int,
        *,
        username: str,
        email: str,
        session_nonce: str,
        token_type: str = "access",
        expires_in: Optional[int] = None,
        additional_claims: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Create a signed JWT."""
        now = datetime.now(timezone.utc)
        ttl = expires_in
        if ttl is None:
            ttl = (
                settings.access_token_ttl_seconds
                if token_type == "access"
                else settings.refresh_token_ttl_seconds
            )

        payload: Dict[str, Any] = {
            "sub": str(user_id),
            "username": username,
            "email": email,
            "session_nonce": session_nonce,
            "type": token_type,
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=ttl)).timestamp()),
        }
        if additional_claims:
            payload.update(additional_claims)
        return jwt.encode(payload, self.secret_key, algorithm=self.algorithm)


DummyJWTService = LocalJWTService

