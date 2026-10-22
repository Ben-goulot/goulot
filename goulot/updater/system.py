"""The real side effects of an update on the Mac: uv, the new version's own commands, launchd, the health probe,
the alerts (docs/UPDATES.md). Fixed absolute paths, argument lists, no shell, time limits.

The only network exit outside the allowlist client is `uv sync --locked --no-dev --no-build`: wheels whose hash is
in the signed `uv.lock`, nothing built from sources, no Python download.
"""

from __future__ import annotations

import http.client
import json
import os
import plistlib
import shutil
import subprocess  # nosec B404 - fixed programs, argument lists, no shell
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from goulot.security.fs import ensure_private_dir
from goulot.settings import Settings
from goulot.updater import github
from goulot.updater.install import Hooks, Offer
from goulot.updater.manifest import UpdateRefused
from goulot.updater.state import Layout
from goulot.updater.trust import TRUSTED_KEYS

APP_LABEL = "com.goulot.app"
UPDATER_LABEL = "com.goulot.updater"
LABELS = (APP_LABEL, UPDATER_LABEL)
LAUNCHCTL = "/bin/launchctl"
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
UV_CANDIDATES = ("/opt/homebrew/bin/uv", "/usr/local/bin/uv", "~/.local/bin/uv", "~/.cargo/bin/uv")
HEALTH_TIMEOUT_S = 60
RELEASE_CHECK_TIMEOUT_S = 300


def find_uv() -> str | None:
    for candidate in UV_CANDIDATES:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    found = shutil.which("uv")
    return str(Path(found).resolve()) if found else None


def _run(args: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
         timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout,  # noqa: S603  # nosec B603
                          check=False)


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    return lines[-1][:200] if lines else "sans message"


def base_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {"HOME": str(Path.home()), "PATH": SYSTEM_PATH, "LANG": "fr_FR.UTF-8"}
    if os.environ.get("GOULOT_HOME"):
        env["GOULOT_HOME"] = os.environ["GOULOT_HOME"]
    env.update(extra or {})
    return env


def sync_env(uv: str, release: Path) -> None:
    env = base_env({"UV_PROJECT_ENVIRONMENT": str(release / ".venv"), "UV_PYTHON_DOWNLOADS": "never",
                    "UV_NO_CONFIG": "1"})
    out = _run([uv, "sync", "--locked", "--no-dev", "--no-build", "--project", str(release)], cwd=release, env=env,
               timeout=900)
    if out.returncode != 0:
        raise RuntimeError(f"uv sync : {_last_line(out.stderr)}")


def goulot_command(release: Path, args: list[str]) -> list[str]:
    return [str(release / ".venv" / "bin" / "python"), "-P", "-m", "goulot", *args]


def run_release(release: Path, args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return _run(goulot_command(release, args), cwd=release, env=base_env({"PYTHONPATH": str(release)}),
                timeout=timeout)


def release_check(release: Path) -> None:
    try:
        out = run_release(release, ["doctor", "--release", str(release)], RELEASE_CHECK_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise UpdateRefused("test de santé trop long (accès au trousseau en attente ?)") from exc
    if out.returncode != 0:
        raise UpdateRefused(f"test de santé échoué : {_last_line(out.stdout + out.stderr)}")


def migrate(release: Path) -> None:
    out = run_release(release, ["db", "migrate"], 300)
    if out.returncode != 0:
        raise RuntimeError(_last_line(out.stdout + out.stderr))


# --- launchd ---------------------------------------------------------------------------------------------
def domain() -> str:
    return f"gui/{os.getuid()}"


def agent_path(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def agent_loaded(label: str) -> bool:
    return _run([LAUNCHCTL, "print", f"{domain()}/{label}"], timeout=10).returncode == 0


def port_open(port: int) -> bool:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        conn.connect()
        return True
    except OSError:
        return False
    finally:
        conn.close()


def stop_app(port: int) -> None:
    """Unload the app's agent: launchd stops it and does not restart it (uvicorn ends on the signal itself, which
    KeepAlive would take for a crash). The plist stays in place for `start_app`."""
    if not agent_loaded(APP_LABEL):
        return
    _run([LAUNCHCTL, "bootout", f"{domain()}/{APP_LABEL}"], timeout=60)
    deadline = time.monotonic() + 30
    while port_open(port) and time.monotonic() < deadline:
        time.sleep(0.5)


def start_app() -> None:
    """Load the app's agent again (RunAtLoad starts it), or restart it when it is still loaded."""
    if agent_loaded(APP_LABEL):
        _run([LAUNCHCTL, "kickstart", "-k", f"{domain()}/{APP_LABEL}"], timeout=30)
    elif agent_path(APP_LABEL).exists():
        _run([LAUNCHCTL, "bootstrap", domain(), str(agent_path(APP_LABEL))], timeout=30)


def healthz(port: int) -> dict[str, Any] | None:
    """Local probe of the app (loopback, not an outbound request)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        conn.request("GET", "/healthz", headers={"Host": f"localhost:{port}"})
        response = conn.getresponse()
        if response.status != 200:
            return None
        data: dict[str, Any] = json.loads(response.read(4096))
        return data
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        conn.close()


def healthy(port: int, version: str, timeout: float = HEALTH_TIMEOUT_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = healthz(port)
        if data and data.get("status") == "ok" and data.get("version") == version:
            return True
        time.sleep(1)
    return False


def render_agent(template: Path, current: Path, logs: Path) -> bytes:
    """Fill a LaunchAgent template (ops/launchd/) with plistlib: no text substitution in XML."""
    data = plistlib.loads(template.read_bytes())

    def fill(value: Any) -> Any:
        if isinstance(value, str):
            return value.replace("@CURRENT@", str(current)).replace("@LOGS@", str(logs))
        if isinstance(value, list):
            return [fill(v) for v in value]
        if isinstance(value, dict):
            return {k: fill(v) for k, v in value.items()}
        return value

    return plistlib.dumps(fill(data))


def install_agents(release: Path, layout: Layout, settings: Settings) -> list[str]:
    """Write and load both LaunchAgents, always pointing at `current`. Asked to the user beforehand."""
    folder = Path.home() / "Library" / "LaunchAgents"
    folder.mkdir(parents=True, exist_ok=True)
    ensure_private_dir(settings.log_dir)  # launchd creates the log files, not their folder
    loaded = []
    for label in LABELS:
        target = agent_path(label)
        content = render_agent(release / "ops" / "launchd" / f"{label}.plist", layout.current, settings.log_dir)
        if agent_loaded(label):
            _run([LAUNCHCTL, "bootout", f"{domain()}/{label}"], timeout=30)
        tmp = target.with_suffix(".plist.new")
        tmp.write_bytes(content)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
        out = _run([LAUNCHCTL, "bootstrap", domain(), str(target)], timeout=30)
        if out.returncode != 0:
            raise RuntimeError(f"launchctl bootstrap {label} : {_last_line(out.stderr)}")
        loaded.append(label)
    return loaded


def stop_agents() -> list[str]:
    """`goulot panic`: unload both agents (their files stay; `launchctl bootstrap` reloads them)."""
    stopped = []
    for label in LABELS:
        if agent_loaded(label):
            _run([LAUNCHCTL, "bootout", f"{domain()}/{label}"], timeout=30)
            stopped.append(label)
    return stopped


# --- alerts ----------------------------------------------------------------------------------------------
def notifier(settings: Settings) -> Any:
    def notify(key: str, text: str) -> None:
        from goulot.alerts.service import Alert, dispatch
        from goulot.api.context import build_context
        from goulot.pipeline.cycle import sender

        if not settings.db_path.exists():
            print(text)
            return
        try:
            ctx = build_context(settings)
            dispatch(ctx.db, [Alert(kind="update", dedupe_key=key, text=text, urgent=True)], sender(ctx),
                     datetime.now(UTC))
            ctx.jobs.shutdown(wait=False)
        except Exception as exc:  # an alert must never break an update or a rollback
            print(f"alerte non envoyée ({type(exc).__name__})")
        print(text)

    return notify


def live_hooks(settings: Settings, uv: str, keys: tuple[tuple[str, str], ...] = TRUSTED_KEYS) -> Hooks:
    def fetch() -> Offer:
        client = github.updater_client()  # closed with the process: one update run, then exit
        assets = github.latest_release(client)
        manifest, signature = github.download_manifest(client, assets)
        return Offer(manifest, signature, lambda size: github.download_archive(client, assets, size))

    return Hooks(
        fetch=fetch,
        sync_env=lambda release: sync_env(uv, release),
        release_check=release_check,
        migrate=migrate,
        stop_app=lambda: stop_app(settings.port),
        start_app=start_app,
        healthy=lambda version: healthy(settings.port, version),
        notify=notifier(settings),
        keys=keys,
    )
