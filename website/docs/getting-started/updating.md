---
sidebar_position: 3
title: "Updating & Uninstalling"
description: "How to update Hermes Agent to the latest version or uninstall it"
---

# Updating & Uninstalling

## Updating

Update to the latest version with a single command:

```bash
hermes update
```

This pulls the latest code, updates dependencies, and prompts you to configure any new options that were added since your last update.

Branch-aware behavior:
- normal installs update from `origin/main`
- installs running on the personal `prod` branch update from `fork/prod` when a `fork` remote exists

That means Hermes agents deployed from the canonical personal prod branch can safely run `hermes update` and stay on your curated forked release line instead of jumping back to upstream main.

:::tip
`hermes update` automatically detects new configuration options and prompts you to add them. If you skipped that prompt, you can manually run `hermes config check` to see missing options, then `hermes config migrate` to interactively add them.
:::

### Updating from Messaging Platforms

You can also update directly from Telegram, Discord, Slack, or WhatsApp by sending:

```
/update
```

This pulls the latest code, updates dependencies, and restarts the gateway.

### Maintainer workflow for the personal fork

If you maintain the personal fork itself, use the repository script:

```bash
cd ~/.hermes/hermes-agent
./scripts/update-prod-branch.sh
```

This formalizes Option B (merge-based maintenance):
1. fast-forward `main` to `origin/main`
2. push that mirror to `fork/main`
3. reset local `prod` to `fork/prod`
4. merge updated `main` into `prod`
5. push `fork/prod`

Why merge-based?
- easier to operate safely than rebasing a long-lived deploy branch
- avoids rewriting `prod` history for every deployed agent
- keeps upstream sync pain low while preserving a clear curated prod line

### Manual Update

If you installed manually (not via the quick installer):

```bash
cd /path/to/hermes-agent
export VIRTUAL_ENV="$(pwd)/venv"

# Pull latest code and submodules
git pull origin main
git submodule update --init --recursive

# Reinstall (picks up new dependencies)
uv pip install -e ".[all]"
uv pip install -e "./tinker-atropos"

# Check for new config options
hermes config check
hermes config migrate   # Interactively add any missing options
```

---

## Uninstalling

```bash
hermes uninstall
```

The uninstaller gives you the option to keep your configuration files (`~/.hermes/`) for a future reinstall.

### Manual Uninstall

```bash
rm -f ~/.local/bin/hermes
rm -rf /path/to/hermes-agent
rm -rf ~/.hermes            # Optional — keep if you plan to reinstall
```

:::info
If you installed the gateway as a system service, stop and disable it first:
```bash
hermes gateway stop
# Linux: systemctl --user disable hermes-gateway
# macOS: launchctl remove ai.hermes.gateway
```
:::
