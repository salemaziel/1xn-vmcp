# vMCP Backend

The backend server for vMCP (Virtual Model Context Protocol) - a FastAPI-based application that aggregates and manages multiple MCP servers with a unified interface.

## Architecture

### Core Components

```
backend/
├── src/
│   ├── main.py                    # Application entry point
│   └── vmcp/                      # Main package
│       ├── cli/                   # CLI commands (Typer)
│       ├── config.py              # Configuration management
│       ├── core/                  # Core services & registry
│       ├── mcps/                  # MCP server management
│       │   ├── mcp_client.py      # MCP client connections
│       │   ├── mcp_configmanager.py  # MCP server configuration
│       │   ├── mcp_auth_manager.py    # Authentication handling
│       │   ├── oauth_handler.py      # OAuth 2.0 flows
│       │   └── router_typesafe.py    # REST API routes
│       ├── proxy_server/          # MCP protocol server
│       │   ├── proxy_server.py    # FastMCP implementation
│       │   ├── middleware.py      # Request middleware
│       │   └── mcp_dependencies.py # Dependency injection
│       ├── storage/               # Database layer
│       │   ├── database.py        # Database initialization
│       │   ├── models.py          # SQLAlchemy models
│       │   ├── blob_service.py    # File/blob storage
│       │   └── migrations.py      # Alembic migrations
│       ├── utilities/             # Utilities
│       │   ├── logging/           # Logging configuration
│       │   └── tracing/           # OpenTelemetry tracing
│       └── vmcps/                 # Virtual MCP management
│           ├── vmcp_config_manager/  # Core vMCP engine
│           │   ├── config_core.py     # Main manager class
│           │   ├── execution_core.py  # Tool execution
│           │   ├── server_manager.py # MCP server coordination
│           │   ├── resource_manager.py # Resource handling
│           │   ├── protocol_handler.py # MCP protocol adapter
│           │   ├── template_parser.py  # Variable substitution
│           │   └── custom_tool_engines/ # Custom tool types
│           │       ├── python_tool.py
│           │       ├── http_tool.py
│           │       └── prompt_tool.py
│           ├── router_typesafe.py    # REST API routes
│           └── stats_router.py        # Usage statistics
├── tests/                         # Test suite
├── pyproject.toml                 # Project configuration
└── pytest.ini                     # Pytest configuration
```

### Data Flow

```
┌─────────────┐
│  AI Client  │ (Claude, Cursor, etc.)
└──────┬──────┘
       │ MCP Protocol (HTTP/SSE)
       ▼
┌─────────────────┐
│  Proxy Server   │ (FastMCP)
│  proxy_server.py│
└──────┬──────────┘
       │
       ├──► VMCPConfigManager
       │    ├──► Aggregates tools/resources/prompts
       │    ├──► Routes to MCP servers
       │    └──► Executes custom tools
       │
       ├──► MCPClientManager
       │    ├──► Manages connections to MCP servers
       │    └──► Handles HTTP/SSE transports
       │
       └──► Storage Layer
            ├──► SQLite
            └──► Configuration & stats
```

## Key Features

### 1. MCP Protocol Server

The `ProxyServer` class (in `proxy_server/proxy_server.py`) implements the MCP protocol using FastMCP:

- **Protocol Handlers**: `list_tools`, `call_tool`, `list_resources`, `read_resource`, `list_prompts`, `get_prompt`
- **Stateless Design**: Each request builds user context from headers
- **Tool Aggregation**: Combines tools from multiple MCP servers and custom tools
- **Resource Management**: Aggregates resources from connected servers
- **Prompt Handling**: Supports both MCP prompts and custom programmable prompts

### 2. vMCP Configuration Manager

The `VMCPConfigManager` (in `vmcps/vmcp_config_manager/`) is the core orchestration engine:

- **CRUD Operations**: Create, read, update, delete vMCP configurations
- **Capability Aggregation**: Combines tools, resources, and prompts from multiple MCP servers
- **Custom Tools**: Supports three types of custom tools:
  - **Python Tools**: Execute Python code snippets (sandboxed, 30s timeout)
  - **HTTP Tools**: Call REST APIs with authentication (Bearer, API Key, Basic, Custom)
  - **Prompt Tools**: Create programmable prompts that can invoke other tools
- **Variable Substitution**: Advanced template system with `@param`, `@config`, `@tool()`, `@resource`, `@prompt()` syntax

### 3. MCP Server Management

The `MCPConfigManager` and `MCPClient` handle connections to external MCP servers:

- **Transport Types**: Supports HTTP, and SSE transports
- **Authentication**: OAuth 2.0, Bearer tokens, API keys, Basic auth
- **Connection Management**: Automatic reconnection and session handling
- **Server Registry**: Preconfigured servers from community registry

### 4. Storage Layer

SQLAlchemy-based storage with support for SQLite:

- **Models**: Users, MCP servers, vMCPs, usage statistics, logs
- **Blob Storage**: File storage for custom tool code and resources

## Frontend Serving

The backend serves the frontend React application through FastAPI. The frontend uses React Router for client-side routing, allowing multiple pages while being served as a single application:

### Frontend Routes

- **`GET /`** - Redirects to `/app/vmcp`
- **`GET /app/`** - Serves the frontend application root (`index.html`)
- **`GET /app/{path}`** - Client-side routing (serves `index.html` for all routes, React Router handles navigation)
- **`GET /app/assets/{file_path}`** - Serves static assets (CSS, JS, images) with long-term caching

### Frontend Pages

The frontend includes multiple pages handled by React Router:
- `/app/vmcp` - vMCP list page
- `/app/vmcp/:id` - vMCP details page
- `/app/servers` - MCP servers management
- `/app/discover` - Server discovery
- `/app/stats` - Usage statistics
- `/app/settings` - Settings
- And more...

All routes are handled client-side by React Router, with FastAPI serving `index.html` for any `/app/*` route.

## Project Structure Details

### `vmcp/proxy_server/`

The MCP protocol server implementation:

- **`proxy_server.py`**: Main `ProxyServer` class extending FastMCP
  - Implements all MCP protocol handlers
  - Aggregates tools/resources/prompts from vMCPs
  - Handles user context and authentication
- **`middleware.py`**: Request middleware for routing and auth
- **`mcp_dependencies.py`**: Dependency injection for user context

### `vmcp/vmcps/vmcp_config_manager/`

The core vMCP engine with modular architecture:

- **`config_core.py`**: Main `VMCPConfigManager` class
  - Orchestrates all subsystems
  - Provides unified API for vMCP operations
- **`execution_core.py`**: Tool execution engine
  - Routes tool calls to appropriate handlers
  - Manages MCP server connections
  - Handles errors and timeouts
- **`server_manager.py`**: MCP server coordination
  - Manages connections to multiple MCP servers
  - Handles connection pooling and lifecycle
- **`resource_manager.py`**: Resource management
  - Aggregates resources from MCP servers
  - Handles resource templates
- **`protocol_handler.py`**: MCP protocol adapter
  - Converts between MCP types and internal types
- **`template_parser.py`**: Variable substitution engine
  - Parses `@param`, `@config`, `@tool()`, `@resource`, `@prompt()` syntax
  - Supports Jinja2 templates
- **`custom_tool_engines/`**: Custom tool implementations
  - **`python_tool.py`**: Python code execution (sandboxed)
  - **`http_tool.py`**: HTTP API calls with auth
  - **`prompt_tool.py`**: Programmable prompts

### `vmcp/mcps/`

MCP server management:

- **`mcp_client.py`**: MCP client implementation
  - Handles stdio/HTTP/SSE transports
  - Manages connections and sessions
- **`mcp_configmanager.py`**: Server configuration management
  - CRUD operations for MCP servers
  - Server registry management
- **`mcp_auth_manager.py`**: Authentication handling
  - OAuth 2.0 flows
  - Token management
- **`oauth_handler.py`**: OAuth callback handlers
- **`router_typesafe.py`**: REST API routes

### `vmcp/storage/`

Database layer:

- **`database.py`**: Database initialization and connection
- **`models.py`**: SQLAlchemy models
  - User, MCPServer, VMCP, VMCPEnvironment
  - Usage statistics and logs
- **`blob_service.py`**: File/blob storage

## Database

### Default: SQLite

By default, vMCP uses SQLite stored at `~/.vmcp/vmcp.db`:

- **Zero Configuration**: Works out of the box
- **Portable**: Single file database
- **Development**: Perfect for local development



## Logging

vMCP uses standard Python logging:

```python
from vmcp.utilities.logging import get_logger

logger = get_logger(__name__)
logger.info("Message")
logger.error("Error", exc_info=True)
```

**Log Levels**: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`

**Configuration**: Set via `VMCP_LOG_LEVEL` environment variable


## Security Considerations

### OSS Version

The backend now uses a **local authentication system** for browser and REST/MCP
access:

- **Local Auth**: Username/password login backed by the app database
- **JWTs**: Signed access, refresh, and short-lived MCP/WebSocket ticket tokens
- **Cookies**: Browser refresh/session continuity uses `HttpOnly` cookies
- **MCP Protection**: MCP requests require a valid bearer token, auth cookie, or
  short-lived `ticket`

For the implementation details, token lifecycle, environment variables, and
deployment notes, see [authentication.md](./authentication.md).

### MCP Server Authentication

vMCP also supports authentication for **upstream MCP servers**:

- **OAuth 2.0**: Full OAuth flow support
- **Bearer Tokens**: API key authentication
- **Basic Auth**: Username/password
- **Custom Headers**: Custom authentication schemes

## Troubleshooting

### Database Connection Issues

```bash
# Reset SQLite database (development only)
rm ~/.vmcp/vmcp.db

```

### Port Already in Use

```bash
# Use different port
vmcp run --port 8080

# Or set environment variable
VMCP_PORT=8080 vmcp run
```

## Contributing

See the main [CONTRIBUTING.md](../CONTRIBUTING.md) for guidelines.

### Backend-Specific Guidelines

1. **Type Hints**: All functions should have type hints
2. **Async/Await**: Use async functions for I/O operations
3. **Error Handling**: Use proper exception types from `vmcp.mcps.models`
4. **Logging**: Use structured logging with context
5. **Tests**: Write tests for new features in `tests/`

## License

MIT License - see [LICENSE](../LICENSE) file.

## Support

- 🐛 [Report Issues](https://github.com/1xn-labs/1xn-vmcp/issues)
- 📧 Email: contact@1xn.ai
- 📖 [Documentation](https://1xn.ai/docs)

---
