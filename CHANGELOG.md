# Changelog

All notable changes to owui-mcp-spawner. The newest version is at the top.
Released versions are also tagged on GitHub; the in-progress section is kept up to
date as work lands, so it is never reconstructed from memory at release time.

## v0.2.2 — in progress (unreleased)

*Kept up to date as work lands; the version number in `app/__init__.py` is bumped only at release.*

**New**
- **File storage** — tools that produce a file had nowhere to put it. An instance that opts in gets `content/<id>/`, and the path reaches the tool three ways (Valve autofill for `content_dir` / `output_dir` / `*_export_dir`, the `MCP_CONTENT_DIR` environment variable, and `MCP_CONTENT_URL` for the link), so a tool written for OpenWebUI usually runs unchanged. Results are rewritten on the way out: a link to OpenWebUI's `/cache/files/...`, which resolves against OpenWebUI in the browser and finds nothing of ours, becomes a download URL of the spawner's own. Each file carries a token derived from a server secret in `runtime/content.key` — deliberately not in `settings.json`, because the runners read it and a runner doing read-modify-write on that file would sooner or later overwrite a write of the manager's. The download route needs no login, the token *is* the credential, and a wrong token gives the same `404` as a missing file so the route cannot be used to probe for names. Quota with a warning line placed **before** the tool's own output (a small model reads the first lines), optional refusal once the folder is full, and retention by file age. Settings tab **Files**, per-instance switch in **Edit → Config**, and `list_content` / `read_content` / `delete_content` in the control tool (v0.0.8, deletion behind its own switch, off by default)
- **mcp 2.x** — the runner is on the 2.x `Server` API, the base packages are pinned to `mcp>=2`, and `require_mcp_2()` refuses to start on an older line rather than failing later with a missing attribute. This lifts the emergency ceiling from v0.2.1, where `mcp` had to be pinned *below* 2 because the 2.0 release rebuilt the API the runner used. The tool router had to follow to v0.0.9 — it is itself an SDK *client*, which the port had not accounted for

**Fixed**
- **Every runner log line appeared twice** — the manager's logger has its own stdout handler, and inside the runner the root logger has one too, so each record was emitted by both. `propagate = False` on the manager logger ends it. Long-standing; it only became obvious next to a newly added line
- **The test suite deleted real files** — `tests/test_api.py::AgentTokenTests` walks *every* method of *every* `/api` route with a valid token and lets them run for real. `DELETE /api/content` was the first such route without a mandatory instance id, so nothing upstream turned it into a harmless `404`, and the first run wiped the storage folder. The same suite runs on the server. Patched like `_restart_after_delay` beside it, and verified with a sentinel file that survives a full run — **any new writing route without a mandatory id has to be patched there too**


## v0.2.1

**New**
- **Settings in tabs** — eleven sections in one modal had outgrown the dialog. They are now grouped into **Security** (password, edit mode, MCP endpoint auth, read token, agent token), **Identity**, **System** (shared port, venvs, server) and **Maintenance** (usage tracking, update check). The panels stay in the DOM, so loading and **Save Settings** still reach every field no matter which tab is open — saving remains one action for the whole dialog. The chosen tab is remembered in `localStorage` (in a `try`/`catch`: in a private window the access itself throws)
- **Login lockout** — three rejected credentials from one address cost 60 seconds, the next three 120, then 240, doubling without a ceiling; the API answers `429` with `Retry-After` and the login dialog shows a countdown instead of "wrong password". There is no login route to protect — the browser sends the password as a Bearer token on every request — so the counter sits in `app/lockout.py` where that decision is made, keyed by the connection's address and never by `X-Forwarded-For`, which a caller could write to dodge their own counter or run someone else's address into a block. A request carrying **no** credential is not counted (the UI asks before anyone has logged in, and a reload must not cost a strike), and neither is a *configured* token that merely lacks the scope for a route — that is a client at the wrong door, not a guess. Counters live in memory only: a manager restart clears every block, which is also how you get back in if you lock yourself out
- **Permissions dialog at scale** — a search over all tools (with a "10 of 152" counter, blocks holding a hit open themselves), a search over the people list, and instance blocks collapsed from eight instances up, each header carrying its status (*no access* · *all tools* · *7 of 86*). Filtering and collapsing only show and hide the rendered blocks, so a checkbox already ticked cannot get lost on the way

**Fixed**
- **A new venv installed an `mcp` the runner cannot use** — the base packages were unpinned, so any venv created after the release of `mcp` 2.0 got the 2.x line, which rebuilt the low-level `Server` API; every instance in that venv died at startup with `'Server' object has no attribute 'list_tools'`. Existing venvs keep the 1.x they installed long ago and show nothing, which is why this stayed invisible on a running installation — but a **fresh install of v0.2.0 was broken out of the box**, as was any newly created venv. `mcp` is now pinned below 2 in `venv_manager.BASE_PACKAGES` and in `pyproject.toml`, with a test that fails if the ceiling is dropped without porting the runner first
- **The permissions dialog painted over its own buttons** — its grid items kept their automatic minimum height, so the columns grew with their content instead of scrolling, and the overflow was drawn across the Save/Close bar (which has no background of its own). Measured at 900×500 with 86 tools: 199 px of overlap, now a 20 px gap and a scrollbar where it belongs
- **A leftover directory took the venv list down** — an interpreter migration parks the old venv next to the new one under a name like `default.py312-migrated-20260816-001110`. The listing asked `venv_dir()` about every directory it found, and the dot in that name raised out of the listing and took `/api/venvs` with it. Unnoticed since 16 Aug because the UI has a fallback and quietly showed only `default`. The listing now skips names this module could never have produced; `venv_dir()` stays strict, so *using* such a name still raises
- **A three-line instance name in the permissions header** — the name stacked next to the dropdown in the narrow column; the dropdown has a fixed width now and the name `min-width: 0`

## v0.2.0

**New**
- **Identity Probe** (`examples/identity-probe.json`) — one read-only tool, `whoami`, that reports who arrived at an MCP server, whether the identity was signed, and what the rules grant them. Ships with the spawner because every mistake in the identity setup is otherwise silent: a wrong secret, an unrestarted instance and a mode left on `off` are indistinguishable from a chat window. Prints a fingerprint of the shared secret instead of the secret, so both sides can be compared without either being shown (see *Per-user identity*)
- **Users & permissions** — a 🔑 dialog that assigns rights per person: the runners record every verified caller in `runtime/identities.db`, the dialog offers that roster, and per governed instance you pick *no access*, *all tools* or single functions. Roles work as base equipment (OpenWebUI sends no groups, but the role is signed), a personal entry adds to them, an explicit deny wins. Saving is live — the rules file is read per call. A rule naming someone who never called is shown and marked, because that is what a mistyped user id looks like
- **Machine identity** — `machine_identity` in an instance config stands in for callers without an OpenWebUI login (agent CLIs, scripts), so `required` no longer locks them out and the rules apply to them too. Assigned, not verified; a valid token still wins over it and a broken one is still refused
- **Per-user identity** — the MCP Bearer token says a client may connect; it never said *who* is calling, so a tool holding credentials of its own acted with the same rights for every user of an OpenWebUI installation. The runner now verifies the HS256 user token OpenWebUI forwards (`ENABLE_FORWARD_USER_INFO_HEADERS` + `FORWARD_USER_INFO_HEADER_JWT_SECRET`) and publishes the verified user for the duration of one tool call, in a `ContextVar` — never on the Tools instance, which is shared by every concurrent caller. Per instance, `identity_mode` is `off` (unchanged behaviour, the default), `optional` (identity passed on, nothing enforced) or `required` (no verified user, no call). Only HS256 is accepted — a verifier that honours the token's own `alg` can be told `"none"` — with 60 s of leeway on `exp`/`iat` for clock drift between the two hosts. Access rules in `runtime/identity_policy.json` map the user id to instances and tools, deny by default, and are enforced on the call, not just on the listing: hiding a tool is presentation, refusing it is the boundary. `account` / `credentials_file` let a tool act under the user's own backend account instead of one shared login — opaque to the spawner, one secret per file, `chmod 600`. Without a shared secret, `MCP_USER_TRUST_HEADERS=1` accepts OpenWebUI's plain user headers instead: enough to keep the users of one installation apart, but a claim rather than a proof, so it is off by default, a signed token always wins over it, and a broken token is refused rather than falling back to it. The tool router (v0.0.8) passes the caller on either way (see *Per-user identity*)
- **MCP tool router** (`examples/mcp-tool-router.json`) — one OpenWebUI connection with three meta-tools (`find_tools`, `describe_tools`, `call_tool`) instead of one connection per instance. Measured on a ten-instance installation: ~690 instead of ~11.600 tokens of schemas per request. Read-only, works with and without the shared port, refuses to route into itself or into the control tool (see *One connection for all tools*)
- **Declared categories** — a tool can name its default category in its docstring (`category: …`), used at install time when none is given. The shipped control tool and tool router declare `System`, which is also how the usage report keeps them out of the ranking
- **Usage statistics** — a report over all instances and their functions behind the 📊 button, plus `GET /api/usage`; the control tool gained `get_usage_stats` and `get_instance_specs` (v0.0.7), so an agent can evaluate the same data
- **Usage tracking** — every tool call is recorded with time, instance and function in `runtime/usage.db` (SQLite, stdlib). The info view shows how often an instance has been used, when last and which function; `never used` otherwise. Counted in the runner, so it works without the shared port, and only for `tools/call`. The event log is pruned to a retention window (default 30 days), the totals are kept, so *ever used* survives pruning. No arguments, no results, no caller addresses
- **Update hint for tools from `examples/`** — the dashboard compares an installed tool against the copy the spawner ships under the same id: a compact `↑ x.y.z` marker in the instance table, the full badge plus the source file in the info view. Both are buttons: they ask, then apply the shipped copy through the normal save path, keeping the Valve values and snapshotting the previous code to `runtime/history`. Forward only (`409` on a downgrade), refused on locked instances, hidden in read-only mode. The control tool aged unnoticed exactly this way, and the router was one day old before it was a version behind. Compared with `packaging.version`, never as strings The control tool aged unnoticed exactly this way, and the router was one day old before it was a version behind. Compared with `packaging.version`, never as strings. Reports only: an "update now" button would overwrite code you may have adapted, and is refused on a locked instance anyway
- **Info view in the dashboard** — an **Info** button per row opens a dialog with description, category, version, venv, status and the tool's function list (name, description, parameters with type and required flag). Read from the `specs` block of the tool JSON, so no instance has to run and no uploaded code is executed; available in every edit mode, hidden for guests (see *Info view*)
- **`GET /api/instances/{id}/specs`** — function catalog of one instance. Deliberately behind `require_auth` only, not `--no-code-edit`: these are metadata, not source
- **`GET /api/instances?include=specs`** — the whole catalog in one call, opt-in. Without the parameter the list payload is byte-for-byte what it was; guests never get specs
- **API tokens** — two optional credentials so a tool never has to carry the admin password, which lands in clear text in an instance config and can only be revoked by changing the password itself. The **read token** is valid on `GET` requests only (the tool router); the **agent token** on every method (the control tool with its write actions). The rule is the HTTP method, decided in one place, so a mutating route cannot forget to opt out of the read token. Both are refused with `403` on the routes that handle credentials: the three that return a token verbatim, the instance export (its payload embeds the MCP token), and `PUT /api/settings` — a token able to write a new password would be the password. Optional and independent, cannot equal the password or each other, managed in the Settings page or via `MCP_MANAGER_READ_TOKEN` / `MCP_MANAGER_AGENT_TOKEN` (see *API tokens*)
- **Update check** — optional, **off by default**: the server asks the GitHub releases API once a day whether a newer version exists and shows a badge in the header after login. Comparison via `packaging.version`, result cached server-side, browser never contacts GitHub, failures stay silent. A **Check now** button runs one check on demand — cache and switch ignored, with the result (and the reason on failure) shown right in the form; with the switch off nothing is persisted. Reports only — nothing is downloaded or installed (see *Update check*)

**Fixed**
- **Uploaded tools kept OpenWebUI's truncated `specs`** — an OWUI export often carries only the first line of each docstring, cut off mid-sentence, and sometimes misses parameters entirely. The upload path validated the code anyway and had the correct schemas in hand, but discarded them. It now replaces `specs` with the generated ones while keeping everything else the upload brought (`meta`, `manifest`, ids). Already installed tools are repaired **once, automatically**, by a background task on the next start (marker `runtime/.specs_migrated`); a file is only rewritten when the specs actually differ. The repair deliberately ignores the instance lock — `content` stays untouched, only a derived field is recomputed, and a locked instance is exactly the one that could not be fixed by hand. Matters now that the info view, the specs API and the tool router show these descriptions to humans and models

**Internal**
- **One version source** — `app/__init__.py:__version__` is the single place; `pyproject.toml` derives its version from it via `[tool.setuptools.dynamic]`. With the update check comparing against it, two places to bump would have meant false "up to date" reports

## v0.1.2

**New**
- **Shared MCP port** — all instances can be served through a single port as `/mcp/<id>` via a streaming reverse proxy; instances then bind to `127.0.0.1` only, so exactly two ports stay open (see *Shared MCP port*)
- **Categories** — instances carry a free-text category set on upload, in the editor or in Edit; the dashboard shows it as a column and offers a category filter plus a search box over ID, name, description and category. The label lives in the MCP config only, so setting it never rewrites the tool JSON/code or restarts the instance
- **Guest mode** — with auth enabled, the login screen can be dismissed with *Continue as guest*; `GET /api/instances` and `GET /api/instances/{id}` then return only `id`, `name`, `description`, `category`, `status` and `version`, and the UI hides ports, URLs, venv and all actions (see *Guest mode*)
- **Control tool v0.0.6** — `upload_tool` and `create_tool` accept manager categories; `update_instance_category` can change or clear them without modifying tool JSON/code or restarting the instance. The write actions also document the lock: code edit, config/values/dependencies/venv changes, restart, reinstall and delete state that a locked instance answers `403`, and `export_tool` states that it needs code-read permission. The shipped `specs` are regenerated from the code, so the descriptions an agent sees can no longer drift from the docstrings
- **Version badge** — the spawner's own version is shown next to the title in the header

**Security**
- **Only advertised tools are callable** — the MCP runner checks the requested name against the tool list built from the `Tools` class instead of resolving it with `getattr` alone, which also exposed private helpers and inherited methods
- **Subresource integrity for CDN assets** — the CodeMirror `<script>`/`<link>` tags carry SRI hashes, so a compromised CDN cannot inject code into the admin UI
- **CLI edit mode is locked** — a mode set via `--no-edit` / `--no-code-edit` can no longer be lifted at runtime through `PUT /api/settings`; the Settings dropdown is greyed out. Modes set via the Settings page remain changeable as before
- **Lock/unlock respects edit mode** — both routes now require upload or full edit mode and return `403` under `--no-edit`

**Fixed**
- **Dependencies starting with `http` were rejected** — the package-spec validator matched the prefix `http`, so `httpx`, `httpcore` and friends failed validation; it now only rejects `http://` and `https://` URLs
- **Bearer scheme is matched case-insensitively** — per RFC 7235 a client sending `authorization: bearer <token>` is no longer turned away by the MCP endpoint
- **Stop never lies about the outcome** — a runner that survives SIGTERM *and* SIGKILL is reported as still running instead of being marked `stopped` while the orphan keeps its port; killed children are reaped, so a zombie no longer counts as alive; `restart` aborts when the stop failed instead of starting a second process on the same port
- **Stop works during startup** — the runner PID is registered as soon as the process spawns, so stopping an instance in its startup window actually kills the process instead of leaving it running with status `stopped`
- **Port race on concurrent installs fixed** — auto-assigned ports are now held for the duration of the install, so two parallel uploads can no longer receive the same port; the shared proxy's port is reserved during allocation as well
- **ID `example` is reserved** — creating a tool with this ID used to overwrite the shipped `configs/example.json` and produce an instance that never showed up in the list
- **Atomic file writes** — configs, `pids.json` and `settings.json` are written via temp file + rename; a crash mid-write can no longer corrupt them
- **Robust request validation** — malformed JSON bodies (wrong types for `server`, `values`, `install`, `id`, `content`, `code`) return `400`/`422` instead of `500`
- **Settings save is change-aware** — an unchanged edit mode is no longer persisted to `settings.json` (which used to freeze a CLI-flag default) or reported as "Saved: edit_mode"
- **Readable error messages** — upload and config-edit dialogs now render structured `422` errors (e.g. a failed pip install) as text instead of `[object Object]`
- **Reinstall visible in readonly mode** — the dashboard now shows the Reinstall button in readonly mode, matching the documented API behaviour (it only re-runs pip for the already-pinned dependency list)
- **Log panel handles auth errors** — an expired session shows the login dialog instead of dumping a JSON error into the log view; `GET /api/instances/{id}` now includes the `version` field like the list endpoint
- **MCP token hint** — changing the MCP Bearer token in Settings now notes that running instances keep the previous token until restarted

**Internal**
- **API split into route modules** — `admin_server.py` shrank from ~1200 to ~140 lines; the endpoints now live in `app/routes/` with shared guards and serialization in `app/api_helpers.py`
- **Frontend split into ES modules** — `web/app.js` is a bootstrap; instances, config, upload, editor, logs and settings each own a module, with shared state and helpers in `common.js`
- **Faster instance list** — one directory scan feeds states and enrichment, tool versions are cached by mtime, and the disk walk runs off the event loop (the UI polls this endpoint per open tab)
- **Server-classified secrets** — `GET /api/instances/{id}/config` ships a `secret_fields` list so the edit dialog no longer has to guess which values are credentials
- **Test suite** — `tests/` covers the API contract, auth and guest exposure, locking, schema/package validation, port allocation and a full e2e lifecycle over HTTP and MCP

## v0.1.1
- **Secrets survive config round-trips** — `PUT /api/instances/{id}` ignores values equal to the `********` mask, so a client that fetches a config and echoes it back (e.g. the control tool's `update_instance_values`) no longer overwrites real API keys with the mask
- **Non-blocking delete** — deleting a running instance stops it in a worker thread instead of blocking the event loop (and every other request) for up to ~5 s
- **Locked instances can be stopped again** — reverts the v0.0.6 restriction: lock means "don't modify", but a misbehaving instance must always be stoppable. Config edit, code edit, restart and delete remain blocked while locked
- **Honest upload errors** — uploading an MCP config whose dependencies fail to install returns `422` with the error (the instance is kept in `dependency_error` for Reinstall) instead of reporting success
- **Validation in the instance venv** — `POST /api/tools/validate` accepts an optional `instance_id` and validates in that instance's venv; the editor passes it automatically when editing, so already-installed third-party imports no longer false-fail
- **pip timeout raised to 600 s per package** (was 120 s) — large wheels like `torch` no longer fail on principle
- **Version badge fix** — the `version:` regex is anchored to line start, so a "version: …" inside a description line no longer wins
- **Control tool v0.0.5** — `manager_url` tolerates a trailing slash; install-heavy calls use 600 s timeouts (`update_instance_venv` previously timed out after 10 s); `validate_tool_code` gains an optional `instance_id`
- **Dead-code cleanup** — unused imports and attributes removed, orphaned status-badge CSS dropped, missing `.alert-warning` style added

## v0.1.0
- **Renamed to `owui-mcp-spawner`** (formerly "MCP Framework" / "MCP Manager") — the focus is spawning OpenWebUI tools as isolated MCP servers. Environment variable names (`MCP_MANAGER_PASSWORD`, …) are unchanged for compatibility.
- **Per-instance virtual environments** — every instance runs in its own venv under `runtime/venvs/<name>/`; dependencies are fully isolated and never pollute the spawner. Created on demand with base packages; validation, installs and runtime all use the instance venv. New `app/venv_manager.py`, `MCPConfig.venv` field, and a one-time migration of existing instances on first start.
- **Dependencies actually install from `requirements:`** — the docstring `requirements:` line is parsed and installed *before* validation, so tools importing numpy/pandas/yfinance/… upload in one go (previously rejected with `ModuleNotFoundError`).
- **One-step `create_tool`** — `POST /api/tools/create` and the editor's **Install as MCP** create an instance from raw Python in a single step (install → validate in venv → fill Valve values → save); no more placeholder + export + upload dance.
- **Valve values sync on code save** — saving edited code merges new Valve defaults into the editable config (keeps user-set values, drops removed valves), so valves stay editable in the UI after a code change.
- **Safe delete** — deleting an instance no longer removes a tool file still referenced by another instance (reference-counted); MCP-config uploads copy into an instance-owned file instead of sharing.
- **Venv management UI** — dashboard **Venv** column, venv dropdowns in the upload/editor/edit dialogs, and a Settings section to create/delete venvs (in-use protection, `default` protected).
- **Port choice at install** — optional fixed port on upload / `create_tool`; a clash returns `409` instead of being silently reassigned.
- **Control tool v0.0.4+** — new tools: `create_tool`, `update_instance_values`, `update_instance_dependencies`, `update_instance_venv` (value changes report a hint to ask before restarting).

## v0.0.6
- **Isolated validation** — tool code is validated in a short-lived subprocess with a timeout; import-time side effects, crashes or endless loops can no longer affect the spawner process
- **Non-blocking API** — dependency installs, instance start/stop/restart and validation run in worker threads; the UI stays responsive during long operations
- **Health checks** — `start` waits until the instance actually answers on its port (with log tail as error detail on failure); a watchdog marks instances whose process died as `failed`
- **Log rotation** — rotating log, oversized runtime logs rotate on instance start, log endpoints return the last 500 lines instead of the whole file
- **Tool-code history** — the previous version of a tool JSON is snapshotted to `runtime/history/<id>/` before every save (last 10 kept)
- **Unified schema generation** — validation, export and runtime now share one implementation (`schema_gen.py`); the editor gains `Literal` → enum and `Annotated`/`Field` descriptions
- **Hardening** — locked instances can no longer be stopped; timing-safe token comparison (`hmac.compare_digest`); no more `exec()` of uploaded code in the spawner process
- **Cleanup** — removed dead config fields (`enabled`, `runtime`, `requirements_file`, `install_on_upload`) and unused status values; the control tool (v0.0.2: precise 403 error details, fixed `export_tool`) now lives in `examples/`

## v0.0.5
- **Settings page** — ⚙ button in the web UI for managing password, edit mode, MCP token and restart; all settings persisted in `runtime/settings.json`
- **MCP Bearer Token auth** — optional token protecting all MCP endpoints; `--mcp-token` and `--no-token-edit` CLI flags; token visible/hidden with 👁 toggle and ⟳ generator in the UI
- **Improved schema generation** — `Literal[...]` → `enum`, `Annotated[T, Field(description=...)]` and Google-style `Args:` docstrings → `description`, defaults → `default`; built from live Python code, not the pre-built specs

## v0.0.4
- Three edit modes: `--no-code-edit` (upload-only) and `--no-edit` (readonly) — enforced at API and UI level
- Edit mode reported at startup and exposed via `/api/auth-status`

## v0.0.3
- systemd deploy examples
- Password via `EnvironmentFile` — no secrets in the service file

## v0.0.2
- Bearer-token authentication (SHA-256, set via `MCP_MANAGER_PASSWORD`)
- Login modal — verified against protected `/api/auth-check` endpoint
- Config, tool-code, and log routes require auth
- Auth initializes at module import time

## v0.0.1
- Initial release: OpenWebUI JSON → MCP server, web UI, auto-start, Streamable HTTP transport
