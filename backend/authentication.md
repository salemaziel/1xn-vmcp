# Backend Authentication Notes

This document summarizes the authentication implementation that replaced the
previous dummy/mock auth path with real local username/password auth in Stage 1
and secure Google/OIDC login support in Stage 2.

## What Changed

The backend no longer relies on the old dummy token injection path for normal
operation. Instead, it now uses:

- local username/password authentication
- signed JWTs for access, refresh, and short-lived MCP/WebSocket tickets
- `HttpOnly` cookies for browser session continuity
- authenticated `UserContext` resolution for REST endpoints
- MCP middleware that accepts either a bearer token, an auth cookie, or a
  short-lived `ticket` query parameter

The current auth model supports:

- local username/password login
- Google OAuth2 login
- generic OpenID Connect login for enterprise IdPs

The browser callback flow is handled entirely server-side so the app no longer
needs to receive access or refresh tokens in the frontend callback URL.

## Primary Files

- `backend/src/vmcp/server/auth_service.py`
  - local auth endpoints
  - password hashing and verification
  - cookie helpers
  - token resolution helpers
- `backend/src/vmcp/server/oauth_service.py`
  - Google and generic OIDC login flows
  - PKCE + nonce generation
  - server-side callback handling
  - state-cookie and database-backed CSRF protection
  - secure account resolution/linking
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
  - stores linked OAuth accounts and login state records
- `backend/src/vmcp/storage/migrations.py`
  - adds migration 003 for the new user auth fields
  - adds migration 004 for OAuth account/state tables

## User Model Changes

The `users` table now includes:

- `password_hash`
- `is_active`
- `is_verified`
- `session_nonce`
- `last_login`

`session_nonce` is important because it allows server-side invalidation of all
existing tokens for a user without maintaining a separate token blacklist.

Stage 2 also adds:

- `user_oauth_accounts`
  - one row per linked provider identity
  - identifies users by `(provider, issuer, subject)`
  - stores email verification status and provider claims
- `oauth_login_states`
  - one row per in-flight browser login attempt
  - stores hashed `state`, PKCE verifier, nonce, requested username, return path,
    expiry, and single-use status

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

OAuth/OIDC logins ultimately mint the exact same local `access` and `refresh`
tokens after the callback succeeds, so the rest of the application continues to
use one unified session model.

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

## OAuth / OIDC Browser Login Model

The Stage 2 browser login flow is:

1. frontend redirects the browser to `GET /api/auth/oauth/{provider}/start`
2. backend loads provider metadata from discovery
3. backend creates:
   - a random `state`
   - a PKCE `code_verifier` / `code_challenge`
   - a random `nonce`
4. backend stores the login attempt in `oauth_login_states`
5. backend sets an `HttpOnly` `vmcp_oauth_state` cookie
6. backend redirects the browser to Google or the configured OIDC provider
7. provider redirects back to `GET /api/auth/oauth/{provider}/callback`
8. backend validates:
   - cookie `state` matches query `state`
   - database state exists, is unused, and is not expired
   - token exchange succeeds
   - ID token signature is valid against provider JWKS
   - ID token `nonce` matches the stored nonce
9. backend resolves or links the local user account
10. backend sets the normal local auth cookies and redirects back to the SPA

No local auth token is placed in the callback URL.

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

### `GET /api/auth/oauth/providers`

Returns which interactive login providers are enabled for the frontend login
page.

### `GET /api/auth/oauth/google/start`

Starts the Google login flow.

### `GET /api/auth/oauth/google/callback`

Handles the Google callback, creates/links the local user, sets session cookies,
and redirects back to the SPA.

### `GET /api/auth/oauth/oidc/start`

Starts the generic OIDC login flow using the configured discovery document.

### `GET /api/auth/oauth/oidc/callback`

Handles the generic OIDC callback with the same cookie/state/nonce protections.

## Account Resolution Rules

Stage 2 intentionally uses conservative account-linking rules:

- if `(provider, issuer, subject)` is already linked, sign in as that user
- if no link exists but a local user with the same email exists:
  - link automatically **only** when the provider explicitly reports
    `email_verified=true`
  - reject the login when the provider email is unverified or missing
- if no user exists yet:
  - create a new local user **only** when the provider email is verified
  - generate a unique username from the requested username, provider username,
    or email local-part

This is important because auto-linking an unverified provider email could allow
account takeover against a pre-existing local account.

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
- OAuth/OIDC callbacks land on `/app/oauth/callback/success`
- the callback page calls `/api/refresh` to establish frontend auth state from
  the server-set cookies
- provider buttons are discovered from `/api/auth/oauth/providers`

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
- `VMCP_OAUTH_STATE_TTL_SECONDS`
- `VMCP_OAUTH_CALLBACK_FRONTEND_PATH`
- `VMCP_GOOGLE_OAUTH_CLIENT_ID`
- `VMCP_GOOGLE_OAUTH_CLIENT_SECRET`
- `VMCP_GOOGLE_OAUTH_DISCOVERY_URL`
- `VMCP_GOOGLE_OAUTH_SCOPE`
- `VMCP_OIDC_CLIENT_ID`
- `VMCP_OIDC_CLIENT_SECRET`
- `VMCP_OIDC_DISCOVERY_URL`
- `VMCP_OIDC_SCOPE`
- `VMCP_OIDC_PROVIDER_NAME`

The database must also be configured normally through `VMCP_DATABASE_URL`.

## Focused Validation Commands

These were the focused checks used while implementing the current auth system:

```bash
# Focused backend auth helper tests
PYTHONPATH=/home/runner/work/1xn-vmcp/1xn-vmcp/backend/src \
pytest --noconftest backend/tests/test_local_auth_service.py

# Focused backend OAuth/OIDC tests
PYTHONPATH=/home/runner/work/1xn-vmcp/1xn-vmcp/backend/src \
pytest --noconftest backend/tests/test_oauth_service.py

# Focused frontend API auth tests
cd /home/runner/work/1xn-vmcp/1xn-vmcp/frontend
npm run test:run -- src/api/client.test.ts

# Frontend build
VITE_VMCP_OSS_BUILD=true npm run build
```

## Important Follow-Up Work

Still not part of the current implementation:

- multiple generic OIDC providers at once
- manual account-link / unlink UI inside the app
- refresh-token rotation beyond `session_nonce` invalidation
- dedicated auth UI for password reset / email verification
- MFA / step-up auth policies
