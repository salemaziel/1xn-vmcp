# Backend Authentication Notes

This document summarizes the Stage 1 authentication implementation that replaced
the previous dummy/mock auth path with a real local username/password flow.

## What Changed

The backend no longer relies on the old dummy token injection path for normal
operation. Instead, it now uses:

- local username/password authentication
- signed JWTs for access, refresh, and short-lived MCP/WebSocket tickets
- `HttpOnly` cookies for browser session continuity
- authenticated `UserContext` resolution for REST endpoints
- MCP middleware that accepts either a bearer token, an auth cookie, or a
  short-lived `ticket` query parameter

Stage 1 is intentionally limited to **local auth only**. Google OAuth and
broader OIDC provider support are still future work.

## Primary Files

- `backend/src/vmcp/server/auth_service.py`
  - local auth endpoints
  - password hashing and verification
  - cookie helpers
  - token resolution helpers
- `backend/src/vmcp/storage/dummy_jwt.py`
  - now provides `LocalJWTService`
  - validates signed JWTs against the database and `session_nonce`
- `backend/src/vmcp/storage/dummy_user.py`
  - now provides authenticated `UserContext`
  - keeps the old module path for compatibility with existing imports
- `backend/src/vmcp/server/middleware.py`
  - removed dummy bearer injection
  - resolves MCP auth from header, cookie, or short-lived ticket
- `backend/src/vmcp/server/vmcp_server.py`
  - mounts auth routes
  - applies proxy-aware middleware
  - protects `/api/config`
- `backend/src/vmcp/storage/models.py`
  - adds auth fields to `users`
- `backend/src/vmcp/storage/migrations.py`
  - adds migration 003 for the new user auth fields

## User Model Changes

The `users` table now includes:

- `password_hash`
- `is_active`
- `is_verified`
- `session_nonce`
- `last_login`

`session_nonce` is important because it allows server-side invalidation of all
existing tokens for a user without maintaining a separate token blacklist.

## Token Types

The backend currently issues three token types:

1. `access`
   - short-lived
   - used for REST requests and normal authenticated API calls
2. `refresh`
   - longer-lived
   - intended for browser session renewal
   - stored in an `HttpOnly` cookie
3. `ws_ticket`
   - very short-lived
   - used when browser clients need to authenticate MCP/WebSocket style access
     but cannot send custom auth headers during the upgrade flow

## Browser Session Model

The browser-facing flow is:

1. `POST /api/login`
2. backend validates credentials and sets:
   - `vmcp_access_token`
   - `vmcp_refresh_token`
3. frontend stores only the access token in `localStorage`
4. refresh token stays in an `HttpOnly` cookie
5. `POST /api/refresh` can mint a fresh access token using the refresh cookie
6. `POST /api/logout` rotates session state and clears cookies

This means refresh credentials are no longer exposed to JavaScript storage.

## Auth Endpoints

### `POST /api/register`

Creates a local user when `VMCP_ALLOW_SELF_REGISTRATION=true`.

### `POST /api/login`

Validates username/email + password and returns:

- `access_token`
- `expires_in`
- `user`

Cookies are set on the response for browser continuity.

### `POST /api/refresh`

Uses the refresh cookie to return a new access token and refreshed cookies.

### `POST /api/logout`

Clears auth cookies and rotates the user `session_nonce` when possible.

### `GET /api/userinfo`

Returns the authenticated user.

### `POST /api/auth/ws-ticket`

Returns a short-lived `ws_ticket` JWT for MCP/WebSocket/browser-constrained
clients.

## REST and MCP Authentication Resolution

### REST endpoints

Most REST routes already depend on `get_user_context`, so replacing the old
dummy user module automatically made those routes require a real authenticated
user.

### MCP endpoints

`mcp_auth_middleware` now authenticates MCP traffic in this order:

1. `Authorization: Bearer <token>`
2. `vmcp_access_token` cookie
3. `ticket` query parameter

If authentication is missing or invalid, MCP requests still return the expected
401 behavior with protected-resource metadata.

## Reverse Proxy Notes

The FastAPI app now applies:

- `ProxyHeadersMiddleware`
- `TrustedHostMiddleware`

Important settings:

- `VMCP_TRUSTED_PROXIES`
- `VMCP_ALLOWED_HOSTS`
- `VMCP_AUTH_COOKIE_SECURE`
- `VMCP_AUTH_COOKIE_DOMAIN`
- `VMCP_BASE_URL`

When deploying behind a reverse proxy, make sure the proxy forwards:

- `X-Forwarded-For`
- `X-Forwarded-Proto`
- `Host`

and that the proxy host/IP is trusted by `VMCP_TRUSTED_PROXIES`.

## Frontend Expectations

The frontend auth context now assumes:

- no automatic `local-token` fallback
- access token may exist in `localStorage`
- refresh happens through cookies, not `localStorage`
- logout must clear both local access-token state and server cookies

If a future agent reintroduces implicit `local-token` behavior in the frontend,
that would bypass the Stage 1 security model and should be treated as a
regression.

## Environment Variables

At minimum, local auth needs:

- `VMCP_JWT_SECRET_KEY`
- `VMCP_JWT_ALGORITHM`
- `VMCP_ACCESS_TOKEN_TTL_SECONDS`
- `VMCP_REFRESH_TOKEN_TTL_SECONDS`
- `VMCP_WEBSOCKET_TICKET_TTL_SECONDS`
- `VMCP_AUTH_COOKIE_SECURE`
- `VMCP_AUTH_COOKIE_DOMAIN`
- `VMCP_ALLOW_SELF_REGISTRATION`
- `VMCP_TRUSTED_PROXIES`
- `VMCP_ALLOWED_HOSTS`

The database must also be configured normally through `VMCP_DATABASE_URL`.

## Focused Validation Commands

These were the focused checks used while implementing Stage 1 auth:

```bash
# Focused backend auth helper tests
PYTHONPATH=/home/runner/work/1xn-vmcp/1xn-vmcp/backend/src \
pytest --noconftest backend/tests/test_local_auth_service.py

# Focused frontend API auth tests
cd /home/runner/work/1xn-vmcp/1xn-vmcp/frontend
npm run test:run -- src/api/client.test.ts

# Frontend build
VITE_VMCP_OSS_BUILD=true npm run build
```

## Important Follow-Up Work

Not part of Stage 1:

- external identity providers
- Google OAuth for end-user login
- general OIDC support
- refresh-token rotation beyond `session_nonce` invalidation
- dedicated auth UI for password reset / email verification

