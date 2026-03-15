"""Default service implementations for the local authentication build."""

from typing import Optional

from vmcp.storage.dummy_jwt import LocalJWTService
from vmcp.storage.dummy_user import UserContext
from vmcp.utilities.logging import get_logger

logger = get_logger(__name__)

DummyJWTService = LocalJWTService
DummyUserContext = UserContext


def ensure_dummy_user():
    """Compatibility hook for startup."""
    logger.info("Local authentication enabled; no dummy user will be created")


class NoOpAnalyticsService:
    """No-op analytics service for OSS."""

    def track_event(
        self,
        event_name: str,
        user_id: str,
        properties: Optional[dict] = None
    ) -> None:
        """No-op tracking."""
        logger.debug(f"[OSS] Analytics disabled: {event_name}")

    def track_mcp_tool_call(
        self,
        user_id: str,
        tool_name: str,
        mcp_server: str,
        success: bool,
        properties: Optional[dict] = None
    ) -> None:
        """No-op MCP tool tracking."""
        logger.debug(f"[OSS] Analytics disabled: MCP tool call {tool_name}")


def register_oss_services():
    """Register default OSS service implementations."""
    from vmcp.core.services.registry import get_registry

    registry = get_registry()
    registry.register_jwt_service(service_class=LocalJWTService)
    registry.register_user_context(context_class=UserContext)
    registry.register_analytics_service(service_class=NoOpAnalyticsService)

    logger.info("✅ Local authentication services registered")
