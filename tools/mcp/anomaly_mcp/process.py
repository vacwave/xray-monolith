"""Find, launch and stop the game process; locate its log files."""

import ctypes
import shutil
import subprocess
import time
from ctypes import wintypes
from pathlib import Path

import psutil

from . import config, game
from .engine_client import EngineClient, EngineError, EngineNotRunning, EngineTimeout

LAUNCH_ARGS = ["-smap2048", "-dbg"]  # same as build.ps1 -Launch
LAUNCH_WAIT = 90.0
LAUNCH_CONSOLE = ["g_always_active on", "keypress_on_start off"]
WINDOW_TITLE = "S.T.A.L.K.E.R.: Anomaly"
SW_MINIMIZE, SW_RESTORE = 6, 9


def find_game() -> psutil.Process | None:
    name = config.exe_name().lower()
    for proc in psutil.process_iter(["name"]):
        if (proc.info["name"] or "").lower() == name:
            return proc
    return None


def _client() -> EngineClient:
    return EngineClient(config.HOST, config.port())


def _user32():
    return ctypes.windll.user32


def find_window(pid: int) -> int | None:
    """A visible top-level window of process pid (preferring the game's title), or None."""
    user32 = _user32()
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            title = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, title, 256)
            found.append((WINDOW_TITLE not in title.value, hwnd))
        return True

    user32.EnumWindows(visit, 0)
    return min(found)[1] if found else None


def window_action(pid: int, action: str) -> str:
    """Minimize, restore or focus the game window of pid. Returns a short report."""
    if action not in ("minimize", "restore", "focus"):
        return f"Unknown action {action!r}; use minimize, restore or focus."
    hwnd = find_window(pid)
    if hwnd is None:
        return f"Game window not found (pid {pid})."
    user32 = _user32()
    if action == "minimize":
        user32.ShowWindow(hwnd, SW_MINIMIZE)
        return "Window minimized." if user32.IsIconic(hwnd) else "Asked the window to minimize; it is not minimized."
    user32.ShowWindow(hwnd, SW_RESTORE)
    if action == "restore":
        return "Window restored."
    if user32.SetForegroundWindow(hwnd):
        return "Window restored and focused."
    return "Window restored; Windows refused to bring it to the foreground."


def game_window(action: str) -> str:
    proc = find_game()
    if proc is None:
        return f"{config.exe_name()} is not running."
    return window_action(proc.pid, action)


def launch_game(clear_shader_cache: bool = False, test_map: bool = False, minimize: bool = True) -> str:
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
            break
        except (EngineNotRunning, EngineTimeout, EngineError):
            time.sleep(1.0)
    else:
        lines.append(f"Process is running but the endpoint did not answer within {LAUNCH_WAIT:.0f} s.")
        return "\n".join(lines)
    lines.append(f"Endpoint answered after {time.monotonic() - start:.0f} s: {status}.")

    try:
        # Keep running and rendering while unfocused/minimized; no "press any key" after loads.
        for cmd in LAUNCH_CONSOLE:
            client.request("console", cmd, timeout=30)
        lines.append(f"Ran {', '.join(LAUNCH_CONSOLE)}.")
        if test_map:
            lines.append(game.start_test_map())
    except (EngineNotRunning, EngineTimeout, EngineError) as e:
        lines.append(f"Setup after launch failed: {e}")
    if minimize:
        lines.append(window_action(popen.pid, "minimize"))
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
