# Mover Codex Local Model Integrator

This repository provides a CLI helper that patches a Codex installation so
that it can route API requests through a lightweight HTTP bridge backed by
[OLAMA](https://github.com/jmorganca/olama). The tool locates the Codex
installation, injects an OpenAI-compatible server that shells out to OLAMA,
and updates Codex's startup sequence so the bridge launches automatically.

## Usage

```bash
python -m mover.tool --codex-path /path/to/codex --model llama3
```

Key options:

- `--codex-path`: Explicit path to your Codex install (falls back to common
  install directories or the `CODEX_INSTALL_DIR` environment variable).
- `--model`: Default OLAMA model to execute (defaults to `llama3`).
- `--host` / `--port`: Network bindings for the injected local server.
- `--dry-run`: Show the actions without modifying the installation.

The tool will:

1. Create `local_model_server.py` inside the Codex package.
2. Back up and patch the CLI entrypoint to boot the bridge on startup.
3. Emit a `codex_local_model_integration.json` manifest describing the change.

Ensure OLAMA is installed and available on your `$PATH` (or set `OLAMA_BIN`).
