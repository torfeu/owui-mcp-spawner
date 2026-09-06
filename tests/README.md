# Test suite

Run the complete regression suite from the project root:

```bash
.venv/bin/python -m unittest discover -v
```

The tests are deliberately isolated from real instance configs and processes.
They cover the public API contract, authentication and edit-mode dependencies,
guest data exposure, instance locking, schema and package validation, secret
masking, port allocation, and shared-proxy collision handling.

`test_api.py` also pins the two API tokens, and both are pinned by enumeration
rather than by example — the point is that nothing slips through unclassified.

For the **read token**, every `GET` route under `/api` is listed in one of two
sets: readable with the token, or password-only because it handles a credential.
The test compares that classification against the routes actually registered, so
a new `GET` route fails the suite until someone decides which group it is in. A
sweep over every non-`GET` route asserts `401`: the design rests on "GET only",
and one route accepting it with another method would hand write access to a
token that lives in clear text inside instance configs.

For the **agent token**, a sweep over every route and method asserts the
mirror image — accepted everywhere except the five admin-only entries, which
must answer `403`. A second test checks those five are actually registered, so
a renamed route cannot leave the sweep silently testing nothing. The escalation
path has its own case: `PUT /api/settings` with a password change must be
refused and the old password must still work afterwards. A token that can write
a new password is the password, and that case is the reason the route is
password-only at all.

`test_api.py` also covers the version comparison against `examples/`: a newer
shipped tool is reported with its path, an equal or older one is not an update,
and `0.0.10` beats `0.0.9` — the reason the comparison goes through
`packaging.version` instead of comparing strings. Edge cases: the shipped MCP
*connection* samples carry an id but no code and must not shadow a real tool of
that name, and one broken file must not take the whole index with it. One test
runs against the real `examples/` directory and asserts the shipped ids are
still `mcp_tool_router` and `mcp_manager_control` — renaming one would switch
the hint off silently for everyone who installed it.

`POST /api/instances/{id}/update-from-example` is covered by its refusals as
much as by its success: it applies the shipped code by delegating to
`save_tool_code` (asserted, so the backup, validation and Valve sync cannot be
reimplemented beside it), and it says no to a downgrade (`409`), to an id that
ships nothing (`404`) and to a locked instance (`403`).

`test_update_check.py` covers the optional GitHub release check: the version
comparison (parsed, not string-compared — `0.1.10` is newer than `0.1.9`), the
24 h cache, that a failed request keeps the previous result, and that a disabled
check never sends a request and stays invisible to anonymous callers. The
"Check now" button has its own cases: it ignores the cache, works while the
switch is off without persisting anything, and reports a failed request instead
of claiming "up to date".

`test_activity.py` covers the usage database: calls are recorded per function,
the timestamp is taken when the call happens (not when the batch is written), a
burst is written as one batch, pruning removes events but keeps the totals so
"ever used" survives, an unusable database never breaks a tool call, deleting
an instance drops both tables — and the polled instance list stays free of it.

`test_activity.py` also pins `GET /api/usage`: never-used instances appear with
zeros, the ranking follows the selected window rather than the lifetime total,
per-function numbers are included and the window is clamped.

`test_specs_repair.py` covers the rule that a tool's `specs` describe its code:
an upload keeps its `meta`/`manifest`/`content` but gets the generated schemas,
and the one-time migration repairs installed tools without touching files whose
specs already match.

`test_tool_router.py` holds the shipped router in `examples/` to its contract:
specs generated from the code, exactly three meta-tools, `find_tools` callable
without arguments, one dotted handle plus a structured `arguments` object, and
the two guards against routing into itself or into the control tool.

`test_control_tool.py` covers the shipped control tool in `examples/`: its Valve
gates, restart hints and the lock notes in its write actions, plus the rule that
the exported `specs` are generated from the code — a hand-edited description
would drift from the docstring and fail the test.

`test_identity.py`, `test_policy.py` and `test_runner_identity.py` cover the
per-user identity: verification, rules, and the gate in the runner.

Verification is tested by what it *refuses*: a tampered payload, another
secret, an expired or future-dated token, a wrong issuer, a missing `sub`, and
both `alg` tricks — `"none"`, and an HS256 signature relabelled `RS256`. A
token that just expired still passes, because the two hosts' clocks differ and
the alternative is rejecting every user over a minute of drift. Two tests pin
that no token ever reaches a log line: not through a `repr`, not through a
refusal message.

`test_policy.py` asserts deny by default from every direction — no file, no
entry, an unlisted instance, a broken file — plus the one deliberate escape
hatch. E-mail matching stays off unless switched on, and an ambiguous address
matches nobody. Credentials are read per access so a rotated file takes effect
without a restart, and a group-readable file is flagged but still served.

`test_runner_identity.py` drives the real handlers from `build_server()` with a
faked request context, which is why the handlers were extracted from
`run_server` at all. The case that carries the design: a tool hidden from a
user's `tools/list` must also be refused when it is called by name — filtering
a listing is presentation, the call is the boundary. Both this and the policy
gate were checked by mutation (disable the gate, watch exactly these tests go
red). `identity_mode=off` is pinned to behave exactly as before, and fifty
interleaved calls from two users against one Tools instance must each see their
own caller.

`test_identity_probe.py` holds the shipped diagnostic tool to the same rule as
the router and the control tool — generated specs, one tool and no more — plus
the property it exists for: its output can be pasted anywhere, so neither the
shared secret, nor an account's credential, nor a raw token may appear in it,
in either branch. It also pins that an unsigned identity is labelled as such;
a signed and an unsigned user otherwise look identical in the answer.

`test_identity_registry.py` and `test_permissions_api.py` cover the roster and
the dialog behind it. The roster is a *roster*, not an audit trail: one row per
user, a returning caller costs no write, changed claims are written through at
once, and no token is stored. Two properties are pinned because both would fail
quietly: reading an empty installation creates no database (otherwise every
test run and every backup would carry an empty file), and an unwritable
database never breaks a tool call — the user is verified either way.

The API tests are mostly about *not hiding things*: a rule naming someone who
never called is listed and marked (that is what a mistyped user id looks like),
a broken policy file is reported in the dialog rather than only in a log, and
forgetting a person leaves their rules alone — tidying a list must not silently
revoke access. One test asserts that no credential ever passes through either
route, in either direction.

The suite redirects the roster into a temp file wherever a verified identity is
recorded (`redirect_registry`). Without it the tests would fill the real
`runtime/identities.db` with `sub-anna`, quietly, in a file nobody looks at.

`test_process_lifecycle.py` covers the window in which a start and a stop meet.
Starting holds a lock across `ensure_venv()` — minutes, when a venv is built —
and stopping must not wait for it, which is how a confirmed stop used to leave a
runner behind: spawned after the stop, never published as running, invisible to
the watchdog, holding the port. Both orders are pinned (a stop during the venv
preparation leaves nothing alive; a stop after the spawn finds the pid and kills
it), plus fifty rounds of the same race with the timing left to the machine.
Three more cover the rest of the lifecycle, all of them about one operation
overwriting a newer one: a start that is overtaken rather than stopped must not
publish itself over its successor; a stop that is still killing must not
deregister the runner that replaced it; and a stop landing between a start's
announcement and its claim must not be forgotten. Everything is faked down to `Popen`: this suite runs on the
server, where spawning a real runner would take a real port.

`test_id_reservation.py` pins that an instance id is held from the availability
check until the config is written. Two creates of one id used to pass the check,
install in parallel and both answer `ok` with their own port, and only one config
survived it. Two *different* ids must still be created side by side, and a
refused create has to give its reservation back — a leaked one would make the id
unusable until a restart.

`test_save_routes.py` covers the two routes that write a config or a tool's code,
and the two rules they broke. Saved is not the same as live: a restart the
manager tried and could not finish must never come back as `restarted: true`,
and the reason travels with it. And a save changes what it was given, not the
file around it — the manifest, the creation time and any field an import brought
along survive an edit. The nested-secret round trip lives here too: read the
config, save it back untouched, and the credential one level down must still be
the real one rather than eight stars — plus the refusal, because a masked value
inside a list that can no longer be traced back to what it stood for has to come
back as a `422` instead of being filled in from the wrong entry.

`test_e2e.py` additionally creates a temporary project tree and manager process.
It validates a complete tool lifecycle through the real HTTP and MCP transports,
including direct and shared-port MCP calls. It reuses the ready `default` venv
but never writes instance data into the real project.
