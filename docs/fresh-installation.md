# Fresh installation and documentation check

Checked on 2026-09-06, using framework source `1b03fc5` (version 0.2.2), plus the new standalone calculator example.

## Installation method

A clean source export was placed in a separate directory. No existing `.venv`, runner environments, instance configurations, tool installations, settings, credentials or content were copied. The tracked `configs/example.json` template was retained; the empty dashboard confirmed that it does not create an instance.

A new manager environment was created and the project installed with:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python app/manager.py --host 127.0.0.1 --port 17860
```

The first tool installation built `runtime/venvs/default` through the normal application workflow. The calculator installation built a second named environment, `calculator`. Package downloads could use pip's download cache; no installed environment was reused.

| Component | Installed version |
|---|---|
| Python | 3.11.14 |
| Framework | 0.2.2 |
| FastAPI | 0.141.1 |
| Starlette | 1.6.0 |
| MCP SDK | 2.1.1 |
| Pydantic | 2.13.5 |
| psutil | 7.2.2 |

`pip check` passed in the manager environment and in both runner environments. These are observed versions, not a lockfile or a promise about future package resolution.

## Functional checks

| Check | Result |
|---|---|
| Initial dashboard | Empty; no external integrations installed |
| Create Valve Example through the real New Tool editor | HTTP 200; source validated and instance installed |
| Create Calculator and Content examples through the manager API | Both installed successfully |
| Start all three instances | All running; three healthy dashboard indicators |
| `greet(name="Alex")` | `Hello, Alex!` |
| Change `greeting` Valve and call again | `Welcome, Alex!` after automatic restart |
| `add(a=7, b=5)` | `12.0` |
| `multiply(a=6, b=7)` | `42.0` |
| Enable file storage and call `write_note` | Markdown note created in the instance's content folder |
| Download the returned file | HTTP 200; expected title and body verified |
| Set Download base URL through Settings | Absolute local download URL generated and downloaded successfully |
| Export an instance connection entry | HTTP 200 |
| Repeat greeting using Info → Test → Call in the browser | Expected result displayed |
| Optional manager password and browser login | Admin controls available after authentication |
| Guest view | Only reduced instance information; sensitive config request returned 401 |
| Browser JavaScript errors in the final UI run | None |

The call endpoint used by these checks performs real MCP communication with the runner. Tool execution and the resulting files were not mocked. No Nextcloud, OpenHAB, OpenWebUI, LLM or third-party service account was used.

## Full-suite result: compatibility issue still open

The repository suite was also run in another disposable copy, using the freshly installed manager packages and a copy of the freshly built default runner environment.

**787 tests ran: 782 passed and 5 errored**, in 18.545 seconds. The errors are all in `tests/test_api.py`, where route enumeration assumes every item of `app.routes` has a `.path` attribute. With the freshly installed FastAPI 0.141.1, the collection also contains `_IncludedRouter` objects, producing:

```text
AttributeError: '_IncludedRouter' object has no attribute 'path'
```

Affected checks:

- `ApiContractTests.test_all_api_routes_remain_registered`
- `ReadTokenTests.test_every_get_route_is_classified`
- `ReadTokenTests.test_read_token_is_refused_on_every_mutating_route`
- `AgentTokenTests.test_agent_token_opens_every_route_except_the_admin_ones`
- `AgentTokenTests.test_every_admin_only_route_is_registered`

This is a test-introspection incompatibility observed with the new dependency resolution. The direct browser and MCP checks above passed, but this installation cannot be described as having a fully green suite: the five contract/permission sweeps did not complete. Adapt route enumeration to include the actual nested API routes, rather than merely skipping objects without `.path`. Runtime dependencies and test code were not changed as part of this documentation task.

For comparison, the previous 787-test green result used FastAPI 0.136.1 and Starlette 1.0.1 in the existing development environment. This fresh installation deliberately did not reuse that environment.

## README changes

- Replaced the long entry document with a fresh-installation guide and working local examples.
- Preserved the feature and API documentation in [reference.md](reference.md); moved migration instructions to [upgrading.md](upgrading.md).
- Removed the unrelated OpenHAB promotion from the entry page and replaced deployment-specific screenshots with this clean demo.
- Clarified that Nextcloud and other integrations are optional tools, not framework dependencies. The former README text did not itself list Nextcloud as an installation requirement.
- Corrected the claim that every instance automatically receives a private environment: `default` is shared; a separate name is an explicit choice.
- Replaced the incomplete hand-maintained pip command with installation from `pyproject.toml`, including `psutil` and the MCP SDK requirement.
- Distinguished the manager password, MCP token, manager URL and runner URLs.
- Added the first install/start/test workflow, Valve change, file-storage switch and Download base URL.
- Corrected guest startup behavior and the control-tool catalog count (33 public methods, previously documented as 28).
- Removed deployment-specific size/token measurements and client-specific approval snippets from the general instructions. Advanced behavior and permissions remain documented.

No framework feature or backend implementation was removed. The added calculator exposes only local addition and multiplication and has no extra package dependencies.

## Screenshots

All images are genuine browser captures; data and results were not composited or substituted.

- [Empty installation](images/fresh-empty.png)
- [Tool editor](images/tool-editor.png)
- [Dashboard with local examples](../Screen_Admin.png)
- [Successful test call](images/test-call.png)
- [Stored note and download settings](images/file-storage.png)
- [Guest view](../Screen_Guest.png)

The screenshots intentionally show demo ports 17860 and 18101–18103. A normal first installation uses manager port 7860 and allocates available instance ports automatically.
