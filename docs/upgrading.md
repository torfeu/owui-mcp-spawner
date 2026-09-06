# Upgrading an existing installation

For a new installation, use the [README](../README.md). These notes apply only to existing state.

1. Export a backup under **Settings → Maintenance → Backup & Restore**. Protect backups containing credentials; copy files from `content/` separately if you need them.
2. Stop the manager, update the source checkout, then run `.venv/bin/python -m pip install -e .`.
3. Read the [changelog](../CHANGELOG.md) for changes affecting your version, restart the manager and check the instance logs and health status.

## Existing runner environments

The current runner uses MCP SDK 2.x. Base packages are installed when a named environment is first created; updating the manager does not automatically upgrade every existing environment. An environment containing MCP 1.x needs an SDK upgrade before its runner can start:

```bash
runtime/venvs/NAME/bin/python -m pip install --upgrade 'mcp>=2'
```

Replace `NAME` with the environment in the dashboard and restart its instances afterwards. Instances sharing an environment share that upgrade.

## Older migrations

- Upgrading from versions at or below 0.0.6 performs a one-time installation of existing dependencies into `default`, recorded in `runtime/.venv_migrated`. This can require network access and take longer than a normal start. Restart instances to use the prepared interpreter.
- Existing uploaded tool schemas may be rebuilt from their source during startup. The tool source remains the basis of the function catalog.

Keep the source checkout, `configs/`, `tools/` and required runtime state together. A fresh installation starts empty and does not need any of these migration steps.
