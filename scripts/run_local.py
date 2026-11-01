"""Run the whole stack without Docker (Windows/macOS/Linux).

Starts, in one terminal:
  - embedded Postgres (pgserver, data in .data/pg)
  - mock LLM provider     http://localhost:8001
  - LiteLLM proxy         http://localhost:4000   (admin UI at /ui)
  - ops dashboard         http://localhost:8501

Creates .env from .env.example with random secrets on first run.
Ctrl+C stops everything.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
PY = sys.executable


def ensure_env_file() -> None:
    if ENV_FILE.exists():
        return
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    text = text.replace("LITELLM_MASTER_KEY=sk-change-me", f"LITELLM_MASTER_KEY=sk-{secrets.token_urlsafe(24)}")
    text = text.replace("LITELLM_SALT_KEY=change-me-too", f"LITELLM_SALT_KEY={secrets.token_urlsafe(32)}")
    text = text.replace("POSTGRES_PASSWORD=change-me-db", f"POSTGRES_PASSWORD={secrets.token_urlsafe(16)}")
    ENV_FILE.write_text(text, encoding="utf-8")
    print("Created .env with random secrets.")


def load_env() -> dict[str, str]:
    env = dict(os.environ)
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env.setdefault(k.strip(), v.strip())
    env["PYTHONUTF8"] = "1"
    return env


def start_postgres() -> tuple[object, str]:
    import pgserver  # embedded Postgres binaries, no system install needed

    data = ROOT / ".data" / "pg"
    data.mkdir(parents=True, exist_ok=True)
    server = pgserver.get_server(str(data), cleanup_mode="stop")
    server.psql("SELECT 1;")
    return server, server.get_uri()


def ensure_prisma_client(env: dict[str, str]) -> None:
    """Generate the Prisma client from LiteLLM's schema once (the Docker image does this at build)."""
    from importlib.metadata import version
    from importlib.util import find_spec

    schema = Path(find_spec("litellm").origin).parent / "proxy" / "schema.prisma"
    marker = ROOT / ".data" / f"prisma-generated-{version('litellm')}"
    if marker.exists():
        return
    print("Generating Prisma client for LiteLLM (one time)...")
    scripts = Path(PY).parent
    gen_env = dict(env, PATH=f"{scripts}{os.pathsep}{env.get('PATH', '')}")
    subprocess.run([str(scripts / "prisma"), "generate", "--schema", str(schema)],
                   cwd=ROOT, env=gen_env, check=True)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()


def wait_http(url: str, name: str, proc: subprocess.Popen, timeout_s: float = 180) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"{name} exited early; see logs/{name}.log")
        try:
            if httpx.get(url, timeout=3).status_code < 500:
                print(f"  {name} ready: {url}")
                return
        except httpx.HTTPError:
            pass
        time.sleep(1.5)
    raise RuntimeError(f"{name} did not start; see logs/{name}.log")


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    ensure_env_file()
    env = load_env()
    (ROOT / "logs").mkdir(exist_ok=True)
    ensure_prisma_client(env)

    print("Starting embedded Postgres...")
    pg, uri = start_postgres()
    env["DATABASE_URL"] = uri
    print(f"  postgres ready ({uri.split('@')[-1]})")

    procs: list[subprocess.Popen] = []

    def spawn(name: str, args: list[str]) -> None:
        log = open(ROOT / "logs" / f"{name}.log", "w", encoding="utf-8")
        procs.append(subprocess.Popen(args, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT))

    try:
        spawn("mock", [PY, "-m", "uvicorn", "mock_provider.app:app", "--port", "8001"])
        wait_http("http://localhost:8001/health", "mock", procs[-1])
        spawn("proxy", [PY, "-m", "litellm.proxy.proxy_cli",
                        "--config", "gateway/config.local.yaml", "--port", "4000"])
        spawn("dashboard", [PY, "-m", "streamlit", "run", "dashboard/app.py",
                            "--server.headless", "true", "--server.port", "8501",
                            "--browser.gatherUsageStats", "false"])
        wait_http("http://localhost:8501", "dashboard", procs[-1])
        print("  proxy starting (first run migrates the DB, can take a minute)...")
        wait_http("http://localhost:4000/health/liveliness", "proxy", procs[1], timeout_s=300)
        print("\nAll up. Now run:  python scripts/demo.py")
        print("Dashboard: http://localhost:8501   LiteLLM admin UI: http://localhost:4000/ui")
        print("Ctrl+C to stop.")
        while all(p.poll() is None for p in procs):
            time.sleep(1)
        print("A process exited; check logs/*.log")
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        del pg  # cleanup_mode="stop" shuts Postgres down


if __name__ == "__main__":
    raise SystemExit(main())
