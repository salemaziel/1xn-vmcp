import importlib.util
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
MODELS_PATH = REPO_ROOT / "src" / "vmcp" / "storage" / "models.py"
TOKEN_INFO_PATH = REPO_ROOT / "src" / "vmcp" / "server" / "token_info.py"
AUTH_SERVICE_PATH = REPO_ROOT / "src" / "vmcp" / "server" / "auth_service.py"
OAUTH_SERVICE_PATH = REPO_ROOT / "src" / "vmcp" / "server" / "oauth_service.py"

fake_server_pkg = types.ModuleType("vmcp.server")
fake_server_pkg.__path__ = [str(REPO_ROOT / "src" / "vmcp" / "server")]
sys.modules["vmcp.server"] = fake_server_pkg

fake_storage_pkg = types.ModuleType("vmcp.storage")
fake_storage_pkg.__path__ = [str(REPO_ROOT / "src" / "vmcp" / "storage")]
sys.modules["vmcp.storage"] = fake_storage_pkg

fake_core_services = types.ModuleType("vmcp.core.services")
fake_core_services.get_jwt_service = lambda: None
sys.modules["vmcp.core.services"] = fake_core_services

fake_database = types.ModuleType("vmcp.storage.database")
fake_database.get_db = lambda: iter(())
sys.modules["vmcp.storage.database"] = fake_database


def _load_module(module_name: str, module_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


models = _load_module("vmcp.storage.models", MODELS_PATH)
_load_module("vmcp.server.token_info", TOKEN_INFO_PATH)
_load_module("vmcp.server.auth_service", AUTH_SERVICE_PATH)
oauth_service = _load_module("vmcp.server.oauth_service", OAUTH_SERVICE_PATH)

Base = models.Base
User = models.User
UserOAuthAccount = models.UserOAuthAccount
ExternalIdentity = oauth_service.ExternalIdentity
_normalize_return_to = oauth_service._normalize_return_to
resolve_oauth_user = oauth_service.resolve_oauth_user


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:")
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def make_identity(
    *,
    email: str = "alice@example.com",
    email_verified: bool = True,
    subject: str = "subject-123",
    provider: str = "google",
) -> ExternalIdentity:
    return ExternalIdentity(
        provider=provider,
        issuer="https://accounts.google.com" if provider == "google" else "https://issuer.example.com",
        subject=subject,
        email=email,
        email_verified=email_verified,
        first_name="Alice",
        last_name="Example",
        full_name="Alice Example",
        picture_url="https://example.com/avatar.png",
        claims={"sub": subject, "email": email, "email_verified": email_verified},
    )


def test_normalize_return_to_only_allows_local_app_paths():
    assert _normalize_return_to("/app/oauth/callback/success") == "/app/oauth/callback/success"
    assert _normalize_return_to("/app/login?client_id=abc") == "/app/login?client_id=abc"
    assert _normalize_return_to("https://evil.example/callback") == "/app/oauth/callback/success"
    assert _normalize_return_to("//evil.example/callback") == "/app/oauth/callback/success"
    assert _normalize_return_to("/api/auth/oauth/google/callback") == "/app/oauth/callback/success"


def test_resolve_oauth_user_links_existing_local_account_when_email_is_verified(db_session):
    user = User(
        username="alice",
        email="alice@example.com",
        first_name="Alice",
        last_name="Local",
        is_active=True,
        is_verified=True,
        session_nonce="nonce-123",
        last_login=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    resolved = resolve_oauth_user(db_session, identity=make_identity(), requested_username=None)
    db_session.commit()

    assert resolved.id == user.id
    account = db_session.query(UserOAuthAccount).filter(UserOAuthAccount.user_id == user.id).one()
    assert account.provider == "google"
    assert account.email_verified is True


def test_resolve_oauth_user_rejects_unverified_email_link_for_existing_user(db_session):
    user = User(
        username="alice",
        email="alice@example.com",
        first_name="Alice",
        last_name="Local",
        is_active=True,
        is_verified=True,
        session_nonce="nonce-123",
    )
    db_session.add(user)
    db_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        resolve_oauth_user(db_session, identity=make_identity(email_verified=False), requested_username=None)

    assert exc_info.value.status_code == 409
    assert db_session.query(UserOAuthAccount).count() == 0


def test_resolve_oauth_user_creates_new_user_with_requested_username(db_session):
    resolved = resolve_oauth_user(
        db_session,
        identity=make_identity(email="new.user@example.com", subject="brand-new"),
        requested_username="preferred_name",
    )
    db_session.commit()
    db_session.refresh(resolved)

    assert resolved.username == "preferred_name"
    assert resolved.email == "new.user@example.com"
    assert resolved.is_verified is True
    assert db_session.query(UserOAuthAccount).filter(UserOAuthAccount.user_id == resolved.id).count() == 1
