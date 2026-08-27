"""IB Gateway lifecycle helpers.

The design goal is that a portfolio read never requires human interaction:
IBC keeps one long-lived, logged-in Gateway process around, and each read is
just a local socket connection to it. This module only probes that socket and,
if asked, delegates start/stop to scripts/gateway-*.sh.
"""

from __future__ import annotations

import socket
import subprocess
import time
import os
from dataclasses import dataclass
from pathlib import Path

from .config import PROJECT_ROOT, Settings

SCRIPTS_DIR = PROJECT_ROOT / "scripts"


@dataclass(frozen=True)
class GatewayStatus:
    host: str
    port: int
    listening: bool
    process_running: bool

    @property
    def ready(self) -> bool:
        return self.listening


def is_port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    """True if something accepts TCP connections on host:port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def gateway_process_running() -> bool:
    """True if an IBC-launched Gateway JVM is alive."""
    result = subprocess.run(
        ["pgrep", "-f", "ibcalpha.ibc.IbcGateway"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def status(settings: Settings) -> GatewayStatus:
    return GatewayStatus(
        host=settings.host,
        port=settings.port,
        listening=is_port_open(settings.host, settings.port),
        process_running=gateway_process_running(),
    )


def _run_script(
    name: str, timeout: float, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    script: Path = SCRIPTS_DIR / name
    if not script.exists():
        raise FileNotFoundError(f"missing script: {script}")
    return subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env={**os.environ, **(env or {})},
    )


def launch(timeout: float = 120) -> subprocess.CompletedProcess[str]:
    """Start Xvfb and the Gateway, returning as soon as the JVM is spawned.

    Unlike `ensure_running`, this does not wait for the API port. That matters
    for the 2FA path: the port cannot open until a code has been typed into the
    dialog, so the caller needs control back while the dialog is still up. See
    `login.run_login`.
    """
    return _run_script("gateway-up.sh", timeout=timeout, env={"GATEWAY_NO_WAIT": "1"})


def ensure_running(settings: Settings, wait_seconds: float = 180) -> GatewayStatus:
    """Make sure the API port is reachable, starting the Gateway if allowed.

    Returns the resulting status; the caller decides whether to fail.
    """
    current = status(settings)
    if current.ready or not settings.auto_start_gateway:
        return current

    _run_script("gateway-up.sh", timeout=wait_seconds + 30)

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if is_port_open(settings.host, settings.port):
            break
        time.sleep(3)
    return status(settings)


def stop() -> subprocess.CompletedProcess[str]:
    return _run_script("gateway-down.sh", timeout=90)
