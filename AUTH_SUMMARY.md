# Authentication Implementation Summary (Stage 1 Complete)

## Quick Overview

**Current State**: Full local authentication with JWT tokens and secure cookies is implemented.

**Status**: 
- ✅ Local username/password auth with Scrypt hashing
- ✅ JWT tokens (access, refresh, ws_ticket)
- ✅ HttpOnly secure cookies
- ✅ Session nonce-based revocation
- ⏳ Google OAuth2 / OIDC (not yet started)

---

## Architecture at a Glance

### Backend Stack
- **Auth Service**: `backend/src/vmcp/server/auth_service.py` (6 endpoints)
- **Token Service**: `backend/src/vmcp/storage/dummy_jwt.py` (LocalJWTService)
- **Database**: User table with `password_hash`, `session_nonce`, OAuth-ready
- **Middleware**: MCP auth resolution (Bearer → cookie → ticket)
- **Config**: Env vars for JWT secret, TTLs, cookie settings

### Frontend Stack
- **Auth Context**: `frontend/src/contexts/auth-context.tsx`
- **API Client**: `frontend/src/api/client.ts` 
- **Storage**: Access token in localStorage, refresh token in HttpOnly cookie
- **Flow**: Login → store token → set Bearer header → API calls

### Token Lifespan
- **Access Token**: 15 minutes (900s)
- **Refresh Token**: 7 days (604800s)
- **WS Ticket**: 30 seconds (for WebSocket upgrades)

---

## Core Endpoints (All in `/api`)

| Endpoint | Auth | Purpose |
|----------|------|---------|
| POST `/register` | No | Create local user |
| POST `/login` | No | Username/password → tokens + cookies |
| POST `/refresh` | No | Use refresh cookie → new access token |
| POST `/logout` | No | Clear cookies + rotate session nonce |
| GET `/userinfo` | Yes | Get current user |
| POST `/auth/ws-ticket` | Yes | Short-lived ticket for WebSocket |

---

## Key Implementation Details

### Password Hashing
```
Scrypt(n=2^14, r=8, p=1) → Base64
Format: scrypt${n}${r}${p}${salt}${digest}
```

### JWT Claims
```json
{
  "sub": "user_id",
  "username": "user@example.com",
  "email": "user@example.com",
  "session_nonce": "server-side-revocation-token",
  "type": "access|refresh|ws_ticket",
  "exp": 1704068100
}
```

### Session Revocation
- Each user has a `session_nonce` in database
- Tokens include nonce in JWT claims
- On logout/password change: rotate nonce → all old tokens invalid
- No token blacklist database needed

### Cookie Security
- **HttpOnly**: JavaScript can't access
- **Secure**: Only over HTTPS
- **SameSite=Lax**: CSRF protection
- Domain: Configurable (`VMCP_AUTH_COOKIE_DOMAIN`)

### MCP Authentication Order
1. `Authorization: Bearer <token>` header
2. `vmcp_access_token` cookie
3. `ticket=<token>` query parameter

---

## Database User Schema

```python
class User:
    id: int (primary key)
    username: str (unique)
    email: str (unique)
    first_name: str
    last_name: str
    password_hash: str (nullable, for local users)
    is_active: bool
    is_verified: bool
    session_nonce: str (revocation token)
    last_login: datetime (nullable)
    created_at: datetime
    updated_at: datetime
```

**For Stage 2**, add OAuth fields:
```python
    oauth_provider: str (nullable)          # "google", "github", etc.
    oauth_subject: str (nullable)            # Provider's user ID
    oauth_email_verified: bool
    oauth_access_token: str (nullable)
    oauth_refresh_token: str (nullable)
    oauth_id_token: str (nullable)
    oauth_expires_at: datetime (nullable)
    oauth_metadata: JSON (nullable)
```

---

## Environment Variables (Stage 1)

```bash
# Required
VMCP_JWT_SECRET_KEY=<random-secret>        # ⚠️ CRITICAL for production

# Optional (defaults shown)
VMCP_JWT_ALGORITHM=HS256
VMCP_ACCESS_TOKEN_TTL_SECONDS=900
VMCP_REFRESH_TOKEN_TTL_SECONDS=604800
VMCP_WEBSOCKET_TICKET_TTL_SECONDS=30
VMCP_AUTH_COOKIE_SECURE=false              # Set true for HTTPS
VMCP_AUTH_COOKIE_DOMAIN=                   # Domain override
VMCP_ALLOW_SELF_REGISTRATION=true
VMCP_TRUSTED_PROXIES=127.0.0.1
VMCP_ALLOWED_HOSTS=localhost,127.0.0.1
```

---

## How to Test

### Backend Auth Tests
```bash
cd backend
uv run pytest tests/test_local_auth_service.py --noconftest -v
```

### Frontend Auth Tests
```bash
cd frontend
npm run test:run -- src/api/client.test.ts
```

### Manual Testing
```bash
# Start backend
make run

# Login (returns access_token)
curl -X POST http://localhost:8000/api/login \
  -H "Content-Type: application/json" \
  -d '{"username":"user@example.com","password":"password"}'

# Use token
curl -X GET http://localhost:8000/api/userinfo \
  -H "Authorization: Bearer <access_token>"

# Logout
curl -X POST http://localhost:8000/api/logout -b "vmcp_refresh_token=<token>"
```

---

## Linting & Building

### Backend
```bash
cd backend
uv run ruff check src/vmcp/server/auth_service.py        # Lint
uv run black src/vmcp/server/auth_service.py             # Format
uv run mypy src/vmcp/server/auth_service.py              # Type check
uv run pytest backend/tests/ -v                          # Test all
```

### Frontend
```bash
cd frontend
npm run lint                                              # Lint
npm run build                                             # Build
npm run test:run                                          # Test
```

### Full Build
```bash
make build-frontend        # Build frontend, copy to backend
make run                   # Start full stack
```

---

## Existing OAuth Code (For MCP Servers)

**Files**: 
- `backend/src/vmcp/mcps/oauth_handler.py`
- `backend/src/vmcp/mcps/oauth_state_manager.py`

**Purpose**: Authenticating **MCP servers** (not users)
- Example: MCP server needs GitHub OAuth to access repos
- NOT related to vMCP user login

**Don't confuse with Stage 2 user OAuth** – keep separate in `backend/src/vmcp/server/oauth_service.py`

---

## Conflicts & Reusable Code (For Stage 2)

### ✅ Can Reuse
1. `LocalJWTService` – Token validation logic
2. `TokenInfo` – Extend to include OAuth claims
3. Cookie management – Same HttpOnly pattern
4. MCP middleware – Already accepts multiple token sources
5. Frontend API client – Same Bearer token injection

### ⚠️ Watch Out For
1. Don't mix MCP server OAuth (`oauth_handler.py`) with user OAuth
2. User ID linking: local users (auto-increment) vs OAuth (provider + subject)
3. Email conflicts: same email registered locally + via OAuth
4. Logout: need to revoke OAuth tokens with provider
5. Refresh token rotation: may need separate counter per provider

---

## Next Steps for Stage 2

1. **Add Config**: Google/OIDC client IDs, redirect URIs
2. **Extend User Model**: OAuth fields + migration
3. **Create OAuth Service**: `/api/oauth/authorize`, `/api/oauth/callback`, `/api/oauth/token`
4. **Frontend**: Wire up `GoogleAuthButton.tsx` to OAuth flow
5. **Dependencies**: Add `authlib>=1.2.0` to pyproject.toml
6. **Testing**: OAuth authorization, token exchange, user linking

See `STAGE2_AUTH_IMPLEMENTATION_GUIDE.md` for detailed integration plan.

