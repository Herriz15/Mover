# Mover: Codex ↔︎ Ollama bootstrapper

Mover is a small Rust utility that wires the Codex terminal client up to a
locally running [Ollama](https://ollama.com/) server. It performs three pieces
of work for you:

1. Make sure `ollama serve` is running (starting it if required).
2. Pull and warm the model you want to use (defaults to `llama3.2:3b`).
3. Launch the Codex CLI with environment variables pointing at the local
   Ollama server so Codex speaks to your chosen model.

The program is meant to run locally on the same machine as Codex. It does not
modify the Codex source tree; instead it supervises the processes that Codex
needs in order to talk to Ollama.

## Building

Mover is a regular Cargo binary crate:

```bash
cargo build --release
```

The resulting binary will live in `target/release/mover`.

## Usage

```bash
mover --codex-bin /usr/local/bin/codex --model llama3.2:3b
```

If you need to pass extra flags to Codex, append them after `--`. For example,
to select a specific Codex profile you would run

```bash
mover --codex-bin /usr/local/bin/codex --model llama3.2:3b -- --profile my-profile
```

Make sure the profile you reference already exists in your Codex configuration;
otherwise Codex will exit with an error before Mover can hand over control.

Key flags:

* `--codex-bin`: Path to the Codex executable (defaults to `codex` on `$PATH`).
  On Windows you can also point this at the directory where Node-based
  shims (such as `codex.cmd` or `codex.ps1`) live; Mover will resolve the
  actual launcher for you, preferring wrappers with executable extensions
  over extensionless helper files.
* `--ollama-bin`: Path to the `ollama` executable (defaults to `ollama`).
* `--model`: Which Ollama model to pull and warm (defaults to `llama3.2:3b`).
* `--host` / `--port`: Where the Ollama server should listen. If a server is
  already listening on that address Mover will reuse it.
* `--skip-pull`: Skip pulling the model before warm-up.
* `--no-warmup`: Skip the warm-up request sent to Ollama after the server is
  available.
* `--serve-only`: Start (or validate) `ollama serve` and exit without spawning
  Codex.
* `--`: Everything after the `--` separator is forwarded to Codex unchanged.

Mover sets the following environment variables before spawning Codex:

* `OPENAI_API_BASE` → `http://<host>:<port>/v1`
* `OPENAI_API_KEY` → `ollama`
* `OLLAMA_HOST` / `OLLAMA_PORT` (so Codex spawned processes share the same
  connection details)

These defaults match the OpenAI-compatible API surface that Ollama exposes.

## Behaviour

* If `ollama serve` is already listening on the requested address Mover will
  leave it running.
* If Mover started `ollama serve` itself it will terminate the child process
  once Codex exits.
* Warm-up requests are sent with an ultra-short prompt so that the chosen model
  loads into memory without wasting tokens.
* Errors from `ollama` or Codex are forwarded to stderr along with the exit
  status that Codex returned.

## Model swapping

You can swap models simply by passing a different `--model` flag. Mover will
pull the new model (unless `--skip-pull` is used) and warm it before launching
Codex.

## Caveats

* The Codex CLI must understand OpenAI-compatible APIs for this integration to
  work. If Codex expects a different protocol you may need an additional
  translation layer.
* Mover does not edit Codex' source; if you need deeper integration you can use
  this tool as the supervising process within your own Rust codebase.
