"""Lightsei CLI.

Subcommands:

    lightsei serve <bot.py>
        Watch a bot file (and its directory, recursively) for `.py` edits
        and restart the bot subprocess when anything changes.

    lightsei deploy <dir>
        Zip a bot directory and POST it to the backend as a deployment.
        The worker (running on Lightsei's infra) picks it up, builds a
        venv, and runs the bot. See worker/README.md for what gets
        injected into the bot's environment.
"""
import argparse
import io
import os
import signal
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import List, Optional


def _spawn(target: Path, extra_args: List[str]) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(target), *extra_args],
        stdin=sys.stdin,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


def _terminate(proc: subprocess.Popen, timeout: float = 3.0) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass


def serve(args: List[str]) -> int:
    if not args:
        print("usage: lightsei serve <bot.py> [args...]", file=sys.stderr)
        return 2
    target = Path(args[0]).resolve()
    if not target.exists():
        print(f"file not found: {target}", file=sys.stderr)
        return 2
    if not target.is_file():
        print(f"not a file: {target}", file=sys.stderr)
        return 2

    try:
        from watchfiles import watch  # type: ignore
    except ImportError:
        print(
            "the 'watchfiles' package is required for `lightsei serve`.\n"
            "  pip install watchfiles",
            file=sys.stderr,
        )
        return 2

    watch_dir = target.parent
    extra_args = args[1:]

    proc = _spawn(target, extra_args)
    print(
        f"\033[1mlightsei serve\033[0m: running {target.name} "
        f"(PID {proc.pid}); watching {watch_dir} for .py edits",
        flush=True,
    )

    # Forward Ctrl+C to the child by letting the default SIGINT handling
    # propagate. We just need to clean up the child on our way out.
    try:
        for changes in watch(watch_dir, recursive=True):
            py_changed = any(p.endswith(".py") for _, p in changes)
            if not py_changed:
                continue
            print(
                "\033[2mlightsei serve: change detected, restarting…\033[0m",
                flush=True,
            )
            _terminate(proc)
            proc = _spawn(target, extra_args)
            print(
                f"\033[2mlightsei serve: restarted (PID {proc.pid})\033[0m",
                flush=True,
            )
    except KeyboardInterrupt:
        print("\nlightsei serve: stopping", flush=True)
    finally:
        _terminate(proc)

    return 0


# ---------- deploy ---------- #

# Directories and file patterns excluded from the deployment bundle. Mirrors
# what a typical .gitignore catches; chosen to keep the upload small and
# avoid shipping the developer's local venv or cache.
_EXCLUDED_DIRS = frozenset({
    "__pycache__",
    ".git",
    ".venv",
    "venv",
    "env",
    "node_modules",
    ".pytest_cache",
    ".lightsei-runtime",
    "dist",
    "build",
    ".idea",
    ".vscode",
    ".mypy_cache",
    ".ruff_cache",
})
_EXCLUDED_SUFFIXES = (".pyc", ".pyo")
_EXCLUDED_NAMES = frozenset({".DS_Store"})


def _build_zip(source: Path) -> bytes:
    """Zip a directory into an in-memory bundle. Skips common dev junk
    (venvs, caches, .git, node_modules, etc.) so the upload is bot code
    only. Walks deterministically (sorted) so the same source tree always
    produces the same sha256.
    """
    if not source.is_dir():
        raise ValueError(f"not a directory: {source}")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(source):
            # Mutate dirs in place so os.walk skips excluded subtrees.
            dirs[:] = sorted(d for d in dirs if d not in _EXCLUDED_DIRS)
            for fname in sorted(files):
                if fname in _EXCLUDED_NAMES:
                    continue
                if fname.endswith(_EXCLUDED_SUFFIXES):
                    continue
                full = Path(root) / fname
                rel = full.relative_to(source)
                z.write(full, str(rel))
    return buf.getvalue()


def _resolve_api_key(arg: Optional[str]) -> str:
    if arg:
        return arg
    env = os.environ.get("LIGHTSEI_API_KEY")
    if env:
        return env
    raise SystemExit(
        "missing api key: pass --api-key or set LIGHTSEI_API_KEY in env"
    )


def _resolve_base_url(arg: Optional[str]) -> str:
    if arg:
        return arg
    return os.environ.get("LIGHTSEI_BASE_URL", "https://api.lightsei.com")


def deploy(args: List[str]) -> int:
    p = argparse.ArgumentParser(
        prog="lightsei deploy",
        description=(
            "Zip a bot directory and upload it to Lightsei. The worker "
            "running on Lightsei's infra builds a venv from your "
            "requirements.txt and spawns bot.py."
        ),
    )
    p.add_argument(
        "directory", type=Path,
        help="path to the bot directory (must contain bot.py)",
    )
    p.add_argument(
        "--agent", "-a", default=None,
        help="agent name (default: directory basename)",
    )
    p.add_argument(
        "--api-key", default=None,
        help="workspace api key (default: $LIGHTSEI_API_KEY)",
    )
    p.add_argument(
        "--base-url", default=None,
        help="backend URL (default: $LIGHTSEI_BASE_URL or https://api.lightsei.com)",
    )
    p.add_argument(
        "--no-wait", action="store_true",
        help="return after upload; don't poll for status",
    )
    p.add_argument(
        "--timeout", type=float, default=300.0,
        help="seconds to wait for status=running before giving up (default: 300)",
    )
    ns = p.parse_args(args)

    source = ns.directory.resolve()
    if not source.is_dir():
        print(f"not a directory: {source}", file=sys.stderr)
        return 2
    if not (source / "bot.py").is_file():
        print(f"no bot.py at {source}", file=sys.stderr)
        return 2

    agent_name = ns.agent or source.name
    api_key = _resolve_api_key(ns.api_key)
    base_url = _resolve_base_url(ns.base_url).rstrip("/")

    try:
        import httpx
    except ImportError:
        print("the 'httpx' package is required for deploy", file=sys.stderr)
        return 2

    print(f"zipping {source}...", flush=True)
    bundle = _build_zip(source)
    print(f"  {len(bundle):,} bytes", flush=True)

    print(f"uploading to {base_url} as agent={agent_name}...", flush=True)
    with httpx.Client(
        base_url=base_url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=60.0,
    ) as http:
        try:
            r = http.post(
                "/workspaces/me/deployments",
                data={"agent_name": agent_name},
                files={
                    "bundle": ("bundle.zip", bundle, "application/zip"),
                },
            )
        except httpx.HTTPError as e:
            print(f"upload failed: {e}", file=sys.stderr)
            return 1
        if r.status_code != 200:
            print(f"upload failed: {r.status_code} {r.text}", file=sys.stderr)
            return 1
        dep = r.json()
        print(f"  deployment id: {dep['id']}")
        print(f"  status: {dep['status']}")

        if ns.no_wait:
            return 0

        # Poll until running or failed.
        deadline = time.monotonic() + ns.timeout
        last = dep["status"]
        while time.monotonic() < deadline:
            time.sleep(2.0)
            try:
                r = http.get(f"/workspaces/me/deployments/{dep['id']}")
                r.raise_for_status()
            except httpx.HTTPError as e:
                print(f"  poll failed: {e}", file=sys.stderr)
                continue
            cur = r.json()
            if cur["status"] != last:
                print(f"  status: {cur['status']}", flush=True)
                last = cur["status"]
            if cur["status"] == "running":
                print("deployment is running.", flush=True)
                return 0
            if cur["status"] == "failed":
                print(
                    f"deployment failed: {cur.get('error') or '(no error)'}",
                    file=sys.stderr,
                )
                return 1
            if cur["status"] == "stopped":
                # Bot exited cleanly before we noticed status=running. Not
                # necessarily a problem (one-shot scripts), so return 0.
                print("deployment stopped (bot exited cleanly).", flush=True)
                return 0
        print(
            f"timed out after {ns.timeout:.0f}s; status is still {last}.\n"
            f"check the dashboard for details.",
            file=sys.stderr,
        )
        return 1


# ---------- main ---------- #

_HELP_TEXT = """\
usage:
  lightsei serve <bot.py> [args...]    Run a bot and auto-restart on file changes.
  lightsei deploy <dir> [opts]         Upload a bot directory as a Lightsei deployment.

Run `lightsei deploy --help` for deploy-specific options.
"""


def main(argv: Optional[List[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(_HELP_TEXT, file=sys.stderr)
        return 0 if argv and argv[0] in ("-h", "--help") else 1
    cmd, *rest = argv
    if cmd == "serve":
        return serve(rest)
    if cmd == "deploy":
        return deploy(rest)
    print(f"lightsei: unknown command {cmd!r}", file=sys.stderr)
    print("try `lightsei --help`", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
