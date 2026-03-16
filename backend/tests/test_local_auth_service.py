import importlib.util
import sys
import types
from pathlib import Path
from dataclasses import dataclass

from starlette.requests import Request

REPO_ROOT = Path(__file__).resolve().parents[1]
AUTH_SERVICE_PATH = REPO_ROOT / "src" / "vmcp" / "server" / "auth_service.py"

fake_config = types.ModuleType("vmcp.config")
fake_config.settings = types.SimpleNamespace(
    access_token_ttl_seconds=900,
    refresh_token_ttl_seconds=604800,
    websocket_ticket_ttl_seconds=30,
    auth_cookie_secure=False,
    auth_cookie_domain=None,
    allow_self_registration=True,
)
sys.modules["vmcp.config"] = fake_config

fake_core_services = types.ModuleType("vmcp.core.services")
fake_core_services.get_jwt_service = lambda: None
sys.modules["vmcp.core.services"] = fake_core_services

fake_database = types.ModuleType("vmcp.storage.database")
fake_database.get_db = lambda: iter(())
sys.modules["vmcp.storage.database"] = fake_database

fake_models = types.ModuleType("vmcp.storage.models")
fake_models.User = type("User", (), {})
sys.modules["vmcp.storage.models"] = fake_models

fake_token_info = types.ModuleType("vmcp.server.token_info")

@dataclass
class FakeTokenInfo:
    user_id: int
    username: str
    email: str
    token: str | None = None
    client_id: str | None = None
    client_name: str | None = None
    is_dummy: bool = False
    raw_info: dict | None = None


def fake_normalize_token_info(raw_token_info, token=None):
    return FakeTokenInfo(
        user_id=int(raw_token_info["user_id"]),
        username=raw_token_info["username"],
        email=raw_token_info["email"],
        token=token,
        client_id=raw_token_info.get("client_id"),
        client_name=raw_token_info.get("client_name"),
        raw_info=raw_token_info,
    )


fake_token_info.TokenInfo = FakeTokenInfo
fake_token_info.normalize_token_info = fake_normalize_token_info
sys.modules["vmcp.server.token_info"] = fake_token_info

auth_service_spec = importlib.util.spec_from_file_location("vmcp_auth_service_test", AUTH_SERVICE_PATH)
assert auth_service_spec and auth_service_spec.loader
auth_service = importlib.util.module_from_spec(auth_service_spec)
sys.modules["vmcp_auth_service_test"] = auth_service
auth_service_spec.loader.exec_module(auth_service)


def make_request(headers=None, query_string: bytes = b""):
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/userinfo",
            "headers": headers or [],
            "query_string": query_string,
        }
    )


def test_hash_password_round_trip():
    password = "CorrectHorseBatteryStaple123!"
    password_hash = auth_service.hash_password(password)

    assert password_hash != password
    assert auth_service.verify_password(password, password_hash) is True
    assert auth_service.verify_password("wrong-password", password_hash) is False


def test_resolve_request_token_prefers_bearer_header():
    request = make_request(
        headers=[
            (b"authorization", b"Bearer header-token"),
            (b"cookie", b"vmcp_access_token=cookie-token"),
        ],
        query_string=b"ticket=query-ticket",
    )

    assert auth_service.resolve_request_token(request) == "header-token"


def test_resolve_request_token_falls_back_to_cookie_and_ticket():
    cookie_request = make_request(headers=[(b"cookie", b"vmcp_access_token=cookie-token")])
    ticket_request = make_request(query_string=b"ticket=query-ticket")

    assert auth_service.resolve_request_token(cookie_request) == "cookie-token"
    assert auth_service.resolve_request_token(ticket_request) == "query-ticket"


def test_get_request_token_info_uses_registered_jwt_service(monkeypatch):
    request = make_request(headers=[(b"authorization", b"Bearer access-token")])

    class FakeJWTService:
        def extract_token_info(self, token: str):
            assert token == "access-token"
            return {
                "user_id": 7,
                "username": "alice",
                "email": "alice@example.com",
                "client_id": "web",
                "client_name": "Browser",
            }

    monkeypatch.setattr(auth_service, "get_jwt_service", lambda: FakeJWTService())

    token_info = auth_service.get_request_token_info(request)

    assert token_info.user_id == 7
    assert token_info.username == "alice"
    assert token_info.email == "alice@example.com"
    assert token_info.token == "access-token"
    assert token_info.client_id == "web"
    assert token_info.client_name == "Browser"
