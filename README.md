# owui-mcp-spawner

**OWUI MCP Spawner** — a local-first tool that turns OpenWebUI-compatible tool definitions into standalone [Model Context Protocol](https://modelcontextprotocol.io) servers, each spawned in its own isolated virtual environment.

Drop in a tool (an OpenWebUI JSON export or plain Python), and the spawner installs its dependencies, validates it, and brings up a dedicated MCP server reachable over Streamable HTTP — ready for OpenWebUI, Claude Code, Codex, or any MCP client. Because every instance gets its own venv, tools with conflicting dependencies never clash, and nothing pollutes the spawner process itself.

> A maintained OpenHAB integration that exposes your smart home as a local MCP server is available separately: [openhab-ai-integration](https://github.com/torfeu/openhab-ai-integration).

![OWUI MCP Spawner UI](screen.png)

---

## Quickstart

```bash
git clone https://github.com/torfeu/owui-mcp-spawner.git
cd owui-mcp-spawner

# Python 3.10+ required
python3 -m venv .venv
.venv/bin/pip install fastapi "uvicorn[standard]" pydantic python-multipart mcp packaging starlette httpx

# Start (localhost only, no auth needed)
.venv/bin/python app/manager.py

# Open the web UI
open http://127.0.0.1:7860
```

---

## Network access (e.g. for OpenWebUI on another machine)

```bash
# Always set a password before binding to a network interface
export MCP_MANAGER_PASSWORD=changeme
.venv/bin/python app/manager.py --host 0.0.0.0
```

MCP endpoints are then reachable at `http://<your-ip>:<port>/mcp`.

> **Security note:** Never bind to `0.0.0.0` without setting `MCP_MANAGER_PASSWORD`.
> The spawner warns at startup if you do.

---

## Settings page

The web UI includes a **⚙ Settings** page (top-right button) for managing common runtime options:

- **Password** — set or change the password (requires current password if one is already set); persisted as SHA-256 hash in `runtime/settings.json`
- **Edit mode** — switch between full / upload-only / readonly at runtime
- **MCP Bearer Token** — set, reveal (👁), generate (⟳), or remove the token that protects all MCP endpoints; stored in `runtime/settings.json`
- **Virtual Environments** — list venvs with their instance counts, create a new venv, or delete an unused one (in-use venvs are protected; the `default` venv cannot be deleted)
- **Restart** — restart the spawner process from the UI

All settings survive restarts. CLI flags always take precedence over saved settings.

---

## Virtual environments

Every instance runs in its own Python virtual environment under `runtime/venvs/<name>/`, so a tool's third-party dependencies are fully isolated — conflicting versions across tools no longer collide, and nothing pollutes the spawner process itself.

- Instances default to the **`default`** venv, which is created on first use with the base packages the runner needs (`mcp`, `uvicorn`, `starlette`, `pydantic`, `httpx`).
- A venv is **created on demand**: assigning an instance to a new venv name (or creating a tool with one) builds it automatically. You can also create/delete venvs explicitly on the Settings page.
- Validation, dependency installs and the runtime all use the instance's venv interpreter, so an import-time check sees exactly the packages the tool will have at runtime.
- The dashboard shows each instance's venv in a **Venv** column; the **Edit** dialog has a venv dropdown to move an instance (deps are reinstalled into the target venv and the instance restarts if running).

> **Upgrading from ≤ v0.0.6:** on the first start, existing instances' dependencies are installed into the `default` venv once (a one-time migration, guarded by `runtime/.venv_migrated`). The first start therefore takes longer and needs network access for pip. Already-running instances keep using the old interpreter until restarted.

---

## Edit modes

Three levels control what the web UI allows. Use the flag that matches your trust level:

| Flag | Mode | Allowed | Blocked |
|---|---|---|---|
| *(none)* | **full** | everything | — |
| `--no-code-edit` | **upload-only** | start/stop/restart, logs, reinstall, upload JSON, config edit, delete | inline code editor (New Tool, Edit Code) |
| `--no-edit` | **readonly** | start/stop/restart, logs, reinstall | upload, code editor, config edit, delete |

Use `--no-edit` on a server where you want to prevent anyone from injecting arbitrary Python code through the web interface. `--no-code-edit` is a middle ground: operators can still install pre-built tool JSONs but cannot write or modify Python code directly.

The current mode is reported at startup:
```
OWUI MCP Spawner starting on http://0.0.0.0:7860  [auth: enabled, edit: upload-only, mcp-auth: bearer-token]
```

Both flags also enforce their restrictions at the API level — the corresponding routes return `403` even if someone bypasses the UI.

Edit mode can also be changed at runtime via the Settings page and persists across restarts.

---

## Instance locking

Each instance can be locked with the 🔒 button in the dashboard. A locked instance can still be **started and stopped** (a misbehaving instance must always be stoppable), and its logs and export stay accessible — but config edits, code edits, restart, reinstall and delete return `403` until it is unlocked. Locking is meant as a guard against accidental changes to production instances, especially when an AI agent manages the spawner via the control tool.

---

## MCP endpoint authentication

By default MCP endpoints are open. To require a Bearer token on all MCP endpoints:

```bash
# Set at startup (overrides saved setting)
.venv/bin/python app/manager.py --host 0.0.0.0 --mcp-token mysecrettoken123

# Lock so the token cannot be changed via the web UI
.venv/bin/python app/manager.py --host 0.0.0.0 --mcp-token mysecrettoken123 --no-token-edit
```

The token can also be set, revealed, and regenerated in the Settings page (unless `--no-token-edit` is active).

**After changing the token, restart each MCP server** — runners read the token at startup.

In OpenWebUI, set the token as Bearer token when adding the MCP connection. In Claude Code, add it to `~/.claude/mcp.json`:

```json
{
  "mcpServers": {
    "my-tool": {
      "url": "http://<your-server-ip>:8104/mcp",
      "headers": { "Authorization": "Bearer mysecrettoken123" }
    }
  }
}
```

---

## Authentication

| Variable | Description |
|---|---|
| `MCP_MANAGER_PASSWORD` | Plain-text password — hashed with SHA-256 at startup |
| `MCP_MANAGER_PASSWORD_HASH` | Pre-hashed SHA-256 hex digest (takes precedence) |
| `MCP_BEARER_TOKEN` | Bearer token for MCP endpoints when starting runner/server components directly. With `app/manager.py`, use `--mcp-token` or the Settings page. |

When auth is active:
- The web UI shows a login screen. The password is verified against a protected endpoint (`GET /api/auth-check`) — wrong passwords are rejected immediately.
- All mutating and sensitive API routes require a `Bearer` token.
- Auth is initialized at module import time, so it is also active when starting directly via `uvicorn app.admin_server:app`.

### Protected routes (require Bearer token)

| Method | Route | Description |
|---|---|---|
| `GET` | `/api/auth-check` | Token validation endpoint |
| `GET` | `/api/settings` | Spawner settings (auth, edit mode, MCP token status) |
| `PUT` | `/api/settings` | Update settings |
| `GET` | `/api/settings/mcp-token` | Retrieve current MCP token value *(blocked by `--no-token-edit`)* |
| `POST` | `/api/server/restart` | Restart the spawner process |
| `GET` | `/api/instances/{id}/config` | Full config including values |
| `GET` | `/api/instances/{id}/tool-code` | Python source of a tool *(blocked by `--no-code-edit`)* |
| `GET` | `/api/instances/{id}/logs/install` | Install log |
| `GET` | `/api/instances/{id}/logs/runtime` | Runtime log |
| `POST` | `/api/instances/upload` | Upload & install a new tool; accepts optional `venv` and `port` form fields *(blocked by `--no-edit`)* |
| `POST` | `/api/tools/create` | Create & install a new tool from raw Python code in one step (installs deps, validates in the venv, fills values) *(blocked by `--no-edit`)* |
| `PUT` | `/api/instances/{id}` | Edit config — `name`, `server`, `values`, `install.dependencies`, `lifecycle`, `venv` (moving venv reinstalls deps + restarts). `values` entries equal to the secret mask `********` are ignored, so echoing back a fetched config never overwrites real secrets *(blocked by `--no-edit`)* |
| `PUT` | `/api/instances/{id}/tool-code` | Save edited tool code; installs newly declared `requirements:` and syncs Valve values *(blocked by `--no-code-edit`)* |
| `GET` | `/api/venvs` | List virtual environments with instance counts |
| `POST` | `/api/venvs` | Create a virtual environment *(blocked by `--no-edit`)* |
| `DELETE` | `/api/venvs/{name}` | Delete an unused venv (refused if in use; `default` protected) *(blocked by `--no-edit`)* |
| `POST` | `/api/instances/{id}/start` | Start |
| `POST` | `/api/instances/{id}/stop` | Stop (allowed even when locked) |
| `POST` | `/api/instances/{id}/restart` | Restart |
| `POST` | `/api/instances/{id}/reinstall` | Reinstall dependencies |
| `POST` | `/api/instances/{id}/lock` | Lock the instance (blocks modifications, see *Instance locking*) |
| `POST` | `/api/instances/{id}/unlock` | Unlock the instance |
| `GET` | `/api/instances/{id}/export` | Download an OpenWebUI MCP-connection JSON for this instance |
| `POST` | `/api/tools/validate` | Validate tool code; pass an optional `instance_id` to validate in that instance's venv so its installed dependencies resolve *(blocked by `--no-code-edit`)* |
| `POST` | `/api/tools/export` | Export tool as OpenWebUI JSON *(blocked by `--no-code-edit`)* |
| `DELETE` | `/api/instances/{id}` | Delete *(blocked by `--no-edit`)* |

### Open routes (no auth required)

| Method | Route | Description |
|---|---|---|
| `GET` | `/api/auth-status` | Returns `{"auth_enabled": bool, "edit_mode": "full"\|"upload"\|"readonly"}` |
| `GET` | `/api/instances` | Instance list (status, URLs) |
| `GET` | `/api/instances/{id}` | Single instance status and URL |
| `GET` | `/api/tools/template` | Starter template for the editor |

---

## Writing a tool

Every tool is a Python file with a `Tools` class. The spawner reads it, installs dependencies, and exposes each method as an MCP tool.

### Minimal structure

```python
"""
title: My Tool
description: What this tool does
author: Your Name
version: 0.1.0
"""

class Tools:
    def my_function(self, param: str) -> str:
        """Short description shown in the MCP schema.

        Args:
            param: Description of this parameter
        """
        return f"Result: {param}"
```

### With configuration (Valves)

```python
from pydantic import BaseModel, Field

class Tools:
    class Valves(BaseModel):
        api_url: str = Field(default="https://api.example.com", description="Base API URL")
        api_key: str = Field(default="", description="API key")

    def __init__(self):
        self.valves = self.Valves()

    def fetch(self, query: str) -> str:
        """Fetch data from the configured API.

        Args:
            query: The search term
        """
        # use self.valves.api_url and self.valves.api_key
        return f"Result for {query}"
```

Valve fields become configurable values in the web UI (Edit → Values).

### Dependencies

Declare third-party packages with a `requirements:` line in the module docstring — they are installed into the instance's venv before the code is validated, so heavy imports (numpy, pandas, yfinance, …) work on the very first upload:

```python
"""
title: Stock Analysis
requirements: yfinance, pandas, numpy
"""
import yfinance, pandas, numpy
```

You can also pass dependencies explicitly to `create_tool`, or edit them later via **Edit → Dependencies** (or `update_instance_dependencies`). Changing dependencies reinstalls them into the instance's venv.

### Schema features

| Python | MCP schema |
|---|---|
| `Literal["a", "b"]` | `"enum": ["a", "b"]` |
| `Annotated[str, Field(description="...")]` | `"description": "..."` |
| `Args:` docstring section | `"description"` per parameter |
| Default values | `"default"` in schema |
| `Optional[T]` | field not required |

### JSON upload format

When uploading a tool (via the web UI, `upload_tool()`, or direct API), the JSON must be an **array** containing one object with these fields:

```json
[
  {
    "id": "my_tool",
    "user_id": "00000000-0000-0000-0000-000000000000",
    "name": "My Tool",
    "meta": {
      "description": "Short description shown in the UI and to the AI",
      "manifest": {
        "title": "My Tool",
        "author": "Your Name",
        "version": "0.1.0"
      }
    },
    "specs": [
      {
        "name": "my_function",
        "description": "What this function does",
        "parameters": {
          "type": "object",
          "properties": {
            "param": { "type": "string", "description": "Description" }
          },
          "required": ["param"]
        }
      }
    ],
    "content": "\"\"\"\\ntitle: My Tool\\n...\\n\"\"\"\\n\\nclass Tools:\\n    ..."
  }
]
```

| Field | Required | Description |
|---|---|---|
| `id` | yes | Unique snake_case ID — becomes the MCP server ID and URL path |
| `user_id` | yes | Always `"00000000-0000-0000-0000-000000000000"` |
| `name` | yes | Display name shown in the UI |
| `meta.description` | yes | Short description for the UI card |
| `meta.manifest` | yes | `title`, `author`, `version` |
| `specs` | yes | List of tool method definitions (name, description, JSON Schema parameters) |
| `content` | yes | Complete Python source code as a string (use `\n` for newlines) |

The `specs` entries must match the public methods of your `Tools` class. One entry per method.

### Upload

1. Write code in the **Tool Editor** → **Install as MCP** (one step: installs deps, validates in the venv, fills values)
2. Or upload an OpenWebUI JSON export via **+ Upload JSON** in the web UI (with optional venv/port)
3. Or use the bundled control tool (`mcp_manager_control`): `create_tool(code, id, …)` for raw code, or `upload_tool(json_content)` for a JSON export

Call `get_tool_template()` on the control tool to get both the Python template and a complete JSON format example at runtime.

---

## Tool Editor

The built-in editor lets you write, validate, and export OpenWebUI-compatible tool JSONs directly in the browser:

- Python syntax highlighting (CodeMirror)
- Live validation: syntax check, runtime inspection, type-hint → JSON Schema generation
- **Install as MCP** — create a new instance in one step (pick a venv and optional port; deps install and validation run in that venv)
- Export as `.json` (importable into OpenWebUI)
- **Edit Code** on any existing MCP server to modify it in-place

> The editor is hidden automatically when `--no-code-edit` or `--no-edit` is active.

### Schema generation

The spawner inspects the actual Python code to build accurate MCP tool schemas:

- `typing.Literal["a", "b"]` → `"enum": ["a", "b"]` in the MCP schema
- `Annotated[T, Field(description="...")]` → `"description"` per parameter
- Google-style `Args:` docstring section → `"description"` per parameter (fallback)
- Default values → `"default"` in the schema
- `Optional[Literal[...]]` is unwrapped correctly

---

## Managing the spawner over MCP (control tool)

`owui-mcp-spawner` ships with a **control tool** — an MCP server that lets an AI agent (Claude Code, OpenWebUI, …) operate the spawner itself: list, start, stop, restart and reinstall instances, read install/runtime logs, create and edit tools, change Valve values, dependencies or venvs, and even restart the spawner. It's the same interface used throughout this project to manage a remote deployment without opening the web UI.

The definition lives in `examples/mcp-manager-control.json` (instance id `mcp_manager_control`). Set it up like any other tool, then point your MCP client at it:

1. Upload `examples/mcp-manager-control.json` (or paste it into the editor) to create and install the `mcp_manager_control` instance, then start it.
2. In your client add it as an MCP server at `http://<host>:<port>/mcp`, with the Bearer token if MCP auth is enabled.

**Available tools (22):** `list_instances`, `get_instance`, `get_instance_config`, `get_settings`, `get_install_log`, `get_runtime_log`, `get_tool_code`, `get_tool_template`, `start_instance`, `stop_instance`, `restart_instance`, `reinstall_instance`, `restart_manager`, `create_tool`, `upload_tool`, `save_tool_code`, `validate_tool_code`, `export_tool`, `update_instance_values`, `update_instance_dependencies`, `update_instance_venv`, `delete_instance`.

**Security — enforced server-side, granular per action.** The control instance carries one `allow_*` Valve per action (`allow_delete`, `allow_create_tool`, `allow_restart`, `allow_manager_restart`, …). Read-only actions are on by default; destructive ones (e.g. `delete_instance`) stay disabled until you flip their Valve in **Edit → Values** and restart the instance — so an agent can never do more than you've allowed. Write operations also need the spawner password, supplied to the control instance via the `auth_token` Valve; `manager_url` points it at the spawner API. The global edit mode (`--no-edit` / `--no-code-edit`) still applies on top.

---

## systemd (optional)

Example files are in `deploy/`. Never put secrets directly into the service file.

**1. Create the environment file**

```bash
sudo cp deploy/owui-mcp-spawner.env.example /etc/owui-mcp-spawner.env
sudo chmod 600 /etc/owui-mcp-spawner.env
sudo nano /etc/owui-mcp-spawner.env   # set MCP_MANAGER_PASSWORD and optionally MCP_BEARER_TOKEN
```

**2. Create the service file**

```bash
sudo cp deploy/owui-mcp-spawner.service.example /etc/systemd/system/owui-mcp-spawner.service
sudo nano /etc/systemd/system/owui-mcp-spawner.service
# Replace YOUR_USER and adjust WorkingDirectory / ExecStart to your actual paths
# Add --no-edit, --no-code-edit, --mcp-token, --no-token-edit to ExecStart as needed
```

**3. Enable and start**

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now owui-mcp-spawner
sudo journalctl -u owui-mcp-spawner -f
```

---

## Config format

See `configs/example.json` for a full template. Configs live in `configs/` — one JSON file per MCP server.

---

## Project layout

```
app/
  manager.py            Entry point (CLI) — --host, --no-edit, --no-code-edit, --mcp-token, --no-token-edit
  admin_server.py       FastAPI admin API + web server (+ instance watchdog)
  mcp_runner.py         Single MCP subprocess (Streamable HTTP + optional Bearer token auth)
  auth.py               Auth, edit mode, MCP token helpers
  settings_store.py     Persistent settings (runtime/settings.json)
  config_store.py       Config file I/O + port management
  process_manager.py    Subprocess lifecycle (start/stop/restart) + health checks
  tool_loader.py        OpenWebUI JSON → MCP tool definitions
  tool_editor.py        Code validation (isolated subprocess) + OpenWebUI JSON export
  validate_worker.py    Subprocess worker that executes untrusted tool code for validation
  schema_gen.py         Shared type-hint/docstring → JSON Schema generation
  dependency_manager.py pip install handling (into the instance venv)
  venv_manager.py       On-demand per-instance virtual environments (runtime/venvs/)
  schema.py             Pydantic models
  security.py           Package validation, secret masking
  logger.py             Logging setup (rotating log)
configs/                Per-server JSON configs (one file = one MCP)
tools/                  Uploaded OpenWebUI tool JSONs
examples/               Example tool JSON (control tool — manage the spawner via MCP)
web/                    Frontend (HTML + JS + CSS)
runtime/                PIDs + logs + venvs + settings.json + tool-code history (gitignored)
deploy/                 systemd service + env file examples
```

---

## Changelog

### v0.1.1
- **Secrets survive config round-trips** — `PUT /api/instances/{id}` ignores values equal to the `********` mask, so a client that fetches a config and echoes it back (e.g. the control tool's `update_instance_values`) no longer overwrites real API keys with the mask
- **Non-blocking delete** — deleting a running instance stops it in a worker thread instead of blocking the event loop (and every other request) for up to ~5 s
- **Locked instances can be stopped again** — reverts the v0.0.6 restriction: lock means "don't modify", but a misbehaving instance must always be stoppable. Config edit, code edit, restart and delete remain blocked while locked
- **Honest upload errors** — uploading an MCP config whose dependencies fail to install returns `422` with the error (the instance is kept in `dependency_error` for Reinstall) instead of reporting success
- **Validation in the instance venv** — `POST /api/tools/validate` accepts an optional `instance_id` and validates in that instance's venv; the editor passes it automatically when editing, so already-installed third-party imports no longer false-fail
- **pip timeout raised to 600 s per package** (was 120 s) — large wheels like `torch` no longer fail on principle
- **Version badge fix** — the `version:` regex is anchored to line start, so a "version: …" inside a description line no longer wins
- **Control tool v0.0.5** — `manager_url` tolerates a trailing slash; install-heavy calls use 600 s timeouts (`update_instance_venv` previously timed out after 10 s); `validate_tool_code` gains an optional `instance_id`
- **Dead-code cleanup** — unused imports and attributes removed, orphaned status-badge CSS dropped, missing `.alert-warning` style added

### v0.1.0
- **Renamed to `owui-mcp-spawner`** (formerly "MCP Framework" / "MCP Manager") — the focus is spawning OpenWebUI tools as isolated MCP servers. Environment variable names (`MCP_MANAGER_PASSWORD`, …) are unchanged for compatibility.
- **Per-instance virtual environments** — every instance runs in its own venv under `runtime/venvs/<name>/`; dependencies are fully isolated and never pollute the spawner. Created on demand with base packages; validation, installs and runtime all use the instance venv. New `app/venv_manager.py`, `MCPConfig.venv` field, and a one-time migration of existing instances on first start.
- **Dependencies actually install from `requirements:`** — the docstring `requirements:` line is parsed and installed *before* validation, so tools importing numpy/pandas/yfinance/… upload in one go (previously rejected with `ModuleNotFoundError`).
- **One-step `create_tool`** — `POST /api/tools/create` and the editor's **Install as MCP** create an instance from raw Python in a single step (install → validate in venv → fill Valve values → save); no more placeholder + export + upload dance.
- **Valve values sync on code save** — saving edited code merges new Valve defaults into the editable config (keeps user-set values, drops removed valves), so valves stay editable in the UI after a code change.
- **Safe delete** — deleting an instance no longer removes a tool file still referenced by another instance (reference-counted); MCP-config uploads copy into an instance-owned file instead of sharing.
- **Venv management UI** — dashboard **Venv** column, venv dropdowns in the upload/editor/edit dialogs, and a Settings section to create/delete venvs (in-use protection, `default` protected).
- **Port choice at install** — optional fixed port on upload / `create_tool`; a clash returns `409` instead of being silently reassigned.
- **Control tool v0.0.4+** — new tools: `create_tool`, `update_instance_values`, `update_instance_dependencies`, `update_instance_venv` (value changes report a hint to ask before restarting).

### v0.0.6
- **Isolated validation** — tool code is validated in a short-lived subprocess with a timeout; import-time side effects, crashes or endless loops can no longer affect the spawner process
- **Non-blocking API** — dependency installs, instance start/stop/restart and validation run in worker threads; the UI stays responsive during long operations
- **Health checks** — `start` waits until the instance actually answers on its port (with log tail as error detail on failure); a watchdog marks instances whose process died as `failed`
- **Log rotation** — rotating log, oversized runtime logs rotate on instance start, log endpoints return the last 500 lines instead of the whole file
- **Tool-code history** — the previous version of a tool JSON is snapshotted to `runtime/history/<id>/` before every save (last 10 kept)
- **Unified schema generation** — validation, export and runtime now share one implementation (`schema_gen.py`); the editor gains `Literal` → enum and `Annotated`/`Field` descriptions
- **Hardening** — locked instances can no longer be stopped; timing-safe token comparison (`hmac.compare_digest`); no more `exec()` of uploaded code in the spawner process
- **Cleanup** — removed dead config fields (`enabled`, `runtime`, `requirements_file`, `install_on_upload`) and unused status values; the control tool (v0.0.2: precise 403 error details, fixed `export_tool`) now lives in `examples/`

### v0.0.5
- **Settings page** — ⚙ button in the web UI for managing password, edit mode, MCP token and restart; all settings persisted in `runtime/settings.json`
- **MCP Bearer Token auth** — optional token protecting all MCP endpoints; `--mcp-token` and `--no-token-edit` CLI flags; token visible/hidden with 👁 toggle and ⟳ generator in the UI
- **Improved schema generation** — `Literal[...]` → `enum`, `Annotated[T, Field(description=...)]` and Google-style `Args:` docstrings → `description`, defaults → `default`; built from live Python code, not the pre-built specs

### v0.0.4
- Three edit modes: `--no-code-edit` (upload-only) and `--no-edit` (readonly) — enforced at API and UI level
- Edit mode reported at startup and exposed via `/api/auth-status`

### v0.0.3
- systemd deploy examples
- Password via `EnvironmentFile` — no secrets in the service file

### v0.0.2
- Bearer-token authentication (SHA-256, set via `MCP_MANAGER_PASSWORD`)
- Login modal — verified against protected `/api/auth-check` endpoint
- Config, tool-code, and log routes require auth
- Auth initializes at module import time

### v0.0.1
- Initial release: OpenWebUI JSON → MCP server, web UI, auto-start, Streamable HTTP transport

---

## Credits

- [CodeMirror](https://codemirror.net) — MIT — in-browser code editor

---

## License

MIT — see [LICENSE](LICENSE).
