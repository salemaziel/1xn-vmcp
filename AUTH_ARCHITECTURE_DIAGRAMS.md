# Authentication Architecture Diagrams

## Stage 1: Local Authentication Flow

```
┌─────────────┐
│   Frontend  │
└──────┬──────┘
       │
       │ 1. User enters username/password
       │    POST /api/login
       ├─────────────────────────────────────────┐
       │                                         │
       ▼                                         ▼
┌──────────────────┐                   ┌─────────────────────┐
│  Login Form      │                   │  Backend            │
│  (React)         │                   │                     │
│                  │                   │ auth_service.py     │
└──────────────────┘                   │ ├─ authenticate_    │
       │                               │ │  credentials()    │
       │ 2. Validate password          │ ├─ build_tokens()  │
       │    against password_hash      │ ├─ set_auth_        │
       │                               │ │  cookies()        │
       │ 3. Issue JWT tokens           │ └─────────────────┘
       │    + Set cookies              │
       │<──────────────────────────────┤
       │                               │
       │ Response:                     │
       │ - access_token (JWT)          │
       │ - expires_in (900s)           │
       │ - user object                 │
       │                               │
       │ Cookies Set:                  │
       │ - vmcp_access_token (HttpOnly)
       │ - vmcp_refresh_token (HttpOnly)
       │                               │
       ▼                               ▼

┌─────────────────────────────────────────────┐
│ LocalStorage                                │
│ ├─ access_token (JWT)                       │
│ └─ user (User object)                       │
│                                             │
│ Cookies (HttpOnly, Secure)                  │
│ ├─ vmcp_access_token                        │
│ └─ vmcp_refresh_token                       │
└─────────────────────────────────────────────┘

       │
       │ 4. Subsequent API calls
       │    Authorization: Bearer <access_token>
       │
       ▼
┌──────────────────────┐
│ MCP/REST Endpoints   │
│                      │
│ middleware.py:       │
│ ├─ Check Bearer      │
│ ├─ Validate JWT      │
│ ├─ Check session_    │
│ │  nonce             │
│ └─ Inject user_ctx   │
└──────────────────────┘
```

---

## Token Lifecycle & Session Nonce

```
User Registration/First Login
│
├─ Create User in DB
│  ├─ id: 1 (auto-increment)
│  ├─ username: "user@example.com"
│  ├─ password_hash: "scrypt$...$..." (Scrypt)
│  └─ session_nonce: "abc123..." (random hex)
│
├─ Issue Tokens with Nonce
│  ├─ Access Token:
│  │  ├─ sub: "1"
│  │  ├─ session_nonce: "abc123..."
│  │  └─ exp: now + 900s
│  │
│  └─ Refresh Token:
│     ├─ sub: "1"
│     ├─ session_nonce: "abc123..."
│     └─ exp: now + 604800s
│
├─ Subsequent Access Token Uses
│  └─ Validate: token.session_nonce == db.User.session_nonce ✓
│
└─ Logout Event
   ├─ Rotate nonce: db.User.session_nonce = "xyz789..."
   ├─ All old tokens now invalid (mismatched nonce)
   └─ New tokens can only be issued with new nonce
```

---

## Request/Response Flow

```
BROWSER REQUEST WITH TOKEN
─────────────────────────────────────────

GET /api/userinfo
Header: Authorization: Bearer eyJhbGc...
Cookie: vmcp_access_token=eyJhbGc...

                          ▼

MIDDLEWARE RESOLUTION (auth_service.py:resolve_request_token)
─────────────────────────────────────────

1. Check Authorization header
   ├─ Found? Extract "Bearer <token>"
   ├─ Not found? Continue...
   │
2. Check vmcp_access_token cookie
   ├─ Found? Use cookie value
   ├─ Not found? Continue...
   │
3. Check ticket query param (?ticket=<token>)
   └─ Found? Use ticket

                          ▼

TOKEN VALIDATION (dummy_jwt.py:LocalJWTService)
─────────────────────────────────────────

├─ Decode JWT (verify signature with VMCP_JWT_SECRET_KEY)
├─ Check expiration (exp claim)
├─ Check token type (type claim in ["access", "ws_ticket"])
├─ Extract user_id, session_nonce from payload
├─ Query DB: User where id=user_id
├─ Verify: token.session_nonce == db.User.session_nonce
└─ Return: TokenInfo(user_id, username, email, ...)

                          ▼

SUCCESSFUL REQUEST
─────────────────────────────────────────

GET /api/userinfo
Inject request.user_context = UserContext(user_id=1, ...)

Endpoint executes:
  user = db.query(User).filter(User.id == 1).first()
  return {
    "id": "1",
    "email": "user@example.com",
    ...
  }

                          ▼

RESPONSE TO CLIENT
─────────────────────────────────────────

200 OK
{
  "id": "1",
  "email": "user@example.com",
  "username": "user",
  "first_name": "John",
  "last_name": "Doe",
  "is_active": true,
  "is_verified": true,
  "created_at": "2024-01-01T00:00:00Z",
  "last_login": "2024-01-15T12:00:00Z"
}
```

---

## WebSocket/MCP Authentication

```
BROWSER
├─ Has access_token in localStorage
├─ But can't send custom headers during WS upgrade
└─ Solution: Use ticket query parameter

       │
       │ 1. Request short-lived ticket
       │    POST /api/auth/ws-ticket
       │    Header: Authorization: Bearer <access_token>
       │
       ▼

BACKEND (auth_service.py:create_websocket_ticket)
├─ Validate access_token
├─ Create new JWT:
│  ├─ type: "ws_ticket"
│  ├─ exp: now + 30s (very short!)
│  └─ session_nonce: (same as user's nonce)
└─ Return: { ticket: "...", expires_in: 30 }

       │
       │ 2. Use ticket for WebSocket upgrade
       │    WS wss://server:8000/vmcp/mcp?ticket=<token>
       │
       ▼

WEBSOCKET UPGRADE
├─ Middleware resolves token from query param
├─ Validates token (checks type="ws_ticket", expiration)
├─ Creates WebSocket connection
└─ All subsequent MCP messages authenticated

       │
       │ 3. MCP Messages (within 30s window)
       │    - ListTools
       │    - CallTool
       │    - etc.
       │
       ▼

AFTER EXPIRATION
├─ Ticket expired, client must request new ticket
└─ (Prevents old captured tickets from being reused)
```

---

## Database Schema Relationships

```
┌─────────────────────────────────────────┐
│ User Table (users)                      │
├─────────────────────────────────────────┤
│ id (PK)              INT                 │
│ username             VARCHAR(50) UNIQUE  │
│ email                VARCHAR(255) UNIQUE │
│ first_name           VARCHAR(100)        │
│ last_name            VARCHAR(100)        │
│                                         │
│ *** Auth Fields (Stage 1) ***          │
│ password_hash        VARCHAR(512)  ✓    │
│ is_active            BOOLEAN            │
│ is_verified          BOOLEAN            │
│ session_nonce        VARCHAR(64)   ✓    │
│ last_login           DATETIME            │
│                                         │
│ *** OAuth Fields (Stage 2) ***         │
│ oauth_provider       VARCHAR(50)   ⏳   │
│ oauth_subject        VARCHAR(255)  ⏳   │
│ oauth_email_verified BOOLEAN       ⏳   │
│ oauth_access_token   VARCHAR(2048) ⏳   │
│ oauth_refresh_token  VARCHAR(2048) ⏳   │
│ oauth_id_token       VARCHAR(2048) ⏳   │
│ oauth_expires_at     DATETIME       ⏳   │
│ oauth_metadata       JSON           ⏳   │
│                                         │
│ created_at           DATETIME            │
│ updated_at           DATETIME            │
└─────────────────────────────────────────┘
      │
      └──────────────┬──────────────┬──────────────┐
                     │              │              │
                     ▼              ▼              ▼
        ┌────────────────┐ ┌────────────────┐ ┌────────────────┐
        │ MCPServer      │ │ VMCP           │ │ VMCPEnvironment│
        │ (FK: user_id)  │ │ (FK: user_id)  │ │ (FK: user_id)  │
        └────────────────┘ └────────────────┘ └────────────────┘
```

---

## Stage 1 vs Stage 2 Authentication

```
┌──────────────────────────────────────────────────────────────┐
│ STAGE 1: LOCAL AUTHENTICATION (✅ COMPLETE)                  │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│ Login Path:                                                  │
│   User enters username + password                            │
│   ├─ Validate against password_hash (Scrypt)               │
│   ├─ Issue access + refresh JWTs                           │
│   └─ Set HttpOnly cookies                                  │
│                                                              │
│ Token Flow:                                                  │
│   └─ Bearer token in header or cookie                      │
│                                                              │
│ User Identification:                                         │
│   └─ User.id (integer, auto-increment)                     │
│                                                              │
│ Scope: Local users only                                      │
│                                                              │
└──────────────────────────────────────────────────────────────┘

              ▼
              
┌──────────────────────────────────────────────────────────────┐
│ STAGE 2: OAUTH2 + OIDC (⏳ TODO)                            │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│ Login Path:                                                  │
│   User clicks "Login with Google"                            │
│   ├─ Redirect to Google consent screen                      │
│   ├─ Google returns authorization code                      │
│   ├─ Backend exchanges code for tokens (token endpoint)     │
│   ├─ Backend verifies ID token signature                    │
│   ├─ Create/link user (email-based matching)                │
│   └─ Issue vMCP access + refresh tokens                     │
│                                                              │
│ Token Flow:                                                  │
│   ├─ vMCP tokens: Bearer header or cookie (same as Stage 1)│
│   └─ Provider tokens: Stored in DB for future API calls    │
│                                                              │
│ User Identification:                                         │
│   ├─ Still User.id for vMCP (auto-increment)               │
│   └─ BUT linked via oauth_provider + oauth_subject         │
│       (Example: oauth_provider="google",                     │
│                oauth_subject="114849386800561234567")       │
│                                                              │
│ Scope: Local + Google + generic OIDC providers              │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

---

## MCP Server OAuth (DO NOT CONFUSE WITH USER OAUTH)

```
vMCP Application
│
├─ User Layer (Stage 1 & 2)
│  ├─ /api/login → authenticate user
│  ├─ /api/oauth/authorize → redirect to Google
│  └─ /api/oauth/callback → create/link user
│
└─ MCP Server Layer (Existing)
   ├─ /api/otherservers/oauth/callback → MCP server OAuth
   │
   │ Example Use Case:
   │   vMCP admin configures an MCP server that needs
   │   GitHub access to list repos.
   │
   │   Workflow:
   │   ├─ Backend: CREATE GitHub OAuth state
   │   ├─ User: Redirected to github.com/login/oauth/authorize
   │   ├─ GitHub: Returns authorization code
   │   ├─ Backend: Exchange code for GitHub token
   │   ├─ Backend: Store token in MCPServer.oauth_state JSON
   │   └─ MCP: Now uses token to call GitHub API as vMCP app
   │
   └─ oauth_state_manager.py → manages this state
```

**Key Difference**:
- **User OAuth**: Authenticates humans to vMCP
- **MCP Server OAuth**: Authenticates vMCP to external APIs on behalf of users

---

## Environment Variable Flow

```
Environment Variables (Deployment)
│
├─ VMCP_JWT_SECRET_KEY="super-secret-key"
│  └─ Used in: LocalJWTService to sign/verify tokens
│
├─ VMCP_ACCESS_TOKEN_TTL_SECONDS=900
│  └─ Used in: build_tokens() to set JWT expiration
│
├─ VMCP_REFRESH_TOKEN_TTL_SECONDS=604800
│  └─ Used in: build_tokens() for refresh token lifetime
│
├─ VMCP_AUTH_COOKIE_SECURE=true
│  └─ Used in: set_auth_cookies() to enforce HTTPS
│
├─ VMCP_AUTH_COOKIE_DOMAIN="example.com"
│  └─ Used in: set_auth_cookies() for domain restriction
│
├─ VMCP_ALLOW_SELF_REGISTRATION=true
│  └─ Used in: register_local_user() to enable/disable signup
│
├─ VMCP_TRUSTED_PROXIES="10.0.0.0/8"
│  └─ Used in: ProxyHeadersMiddleware for X-Forwarded-* headers
│
└─ VMCP_ALLOWED_HOSTS="api.example.com"
   └─ Used in: TrustedHostMiddleware to validate Host header

              ▼
              
app = FastAPI(...)
app.add_middleware(ProxyHeadersMiddleware, ...)
app.add_middleware(TrustedHostMiddleware, ...)
app.include_router(auth_router)  ← Uses env vars above

              ▼
              
settings = Settings()  ← Loads from environment
```

---

## File Dependency Graph

```
Frontend Entry
└─ App.tsx
   └─ AuthProvider (contexts/auth-context.tsx)
      ├─ useAuth() hook
      └─ apiClient (api/client.ts)
         ├─ client.setToken(token)
         └─ API calls with Authorization header

Backend Entry
└─ vmcp_server.py (FastAPI app)
   ├─ include_router(auth_router)  ← auth_service.py
   │  ├─ POST /api/login
   │  ├─ POST /api/register
   │  ├─ POST /api/refresh
   │  ├─ POST /api/logout
   │  ├─ GET /api/userinfo
   │  └─ POST /api/auth/ws-ticket
   │
   ├─ add_middleware(register_middleware)  ← middleware.py
   │  ├─ resolve_request_token()
   │  └─ Calls: LocalJWTService.extract_token_info()
   │
   ├─ Depends on: dummy_jwt.py
   │  └─ LocalJWTService
   │     ├─ create_token()
   │     ├─ decode_token()
   │     └─ extract_token_info()
   │
   ├─ Depends on: models.py (User)
   │  └─ Database schema
   │     ├─ password_hash (Scrypt)
   │     ├─ session_nonce
   │     └─ oauth_* fields (Stage 2)
   │
   ├─ Depends on: dummy_user.py
   │  └─ UserContext, get_user_context()
   │
   ├─ Depends on: token_info.py
   │  └─ TokenInfo dataclass
   │
   └─ Depends on: config.py
      └─ Settings (env vars)
```

