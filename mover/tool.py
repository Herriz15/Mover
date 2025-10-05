"""Command line utility for wiring Codex up to a local OLAMA runtime.

The tool looks for a Codex installation, drops in a lightweight HTTP
server that proxies OpenAI-compatible requests to OLAMA, and injects a
bootstrap call into Codex's CLI so the server spins up automatically.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

DEFAULT_INSTALL_LOCATIONS = (
    "~/.codex",
    "~/Library/Application Support/Codex",
    "~/Applications/Codex.app/Contents/Resources/app",
    "~/codex",
    "/usr/local/lib/codex",
    "/opt/codex",
)


SERVER_MODULE_TEMPLATE = '''\
"""A tiny HTTP server that speaks a subset of the OpenAI API using OLAMA."""
import json
import os
import socket
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Tuple

HOST = os.environ.get("CODEX_LOCAL_SERVER_HOST", "{host}")
PORT = int(os.environ.get("CODEX_LOCAL_SERVER_PORT", "{port}"))
DEFAULT_MODEL = os.environ.get("CODEX_LOCAL_MODEL", "{model}")
OLAMA_BIN = os.environ.get("OLAMA_BIN", "olama")
_API_BASE = f"http://{host}:{port}/v1"
_SERVER_THREAD: threading.Thread | None = None
_SERVER: ThreadingHTTPServer | None = None


def _normalise_prompt(messages: Any, prompt: str | None) -> Tuple[str, str]:
    if prompt:
        return prompt, "completion"
    if not isinstance(messages, list):
        raise ValueError("messages must be a list of chat messages")
    rendered = []
    for item in messages:
        role = item.get("role", "user")
        content = item.get("content", "")
        rendered.append(f"[{role.upper()}] {content}")
    rendered.append("[ASSISTANT]")
    return "\n".join(rendered), "chat"


def _run_olama(prompt: str, model: str) -> str:
    cmd = [OLAMA_BIN, "run", model, "--prompt", prompt]
    proc = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "OLAMA command failed", cmd, proc.returncode, proc.stderr.strip()
        )
    return proc.stdout.strip()


class _RequestHandler(BaseHTTPRequestHandler):
    server_version = "CodexLocalModel/0.1"

    def _set_headers(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()

    def _send_error(self, message: str, status: int = 500) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        payload = {"error": {"message": message, "type": "olama_error"}}
        self.wfile.write(json.dumps(payload).encode("utf-8"))

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        return json.loads(body or "{}")

    def do_POST(self) -> None:  # noqa: N802 (http method name)
        try:
            payload = self._read_json()
            model = payload.get("model") or DEFAULT_MODEL
            prompt, mode = _normalise_prompt(
                payload.get("messages"), payload.get("prompt")
            )
            completion = _run_olama(prompt, model)
            response_id = f"local-codex-{uuid.uuid4()}"
            if mode == "chat":
                body = {
                    "id": response_id,
                    "object": "chat.completion",
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": completion,
                            },
                        }
                    ],
                }
            else:
                body = {
                    "id": response_id,
                    "object": "text_completion",
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "text": completion,
                        }
                    ],
                }
            self._set_headers()
            self.wfile.write(json.dumps(body).encode("utf-8"))
        except Exception as exc:  # pragma: no cover - defensive
            self._send_error(str(exc))

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        if os.environ.get("CODEX_LOCAL_SERVER_LOG", "0") not in {"1", "true", "True"}:
            return
        super().log_message(format, *args)


def _start_server() -> None:
    global _SERVER_THREAD, _SERVER
    if _SERVER_THREAD and _SERVER_THREAD.is_alive():
        return
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((HOST, PORT))
        except OSError as exc:
            raise RuntimeError(
                f"Port {PORT} on {HOST} is already in use; "
                "set CODEX_LOCAL_SERVER_PORT to override"
            ) from exc
    server = ThreadingHTTPServer((HOST, PORT), _RequestHandler)
    _SERVER = server

    def _serve() -> None:
        with server:
            server.serve_forever()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    _SERVER_THREAD = thread


def ensure_local_model_server() -> Dict[str, str]:
    """Ensure the OLAMA-backed bridge server is running."""
    _start_server()
    os.environ.setdefault("OPENAI_API_BASE", _API_BASE)
    os.environ.setdefault("OPENAI_API_KEY", "olama-local")
    return {"host": HOST, "port": str(PORT), "base_url": _API_BASE}


__all__ = ["ensure_local_model_server"]
'''

@dataclass
class CodexLocalModelIntegrator:
    """Injects OLAMA server support into a Codex installation."""

    codex_root: Optional[Path] = None
    model: str = "llama3"
    host: str = "127.0.0.1"
    port: int = 3925
    dry_run: bool = False
    backup_suffix: str = ".bak"

    def locate_codex(self) -> Path:
        """Return the Codex installation directory, searching if needed."""
        if self.codex_root:
            root = Path(self.codex_root).expanduser().resolve()
            if not root.exists():
                raise FileNotFoundError(f"Provided Codex path does not exist: {root}")
            return root

        env_dir = os.environ.get("CODEX_INSTALL_DIR")
        if env_dir:
            root = Path(env_dir).expanduser().resolve()
            if root.exists():
                return root

        for candidate in DEFAULT_INSTALL_LOCATIONS:
            path = Path(candidate).expanduser().resolve()
            if path.exists():
                return path

        raise FileNotFoundError(
            "Unable to locate Codex installation. Provide --codex-path explicitly "
            "or set CODEX_INSTALL_DIR."
        )

    def _candidate_package_dirs(self, root: Path) -> Iterable[Path]:
        direct = root / "codex"
        if direct.exists():
            yield direct
        src = root / "src" / "codex"
        if src.exists():
            yield src
        lib = root / "lib" / "codex"
        if lib.exists():
            yield lib
        for child in root.iterdir():
            if child.is_dir() and child.name.lower().startswith("codex"):
                init_py = child / "__init__.py"
                if init_py.exists():
                    yield child

    def resolve_package_dir(self, root: Path) -> Path:
        for candidate in self._candidate_package_dirs(root):
            init_py = candidate / "__init__.py"
            if init_py.exists():
                return candidate
        raise FileNotFoundError(
            f"Could not find a Codex python package under {root}. "
            "Expected to see a directory containing an __init__.py file."
        )

    def resolve_entrypoint(self, package_dir: Path) -> Path:
        direct = package_dir / "__main__.py"
        if direct.exists():
            return direct
        for name in ("cli.py", "main.py", "app.py"):
            candidate = package_dir / name
            if candidate.exists():
                return candidate
        for py_file in package_dir.glob("*.py"):
            text = py_file.read_text(encoding="utf-8", errors="ignore")
            if "__name__ == \"__main__\"" in text:
                return py_file
        raise FileNotFoundError(
            f"Unable to locate Codex CLI entrypoint under {package_dir}. "
            "Tried __main__.py, cli.py, main.py and files with a __main__ guard."
        )

    def _write_file(self, path: Path, content: str) -> None:
        if self.dry_run:
            print(f"[dry-run] Would write {path}")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def install_server_module(self, package_dir: Path) -> Path:
        server_path = package_dir / "local_model_server.py"
        if server_path.exists() and not self.dry_run:
            backup = server_path.with_suffix(server_path.suffix + self.backup_suffix)
            if not backup.exists():
                shutil.copy2(server_path, backup)
        module_text = SERVER_MODULE_TEMPLATE.format(
            host=self.host,
            port=self.port,
            model=self.model,
        )
        self._write_file(server_path, module_text)
        return server_path

    def inject_bootstrap(self, entrypoint: Path, package_name: str) -> None:
        text = entrypoint.read_text(encoding="utf-8")
        if "ensure_local_model_server" in text:
            return

        injection = textwrap.dedent(
            f"""
            # >>> CODEx local model integration (auto-generated)
            try:
                from .local_model_server import ensure_local_model_server  # type: ignore
            except Exception:  # pragma: no cover - fallback for script entrypoints
                from {package_name}.local_model_server import ensure_local_model_server  # type: ignore
            ensure_local_model_server()
            # <<< CODEx local model integration (auto-generated)
            """
        ).strip("\n")

        lines = text.splitlines()
        insert_at = 0
        for index, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", '"', "'")):
                continue
            if stripped.startswith("import ") or stripped.startswith("from "):
                insert_at = index + 1
                continue
            insert_at = index
            break
        else:
            insert_at = len(lines)

        new_lines = lines[:insert_at] + [injection, ""] + lines[insert_at:]
        new_text = "\n".join(new_lines)

        if self.dry_run:
            print(f"[dry-run] Would update {entrypoint}")
            return

        backup_path = entrypoint.with_suffix(entrypoint.suffix + self.backup_suffix)
        if not backup_path.exists():
            shutil.copy2(entrypoint, backup_path)
        entrypoint.write_text(new_text, encoding="utf-8")

    def create_integration_manifest(self, root: Path, server_path: Path) -> None:
        info = {
            "message": "Codex has been patched to use a local OLAMA model.",
            "server_module": str(server_path.relative_to(root)),
            "host": self.host,
            "port": self.port,
            "model": self.model,
        }
        manifest_path = root / "codex_local_model_integration.json"
        self._write_file(
            manifest_path,
            json.dumps(info, indent=2, sort_keys=True) + "\n",
        )

    def run(self) -> Path:
        root = self.locate_codex()
        package_dir = self.resolve_package_dir(root)
        entrypoint = self.resolve_entrypoint(package_dir)
        package_name = package_dir.name
        server_path = self.install_server_module(package_dir)
        self.inject_bootstrap(entrypoint, package_name)
        self.create_integration_manifest(root, server_path)
        return root


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--codex-path",
        type=Path,
        help="Explicit path to the Codex installation",
    )
    parser.add_argument(
        "--model",
        default="llama3",
        help="Default OLAMA model to run (default: %(default)s)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host for the injected local server (default: %(default)s)",
    )
    parser.add_argument(
        "--port",
        default=3925,
        type=int,
        help="Port for the injected local server (default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show actions without touching the Codex installation",
    )
    parser.add_argument(
        "--backup-suffix",
        default=".bak",
        help="Suffix to use for backup files (default: %(default)s)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    integrator = CodexLocalModelIntegrator(
        codex_root=args.codex_path,
        model=args.model,
        host=args.host,
        port=args.port,
        dry_run=args.dry_run,
        backup_suffix=args.backup_suffix,
    )
    try:
        root = integrator.run()
    except Exception as exc:  # pragma: no cover - CLI entry point
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(f"Codex installation patched at {root}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI behaviour
    raise SystemExit(main())
