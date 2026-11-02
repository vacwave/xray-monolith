"""Find, launch and stop the game process; locate its log files."""

import shutil
import subprocess
import time
from pathlib import Path

import psutil

from . import config
from .engine_client import EngineClient, EngineError, EngineNotRunning, EngineTimeout

LAUNCH_ARGS = ["-smap2048", "-dbg"]  # same as build.ps1 -Launch
LAUNCH_WAIT = 90.0


def find_game() -> psutil.Process | None:
    name = config.exe_name().lower()
    for proc in psutil.process_iter(["name"]):
        if (proc.info["name"] or "").lower() == name:
            return proc
    return None


def _client() -> EngineClient:
    return EngineClient(config.HOST, config.port())


def launch_game(clear_shader_cache: bool = False) -> str:
    proc = find_game()
    if proc is not None:
        return f"Refused: {config.exe_name()} is already running (pid {proc.pid})."
    bin_dir = config.anomaly_dir() / "bin"
    exe = bin_dir / config.exe_name()
    if not exe.is_file():
        return f"Cannot launch: {exe} does not exist."

    lines = []
    if clear_shader_cache:
        cache = config.anomaly_dir() / "appdata" / "shaders_cache"
        if cache.exists():
            shutil.rmtree(cache)
            lines.append(f"Deleted {cache}.")
        else:
            lines.append(f"No shader cache at {cache}.")

    # Detached, with no inherited stdio: our stdout is the MCP stream.
    popen = subprocess.Popen(
        [str(exe), *LAUNCH_ARGS],
        cwd=bin_dir,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    lines.append(f"Started {exe} {' '.join(LAUNCH_ARGS)} (pid {popen.pid}).")

    client = _client()
    start = time.monotonic()
    while time.monotonic() - start < LAUNCH_WAIT:
        code = popen.poll()
        if code is not None:
            lines.append(f"Process exited with code {code} after {time.monotonic() - start:.0f} s; check log_tail.")
            return "\n".join(lines)
        try:
            status = client.status()
            lines.append(f"Endpoint answered after {time.monotonic() - start:.0f} s: {status}.")
            return "\n".join(lines)
        except (EngineNotRunning, EngineTimeout, EngineError):
            time.sleep(1.0)
    lines.append(f"Process is running but the endpoint did not answer within {LAUNCH_WAIT:.0f} s.")
    return "\n".join(lines)


def kill_game() -> str:
    proc = find_game()
    if proc is None:
        return f"{config.exe_name()} is not running."
    pid = proc.pid
    # Graceful first: ask the engine to quit. The reply may never come.
    try:
        _client().request("console", "quit", timeout=2.0)
    except (EngineNotRunning, EngineTimeout, EngineError):
        pass
    try:
        proc.wait(timeout=10)
        return f"{config.exe_name()} (pid {pid}) quit gracefully."
    except psutil.TimeoutExpired:
        pass
    except psutil.NoSuchProcess:
        return f"{config.exe_name()} (pid {pid}) quit gracefully."
    try:
        proc.kill()
        proc.wait(timeout=10)
    except psutil.NoSuchProcess:
        pass
    except psutil.TimeoutExpired:
        return f"Killed {config.exe_name()} (pid {pid}) but it has not exited after 10 s."
    return f"{config.exe_name()} (pid {pid}) did not quit within 10 s; killed it."


def newest_log_file() -> Path | None:
    logs = config.anomaly_dir() / "appdata" / "logs"
    files = [p for p in logs.glob("*.log") if p.is_file()]
    return max(files, key=lambda p: p.stat().st_mtime, default=None)
