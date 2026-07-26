# Test suite

Run the complete regression suite from the project root:

```bash
.venv/bin/python -m unittest discover -v
```

The tests are deliberately isolated from real instance configs and processes.
They cover the public API contract, authentication and edit-mode dependencies,
guest data exposure, instance locking, schema and package validation, secret
masking, port allocation, and shared-proxy collision handling.

`test_control_tool.py` covers the shipped control tool in `examples/`: its Valve
gates, restart hints and the lock notes in its write actions, plus the rule that
the exported `specs` are generated from the code — a hand-edited description
would drift from the docstring and fail the test.

`test_e2e.py` additionally creates a temporary project tree and manager process.
It validates a complete tool lifecycle through the real HTTP and MCP transports,
including direct and shared-port MCP calls. It reuses the ready `default` venv
but never writes instance data into the real project.
