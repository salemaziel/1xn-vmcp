"""
Authenticated user context helpers.

This module keeps the legacy filename for compatibility with existing imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, Request

from vmcp.server.auth_service import get_request_token_info
from vmcp.server.token_info import TokenInfo
from vmcp.utilities.logging import get_logger

logger = get_logger(__name__)


@dataclass
class UserContext:
    """User context for authenticated requests."""

    user_id: str
    username: str
    email: Optional[str]
    token: str
    vmcp_name: str
    vmcp_config_manager: Optional[object] = None
    vmcp_name_header: Optional[str] = None
    vmcp_username_header: Optional[str] = None
    client_id: Optional[str] = None
    client_name: Optional[str] = None
    agent_name: Optional[str] = None
    is_dummy: bool = False

    def __init__(
        self,
        user_id: str | int,
        username: Optional[str] = None,
        user_email: Optional[str] = None,
        token: Optional[str] = None,
        vmcp_name: Optional[str] = None,
    ):
        self.user_id = str(user_id)
        self.username = username or ""
        self.email = user_email
        self.token = token or ""
        self.vmcp_name = vmcp_name or "default"
        self.vmcp_config_manager = None
        self.vmcp_name_header = None
        self.vmcp_username_header = None
        self.client_id = None
        self.client_name = None
        self.agent_name = None
        self.is_dummy = False


def get_dummy_user_context() -> dict:
    """Deprecated compatibility helper."""
    raise RuntimeError("Dummy user context is no longer available")


def ensure_dummy_user() -> None:
    """No-op compatibility hook retained for application startup."""
    logger.info("Local authentication enabled; skipping dummy user bootstrap")


def _build_user_context(request: Request, token_info: TokenInfo) -> UserContext:
    user_context = UserContext(
        user_id=token_info.user_id,
        username=token_info.username,
        user_email=token_info.email,
        token=token_info.token,
        vmcp_name=request.headers.get("vmcp-name", "default"),
    )
    user_context.vmcp_name_header = request.headers.get("vmcp-name")
    user_context.vmcp_username_header = request.headers.get("vmcp-username")
    user_context.client_id = token_info.client_id
    user_context.client_name = token_info.client_name
    return user_context


def get_user_context(request: Request, token_info: TokenInfo = Depends(get_request_token_info)) -> UserContext:
    """Resolve the authenticated user context for FastAPI endpoints."""
    return _build_user_context(request, token_info)

