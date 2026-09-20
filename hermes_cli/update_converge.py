"""Idle/after-turn converge onto a locally pinned SHA.

Generic: Hermes never talks to fleetctl. Anything may write ``updates.pin``
or ``{HERMES_HOME}/updates.pin``. The tick is ``hermes update --converge``
and must run *outside* the gateway process tree (LaunchAgent/systemd timer).
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_PIN_RE = re.compile(r"^[0-9a-f]{7,40}$")
_DEFAULT_INTERVAL = 1800
_DEFAULT_BUSY_SLA = 21600.0  # 6h
_STATE_NAME = "converge_state.json"
_PIN_FILE_NAME = "updates.pin"


@dataclass(frozen=True)
class ConvergeSettings:
    enabled: bool
    pin: str
    interval: int
    busy_sla: float
    skip_gateway_restart: bool


@dataclass(frozen=True)
class ConvergeDecision:
    action: str  # skip | update | restart
    reason: str
    pin: str = ""


def default_pin_path(home: Optional[Path] = None) -> Path:
    from hermes_constants import get_hermes_home

    return Path(home if home is not None else get_hermes_home()) / _PIN_FILE_NAME


def normalize_pin(raw: object) -> str:
    """Return a lowercase hex SHA prefix, or '' if missing/malformed."""
    text = str(raw or "").strip().split()[0] if raw else ""
    text = text.lower().removeprefix("sha:")
    return text if text and _PIN_RE.fullmatch(text) else ""


def read_pin_file(path: Path) -> str:
    try:
        return normalize_pin(path.read_text(encoding="utf-8").splitlines()[0] if path.is_file() else "")
    except OSError:
        return ""


def load_converge_settings(cfg: Optional[dict[str, Any]] = None) -> ConvergeSettings:
    if cfg is None:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
    raw_updates = cfg.get("updates")
    updates: dict[str, Any] = raw_updates if isinstance(raw_updates, dict) else {}
    pin = normalize_pin(updates.get("pin"))
    if not pin:
        pin_file = str(updates.get("pin_file") or "").strip()
        pin = read_pin_file(Path(pin_file).expanduser()) if pin_file else read_pin_file(default_pin_path())
    try:
        interval = int(updates.get("converge_interval") or _DEFAULT_INTERVAL)
    except (TypeError, ValueError):
        interval = _DEFAULT_INTERVAL
    raw_sla = updates.get("converge_busy_sla")
    try:
        sla = _DEFAULT_BUSY_SLA if raw_sla in (None, "") else float(raw_sla)
    except (TypeError, ValueError):
        sla = _DEFAULT_BUSY_SLA
    return ConvergeSettings(
        enabled=bool(updates.get("converge")),
        pin=pin,
        interval=max(60, interval),
        busy_sla=max(0.0, sla),
        skip_gateway_restart=bool(updates.get("skip_gateway_restart")),
    )


def prefixes_match(left: str, right: str) -> bool:
    """True when two SHA prefixes name the same commit (either may be short)."""
    a, b = normalize_pin(left), normalize_pin(right)
    if not a or not b:
        return False
    n = min(len(a), len(b))
    return n >= 7 and a[:n] == b[:n]


def decide_converge(
    *,
    settings: ConvergeSettings,
    checkout_sha: str,
    live_sha: str,
    dirty: bool,
    busy: bool,
    pin_age_s: float,
) -> ConvergeDecision:
    """Pure policy. ``pin_age_s`` is how long this pin has been visible on the host."""
    if not settings.enabled:
        return ConvergeDecision("skip", "disabled")
    if not settings.pin:
        if dirty:
            return ConvergeDecision("skip", "dirty_tree")
        if busy and pin_age_s < settings.busy_sla:
            return ConvergeDecision("skip", "busy")
        return ConvergeDecision("update", "channel_tip")
    pin = settings.pin
    if dirty and not prefixes_match(checkout_sha, pin):
        return ConvergeDecision("skip", "dirty_tree", pin)
    at_pin = prefixes_match(checkout_sha, pin)
    live_at_pin = prefixes_match(live_sha, pin) if live_sha else at_pin
    if at_pin and (live_at_pin or settings.skip_gateway_restart):
        return ConvergeDecision("skip", "already_current", pin)
    if busy and pin_age_s < settings.busy_sla:
        return ConvergeDecision("skip", "busy", pin)
    if at_pin:
        return ConvergeDecision("restart", "stale_runtime", pin)
    return ConvergeDecision("update", "checkout_behind_pin", pin)


def _state_path(home: Optional[Path] = None) -> Path:
    from hermes_constants import get_hermes_home

    return Path(home if home is not None else get_hermes_home()) / _STATE_NAME


def pin_age_seconds(pin: str, home: Optional[Path] = None, *, now: Optional[float] = None) -> float:
    """Age of the current pin on this host; first sighting starts the SLA clock."""
    now = time.time() if now is None else now
    path = _state_path(home)
    body: dict[str, Any] = {}
    try:
        body = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if not isinstance(body, dict):
            body = {}
    except (OSError, ValueError, TypeError):
        body = {}
    seen = str(body.get("pin") or "")
    first = body.get("first_seen")
    if not prefixes_match(seen, pin) or not isinstance(first, (int, float)):
        body = {"pin": pin, "first_seen": now}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(body) + "\n", encoding="utf-8")
        except OSError as e:
            logger.debug("converge state write failed: %s", e)
        return 0.0
    return max(0.0, now - float(first))


def checkout_sha(project_root: Path) -> str:
    try:
        r = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        return normalize_pin(r.stdout)
    except (OSError, subprocess.TimeoutExpired):
        return ""


def checkout_is_dirty(project_root: Path) -> bool:
    try:
        r = subprocess.run(
            ["git", "-C", str(project_root), "status", "--porcelain"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        return bool(r.stdout.strip()) if r.returncode == 0 else True
    except (OSError, subprocess.TimeoutExpired):
        return True


def live_code_sha() -> str:
    try:
        from gateway.status import get_running_pid, read_runtime_status

        rec = read_runtime_status() or {}
        if get_running_pid() is None:
            return ""
        return normalize_pin(rec.get("code_sha"))
    except Exception:
        return ""


def gateway_is_busy() -> bool:
    try:
        from gateway.status import derive_gateway_busy, get_running_pid, read_runtime_status

        rec = read_runtime_status() or {}
        return derive_gateway_busy(
            gateway_running=get_running_pid() is not None,
            gateway_state=rec.get("gateway_state"),
            active_agents=rec.get("active_agents"),
        )
    except Exception:
        return False


def _log_tick(message: str) -> None:
    try:
        from hermes_constants import get_hermes_home

        path = get_hermes_home() / "logs" / "converge.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        line = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + " " + message + "\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        logger.debug("converge log write failed", exc_info=True)


def decide_from_live(project_root: Path, settings: Optional[ConvergeSettings] = None) -> ConvergeDecision:
    settings = settings or load_converge_settings()
    pin = settings.pin
    age = pin_age_seconds(pin or "channel") if settings.enabled else 0.0
    return decide_converge(
        settings=settings,
        checkout_sha=checkout_sha(project_root),
        live_sha=live_code_sha(),
        dirty=checkout_is_dirty(project_root),
        busy=gateway_is_busy(),
        pin_age_s=age,
    )


def _mark_planned_drain() -> None:
    try:
        from gateway.drain_control import write_drain_request

        write_drain_request(principal="update-converge", suppress_notification=True)
    except Exception as e:
        logger.debug("drain marker write failed: %s", e)


def cmd_converge_tick(args: Any) -> None:
    """Entry for ``hermes update --converge`` / ``hermes converge run``. Quiet on skip."""
    from hermes_cli.main import PROJECT_ROOT, cmd_update

    settings = load_converge_settings()
    decision = decide_from_live(PROJECT_ROOT, settings)
    _log_tick(f"action={decision.action} reason={decision.reason} pin={decision.pin[:12]}")
    if decision.action == "skip":
        return
    if settings.skip_gateway_restart and decision.action == "restart":
        _log_tick("skip restart (updates.skip_gateway_restart)")
        return
    args.converge = False
    args.yes = True
    if decision.pin:
        args.sha = decision.pin
    if settings.skip_gateway_restart:
        args.no_gateway_restart = True
    _mark_planned_drain()
    if decision.action == "restart":
        from hermes_cli.update_cmd_fleet import _restart_gateway_fleet_after_update

        _restart_gateway_fleet_after_update(None, gateway_mode=False)
        return
    cmd_update(args)


def converge_label() -> str:
    from hermes_cli.gateway import _profile_suffix

    suffix = _profile_suffix()
    return f"ai.hermes.converge-{suffix}" if suffix else "ai.hermes.converge"


def converge_plist_path() -> Path:
    import pwd

    home = Path(pwd.getpwuid(os.getuid()).pw_dir)  # windows-footgun: ok — POSIX launchd (macOS) helper, never invoked on Windows
    return home / "Library" / "LaunchAgents" / f"{converge_label()}.plist"


def generate_converge_plist(settings: Optional[ConvergeSettings] = None) -> str:
    from hermes_cli.gateway import _service_venv_dir, _stable_service_working_dir
    from hermes_constants import get_hermes_home

    settings = settings or load_converge_settings()
    hermes_home = str(get_hermes_home().resolve())
    log_dir = get_hermes_home() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    venv_dir = _service_venv_dir()
    hermes_bin = str(Path(venv_dir) / "bin" / "hermes")
    label = converge_label()
    working_dir = _stable_service_working_dir()
    interval = settings.interval
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{hermes_bin}</string>
        <string>update</string>
        <string>--converge</string>
        <string>-y</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{working_dir}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HERMES_HOME</key>
        <string>{hermes_home}</string>
        <key>VIRTUAL_ENV</key>
        <string>{venv_dir}</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>StartInterval</key>
    <integer>{interval}</integer>
    <key>StandardOutPath</key>
    <string>{log_dir}/converge.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/converge.log</string>
</dict>
</plist>
"""


def install_converge_agent(*, force: bool = False) -> int:
    """Write and load the macOS LaunchAgent. No-op (0) on non-macOS."""
    if sys.platform != "darwin":
        print("converge agent install is macOS LaunchAgent-only; on Linux run `hermes update --converge -y` from a systemd timer.")
        return 0
    from hermes_cli.gateway import _launchd_domain, _launchctl_bootstrap, _refuse_temp_home_service_write

    settings = load_converge_settings()
    if not settings.enabled and not force:
        print("updates.converge is false — not installing (pass install --force to write the plist anyway).")
        return 1
    plist_path = converge_plist_path()
    body = generate_converge_plist(settings)
    if _refuse_temp_home_service_write(body, "converge launchd plist"):
        return 1
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(body, encoding="utf-8")
    label = converge_label()
    domain = _launchd_domain()
    try:
        import subprocess

        subprocess.run(
            ["launchctl", "bootout", f"{domain}/{label}"],
            check=False, timeout=30, capture_output=True,
        )
        _launchctl_bootstrap(domain, plist_path, label, timeout=30)
    except Exception as e:
        print(f"⚠ Wrote {plist_path} but launchctl load failed: {e}")
        return 1
    print(f"✓ Converge agent installed: {label} every {settings.interval}s")
    print(f"  {plist_path}")
    return 0


def uninstall_converge_agent() -> int:
    if sys.platform != "darwin":
        return 0
    from hermes_cli.gateway import _launchd_domain

    plist_path = converge_plist_path()
    label = converge_label()
    try:
        import subprocess

        subprocess.run(
            ["launchctl", "bootout", f"{_launchd_domain()}/{label}"],
            check=False, timeout=30, capture_output=True,
        )
    except Exception:
        pass
    if plist_path.exists():
        plist_path.unlink()
        print(f"✓ Removed {plist_path}")
    else:
        print("converge agent was not installed")
    return 0


def maybe_install_converge_agent() -> None:
    """Best-effort hook from ``hermes gateway install`` when converge is enabled."""
    if sys.platform != "darwin":
        return
    if not load_converge_settings().enabled:
        return
    try:
        install_converge_agent()
    except Exception as e:
        logger.warning("converge agent install skipped: %s", e)


def cmd_converge(args: Any) -> None:
    action = getattr(args, "converge_action", None) or "run"
    if action == "install":
        sys.exit(install_converge_agent(force=bool(getattr(args, "force", False))))
    if action == "uninstall":
        sys.exit(uninstall_converge_agent())
    if action == "status":
        settings = load_converge_settings()
        from hermes_cli.main import PROJECT_ROOT

        d = decide_from_live(PROJECT_ROOT, settings)
        print(f"enabled={settings.enabled} pin={settings.pin or '-'} skip_restart={settings.skip_gateway_restart}")
        print(f"action={d.action} reason={d.reason}")
        if sys.platform == "darwin":
            print(f"plist={converge_plist_path()} exists={converge_plist_path().exists()}")
        return
    cmd_converge_tick(args)
