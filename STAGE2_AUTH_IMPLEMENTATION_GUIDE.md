# Stage 1 Authentication Architecture Review

## Current Architecture Summary

### Stage 1 Implementation Status
- **Stage 1 (Local Auth)**: ✅ COMPLETE - Username/password with signed JWTs
- **Stage 2 (Google OAuth2 + OIDC)**: ⏳ TODO - Not yet implemented

The backend has successfully transitioned from dummy/mock auth to **real local authentication** with:
- Scrypt password hashing (n=2^14, r=8, p=1)
- Signed JWTs (HS256 by default)
- HttpOnly secure cookies for refresh tokens
- Session nonce-based token revocation
- MCP authentication via Bearer token, cookie, or short-lived ticket

---

## Relevant Files & Locations

### Backend Auth Files
| File | Purpose |
|------|---------|
| `backend/src/vmcp/server/auth_service.py` | Primary auth endpoints (login, register, refresh, logout, userinfo, ws-ticket) |
| `backend/src/vmcp/storage/dummy_jwt.py` | `LocalJWTService` - JWT creation and validation |
| `backend/src/vmcp/server/token_info.py` | `TokenInfo` dataclass - normalized token representation |
| `backend/src/vmcp/server/middleware.py` | MCP auth middleware - resolves tokens from header/cookie/query |
| `backend/src/vmcp/storage/models.py` | User model with auth fields |
| `backend/src/vmcp/storage/dummy_user.py` | `UserContext` & `get_user_context()` dependency |
| `backend/src/vmcp/server/vmcp_server.py` | FastAPI app, auth router mounting |
| `backend/src/vmcp/config.py` | Auth configuration (JWT secrets, TTLs, cookies) |
| `backend/authentication.md` | Stage 1 implementation notes |

### Frontend Auth Files
| File | Purpose |
|------|---------|
| `frontend/src/contexts/auth-context.tsx` | Auth state management, login/logout/refresh logic |
| `frontend/src/api/client.ts` | API client with `login()`, `logout()`, `refreshSession()`, `getUserInfo()` |
| `frontend/src/components/ui/GoogleAuthButton.tsx` | Google OAuth button (UI-only, not yet wired to Stage 2 backend) |

### Existing OAuth/MCP Server OAuth Files
| File | Purpose |
|------|---------|
| `backend/src/vmcp/mcps/oauth_handler.py` | OAuth callback handler for **MCP servers** (NOT user auth) |
| `backend/src/vmcp/mcps/oauth_state_manager.py` | OAuth state storage for MCP server auth flows |

**⚠️ Important**: These are for authenticating **MCP servers themselves** (e.g., connecting to GitHub, Google APIs as an MCP server), NOT for user login to vMCP.

---

## Database Schema (Auth-Related)

### Users Table
Located in `backend/src/vmcp/storage/models.py`, class `User`:

```python
class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True, nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    first_name = Column(String(100), nullable=False)
    last_name = Column(String(100), nullable=False)
    
    # Auth fields (NEW in Stage 1)
    password_hash = Column(String(512), nullable=True)          # Scrypt hash
    is_active = Column(Boolean, nullable=False, default=True)
    is_verified = Column(Boolean, nullable=False, default=True)
    session_nonce = Column(String(64), nullable=False)          # Server-side revocation token
    last_login = Column(DateTime, nullable=True)
    
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now())
```

**Schema Migration**: `backend/src/vmcp/storage/migrations.py` - Migration 003 adds auth fields.

**For Stage 2 (OAuth2/OIDC)**, consider adding:
```python
oauth_provider = Column(String(50), nullable=True)              # "google", "github", "okta", etc.
oauth_subject = Column(String(255), nullable=True)             # Provider's unique identifier
oauth_email_verified = Column(Boolean, default=False)
oauth_access_token = Column(String(2048), nullable=True)       # For refresh capability
oauth_refresh_token = Column(String(2048), nullable=True)
oauth_id_token = Column(String(2048), nullable=True)          # For claim verification
oauth_expires_at = Column(DateTime, nullable=True)
oauth_metadata = Column(JSONType, nullable=True)              # Additional provider claims
```

---

## Current Auth Routes (Stage 1)

All routes are in `backend/src/vmcp/server/auth_service.py`:

### Endpoints (in `/api` prefix)
| Method | Path | Auth Required | Purpose |
|--------|------|--------------|---------|
| POST | `/api/register` | ❌ No | Create local user (if `VMCP_ALLOW_SELF_REGISTRATION=true`) |
| POST | `/api/login` | ❌ No | Authenticate local user, issue tokens, set cookies |
| POST | `/api/refresh` | ❌ No | Use refresh cookie to get new access token |
| POST | `/api/logout` | ❌ No | Clear cookies, rotate session nonce |
| GET | `/api/userinfo` | ✅ Yes | Get authenticated user info |
| POST | `/api/auth/ws-ticket` | ✅ Yes | Issue short-lived ticket for WebSocket upgrades |

### Request/Response Examples

**POST /api/login**
```json
// Request
{
  "username": "user@example.com or username",
  "password": "password123"
}

// Response
{
  "access_token": "eyJhbGc...",
  "token_type": "bearer",
  "expires_in": 900,
  "user": {
    "id": "1",
    "email": "user@example.com",
    "username": "username",
    "first_name": "John",
    "last_name": "Doe",
    "full_name": "John Doe",
    "is_active": true,
    "is_verified": true,
    "last_login": "2024-01-01T12:00:00Z",
    "created_at": "2024-01-01T00:00:00Z",
    "photo_url": null
  }
}

// Cookies set (HttpOnly, Secure, SameSite=Lax)
vmcp_access_token=<token>; max-age=900
vmcp_refresh_token=<token>; max-age=604800
```

---

## Token Types & TTLs

Configured in `backend/src/vmcp/config.py`:

```python
access_token_ttl_seconds: int = 900              # 15 minutes
refresh_token_ttl_seconds: int = 604800          # 7 days
websocket_ticket_ttl_seconds: int = 30           # 30 seconds
```

### JWT Payload Structure

All tokens are signed JWTs with these claims:
```json
{
  "sub": "1",                        // User ID
  "username": "user@example.com",
  "email": "user@example.com",
  "session_nonce": "abc123...",      // For server-side revocation
  "type": "access",                  // "access", "refresh", or "ws_ticket"
  "iat": 1704067200,                // Issued at
  "nbf": 1704067200,                // Not before
  "exp": 1704068100                 // Expiration
}
```

---

## MCP/WebSocket Authentication

**Resolution Order** (in `backend/src/vmcp/server/auth_service.py:resolve_request_token()`):

1. `Authorization: Bearer <token>` header
2. `vmcp_access_token` cookie
3. `ticket` query parameter (for WebSocket upgrades)

**Middleware Integration** (in `backend/src/vmcp/server/middleware.py:mcp_auth_middleware`):
- Extracts token via `resolve_request_token()`
- Validates via `LocalJWTService.extract_token_info()`
- Returns 401 with proper MCP protocol error if invalid
- Injects `_set_authorization_header()` for downstream handlers

---

## Frontend Authentication Flow

In `frontend/src/contexts/auth-context.tsx`:

### State Management
```typescript
interface AuthState {
  user: User | null;
  loading: boolean;
  error: string | null;
  isAuthenticated: boolean;
}
```

### Key Functions
- **login(email, password)**: POST /api/login, store access token, set auth header
- **logout()**: POST /api/logout, clear tokens & cookies, clear localStorage
- **refreshUser()**: GET /api/userinfo or POST /api/refresh
- **handleOAuthCallback()**: (For Stage 2) Processes OAuth redirect params

### Storage Strategy
- **Access token**: localStorage (available to JavaScript)
- **Refresh token**: HttpOnly cookie (secure, not accessible to JavaScript)
- **User data**: localStorage (as fallback cache)

**Token Resolution** (in `frontend/src/api/client.ts`):
```typescript
// ApiClient.setToken() injects Authorization header
Authorization: Bearer <access_token>
```

---

## Environment Variables (Stage 1)

### Required for Auth
```bash
VMCP_JWT_SECRET_KEY=<random-secret>        # MUST be set for production
VMCP_JWT_ALGORITHM=HS256                    # Default: HS256
VMCP_ACCESS_TOKEN_TTL_SECONDS=900           # 15 min
VMCP_REFRESH_TOKEN_TTL_SECONDS=604800       # 7 days
VMCP_WEBSOCKET_TICKET_TTL_SECONDS=30        # 30 sec
VMCP_AUTH_COOKIE_SECURE=false               # Set true for HTTPS
VMCP_AUTH_COOKIE_DOMAIN=                    # Optional domain override
VMCP_ALLOW_SELF_REGISTRATION=true           # Allow /api/register
VMCP_TRUSTED_PROXIES=127.0.0.1              # For X-Forwarded-* headers
VMCP_ALLOWED_HOSTS=localhost,127.0.0.1      # For Host header validation
```

---

## How to Build/Test/Lint

### Backend Commands

**Run Tests** (auth-specific):
```bash
cd backend
uv run pytest tests/test_local_auth_service.py --noconftest -v
```

**Lint Backend**:
```bash
cd backend
uv run ruff check src/vmcp/server/auth_service.py
uv run black --check src/vmcp/server/auth_service.py
uv run mypy src/vmcp/server/auth_service.py
```

**Format Backend**:
```bash
cd backend
uv run black src/vmcp/server/auth_service.py
uv run ruff check --fix src/vmcp/server/auth_service.py
```

**Run Full Backend**:
```bash
make run
# or
cd backend && uv run python -m vmcp.cli.main run
```

### Frontend Commands

**Run Tests**:
```bash
cd frontend
npm run test:run -- src/api/client.test.ts
npm run test:integration
```

**Lint Frontend**:
```bash
cd frontend
npm run lint
```

**Build Frontend**:
```bash
cd frontend
VITE_VMCP_OSS_BUILD=true npm run build
```

**Type Check**:
```bash
cd frontend
npx tsc --noEmit
```

### Integrated Commands

**Full Build** (Makefile):
```bash
make build-frontend   # Builds frontend, copies to backend
make run              # Starts full stack
make start-docker     # Docker build + compose
```

---

## Existing OAuth/Callback Code (For MCP Servers)

### ⚠️ **Not for User Authentication**

Located in `backend/src/vmcp/mcps/`:

**oauth_handler.py**:
- Route: `GET /api/otherservers/oauth/callback`
- Purpose: Handles OAuth callbacks for **MCP server** authentication (e.g., authenticating as a GitHub app)
- Does NOT authenticate users; instead configures OAuth state for external servers to connect

**oauth_state_manager.py**:
- Stores/retrieves OAuth state mappings (state token → user_id, server_name, config)
- Manages state expiration (1 hour TTL)
- Uses filesystem storage (`StorageBase()`)

### What This Does
```python
# Example: MCP server requesting OAuth (e.g., GitHub OAuth to access repos as vMCP)
1. Frontend initiates MCP server OAuth → backend creates state
2. User redirects to GitHub OAuth consent screen
3. GitHub redirects back to /api/otherservers/oauth/callback
4. Handler validates state, exchanges code for token
5. Stores access_token in MCPServer.oauth_state JSON field
6. MCP server now uses token to authenticate API calls
```

**This is NOT related to vMCP user login.** It's for provisioning MCP servers with OAuth capabilities.

---

## Likely Locations for Stage 2 (Google OAuth2 + OIDC)

### 1. **New Google/OIDC Auth Routes**
Create: `backend/src/vmcp/server/oauth_service.py`

```python
# OAuth2 endpoints
POST /api/oauth/authorize       # Redirect to Google consent
GET /api/oauth/callback         # Receive authorization code
POST /api/oauth/token           # Exchange code for tokens
POST /api/oauth/refresh         # Use refresh token to get new access token
```

**Related**: `backend/src/vmcp/server/auth_service.py` (keep local auth, add OAuth)

### 2. **OAuth State Management**
Create: `backend/src/vmcp/server/oauth_state.py`

- Store OAuth authorization code + PKCE state
- Not to be confused with `oauth_state_manager.py` (for MCP servers)
- Purpose: Prevent CSRF attacks during OAuth callback

### 3. **User Model Extension**
Modify: `backend/src/vmcp/storage/models.py`

```python
# Add to User class:
oauth_provider = Column(String(50), nullable=True)      # "google", "github", etc.
oauth_subject = Column(String(255), nullable=True)      # Provider's user ID
oauth_email_verified = Column(Boolean, default=False)
oauth_access_token = Column(String(2048), nullable=True)
oauth_refresh_token = Column(String(2048), nullable=True)
oauth_id_token = Column(String(2048), nullable=True)
oauth_expires_at = Column(DateTime, nullable=True)
oauth_metadata = Column(JSONType, nullable=True)
```

New migration: `migration_004_add_oauth_fields.py`

### 4. **Configuration**
Modify: `backend/src/vmcp/config.py`

```python
# Google OAuth
google_oauth_client_id: Optional[str] = Field(...)
google_oauth_client_secret: Optional[str] = Field(...)
google_oauth_redirect_uri: Optional[str] = Field(...)

# Generic OIDC
oidc_provider_url: Optional[str] = Field(...)
oidc_client_id: Optional[str] = Field(...)
oidc_client_secret: Optional[str] = Field(...)

# OAuth behavior
oauth_auto_register: bool = Field(default=True)  # Auto-create user on first OAuth
oauth_email_as_username: bool = Field(default=True)
```

### 5. **Frontend OAuth Callback**
Modify: `frontend/src/contexts/auth-context.tsx`

- Detect OAuth redirect params: `code`, `state`
- Exchange code for tokens (via new `/api/oauth/token`)
- Stored tokens & redirect to `/vmcp`
- Already has `handleOAuthCallback()` stub

Modify: `frontend/src/components/ui/GoogleAuthButton.tsx`

- Wire up to OAuth flow instead of being UI-only
- Construct redirect: `https://accounts.google.com/o/oauth2/v2/auth?client_id=...&redirect_uri=...`

### 6. **Dependencies**
Add to `backend/pyproject.toml`:
```toml
authlib>=1.2.0           # OAuth2/OIDC libraries
google-auth-oauthlib>=0.8.0  # Google-specific helpers (optional)
python-jose[cryptography]>=3.3.0  # Already in, for ID token validation
```

### 7. **Testing**
Create: `backend/tests/test_oauth_service.py`

- Test OAuth authorization flow
- Test token exchange
- Test user creation/linking
- Test PKCE validation
- Test ID token signature verification

---

## Potential Conflicts & Reusable Code

### ✅ Reusable Components

1. **JWT Validation**: `LocalJWTService` (in `dummy_jwt.py`)
   - Already validates tokens, can be extended to validate Google ID tokens
   - Use `jose.jwt.decode()` for ID token validation

2. **User Model & Session Nonce**:
   - Can link OAuth users to local users via `session_nonce` rotation
   - Use same nonce approach to revoke all tokens on password change

3. **Token Info Normalization**: `TokenInfo` dataclass (in `token_info.py`)
   - Extend to handle OAuth token types (bearer, id_token, etc.)
   - Support both local and OAuth auth uniformly

4. **Cookie Management**: `set_auth_cookies()`, `clear_auth_cookies()`
   - Reuse for OAuth login flow
   - Same HttpOnly, Secure, SameSite attributes

5. **MCP Middleware**: `mcp_auth_middleware` (in `middleware.py`)
   - Already accepts multiple token sources
   - Works seamlessly if OAuth tokens follow same Bearer format

6. **Frontend API Client**: `apiClient` (in `client.ts`)
   - Token injection already generic
   - Same login/logout/refresh pattern works

### ⚠️ Potential Conflicts

1. **Duplicate OAuth Handlers**:
   - `oauth_handler.py` (MCP servers) ≠ User OAuth
   - Keep separate: `backend/src/vmcp/server/oauth_service.py` (user OAuth)

2. **User ID Linking**:
   - Local users: `User.id` (integer, auto-increment)
   - OAuth users: Can't use provider's subject directly (not unique across providers)
   - Solution: Keep `User.id` as primary key, add `oauth_subject` foreign reference

3. **Email Conflict**:
   - Same email registered locally + via Google
   - Need unique constraint on `(oauth_provider, oauth_subject)` if allowing linking
   - Or separate unique constraint on `email` with provider-agnostic account linking

4. **Refresh Token Rotation**:
   - Current: `session_nonce` invalidates all tokens
   - OAuth: May need separate `oauth_token_generation` counter per provider
   - Avoid breaking existing local auth

5. **Logout Behavior**:
   - Local: Rotate session nonce (clears all tokens)
   - OAuth: Should also revoke refresh token with provider (separate call to Google API)
   - Implement graceful fallback if revocation fails

---

## Quick Integration Checklist for Stage 2

- [ ] Add OAuth config to `backend/src/vmcp/config.py`
- [ ] Extend `User` model with OAuth fields + new migration
- [ ] Create `backend/src/vmcp/server/oauth_service.py` with authorization/callback/token endpoints
- [ ] Create `backend/src/vmcp/server/oauth_state_manager_user.py` (separate from MCP OAuth state)
- [ ] Update `backend/src/vmcp/server/auth_service.py` to unify login responses
- [ ] Add Google/OIDC dependencies to `pyproject.toml`
- [ ] Wire up `frontend/src/contexts/auth-context.tsx` OAuth callback
- [ ] Update `frontend/src/components/ui/GoogleAuthButton.tsx` to trigger OAuth flow
- [ ] Add tests for OAuth flows
- [ ] Update `authentication.md` with Stage 2 notes

---

## Key Files to Watch During Implementation

1. **Backend Entry Point**: `backend/src/vmcp/server/vmcp_server.py` (router mounting)
2. **Auth Middleware**: `backend/src/vmcp/server/middleware.py` (request authentication)
3. **JWT Service**: `backend/src/vmcp/storage/dummy_jwt.py` (token validation)
4. **Frontend Context**: `frontend/src/contexts/auth-context.tsx` (state management)
5. **API Client**: `frontend/src/api/client.ts` (HTTP requests)

All imports of these should automatically pick up OAuth tokens once Stage 2 is integrated.

___BEGIN___COMMAND_DONE_MARKER___0
