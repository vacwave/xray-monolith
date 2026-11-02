"""Multi-step game actions built on lua_eval: test map, teleport, screenshot.

They call the Lua helpers in gamedata/scripts/mcp_helper.script and raise the
EngineClient exceptions (EngineNotRunning/EngineTimeout/EngineError); the
callers turn those into text.
"""

import time
from datetime import datetime
from pathlib import Path

from PIL import Image

from . import config
from .engine_client import EngineClient, EngineError, EngineTimeout

POLL = 1.0  # seconds between readiness polls
MENU_WAIT = 60.0
LEVEL_WAIT = 120.0
SHOT_WAIT = 5.0
EVAL_TIMEOUT = 10.0

# precache_frame > 0 while the loading screen is still up (the actor already exists then)
LEVEL_NAME = "return (db.actor ~= nil and device().precache_frame == 0) and level.name() or ''"
MENU_READY = "return _G.main_menu ~= nil and _G.main_menu.get_main_menu() ~= nil"
ACTOR_POS = "return db.actor:position()"
UNPAUSE = "device():pause(false)"


def _client() -> EngineClient:
    return EngineClient(config.HOST, config.port())


def _lua_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def wait_until(predicate, timeout: float, interval: float | None = None):
    """Call predicate() every `interval` s (default POLL) until it returns something truthy
    (returned) or `timeout` s pass (None)."""
    interval = POLL if interval is None else interval
    deadline = time.monotonic() + timeout
    while True:
        result = predicate()
        if result:
            return result
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


def _poll_eval(client: EngineClient, code: str) -> str | None:
    """Eval for readiness polls: None while the engine is busy (queued eval, level loading)."""
    if client.status().get("pending"):
        return None  # an earlier poll is still queued; don't pile up more
    try:
        return client.request("eval", code, timeout=5)
    except (EngineTimeout, EngineError):
        return None


def wait_for_menu(client: EngineClient) -> bool:
    return wait_until(lambda: _poll_eval(client, MENU_READY) == "true", MENU_WAIT) is not None


def wait_for_level(client: EngineClient, frame0: int, target: str | None = None) -> str | None:
    """Wait until a level with an actor is loaded (named `target`, if given); return its name.

    frame0 is the status frame from before the command that changes the level: the
    level must not be checked before the engine has run a few frames since, or the
    old level may still answer.
    """

    def ready():
        status = client.status()
        if not status.get("level") or abs(status.get("frame", 0) - frame0) < 3:
            return None
        name = _poll_eval(client, LEVEL_NAME)
        return name if name and (target is None or name == target) else None

    name = wait_until(ready, LEVEL_WAIT)
    if name:
        # a level change leaves the device paused; nobody unpauses it in an unattended run
        client.request("eval", UNPAUSE, timeout=EVAL_TIMEOUT)
    return name


def start_test_map() -> str:
    client = _client()
    status = client.status()
    if not status.get("level") and not wait_for_menu(client):
        return f"Main menu not ready after {MENU_WAIT:.0f} s; test map not started."
    frame0 = client.status().get("frame", 0)
    result = client.request("eval", "return mcp_helper.start_test_map()", timeout=EVAL_TIMEOUT)
    if result != "started":
        return f"start_test_map failed: {result}"
    name = wait_for_level(client, frame0)
    if name is None:
        return f"Test map started but no level with an actor after {LEVEL_WAIT:.0f} s; check game_status and log_tail."
    pos = client.request("eval", ACTOR_POS, timeout=EVAL_TIMEOUT)
    return f"Test map loaded: level {name}, actor at {pos}."


def teleport(x: float, y: float, z: float, level: str | None = None) -> str:
    client = _client()
    lines = []
    if level:
        current = client.request("eval", LEVEL_NAME, timeout=EVAL_TIMEOUT)
        if not current:
            return "error: no actor (not in a level)"
        if current != level:
            frame0 = client.status().get("frame", 0)
            try:
                result = client.request("eval", f"return mcp_helper.jump_to_level({_lua_str(level)})", timeout=EVAL_TIMEOUT)
            except EngineTimeout:
                result = "changing"  # the level change can swallow the reply; wait and see
            if result != "changing":
                return result
            if wait_for_level(client, frame0, level) is None:
                return f"Level change {current} -> {level} did not finish within {LEVEL_WAIT:.0f} s; check game_status."
            lines.append(f"Changed level {current} -> {level}.")
    lines.append(client.request("eval", f"return mcp_helper.teleport({float(x)!r}, {float(y)!r}, {float(z)!r})", timeout=EVAL_TIMEOUT))
    return "\n".join(lines)


INPUT_MODES = ("tap", "hold", "press", "release")
HOLD_POLL = 0.1
MIN_FPS = 10.0  # worst case assumed when waiting for a hold to run its frames


def game_input(action: str, mode: str = "tap", frames: int = 60) -> str:
    """Press a game action (or DIK key) via mcp_helper.input; a hold waits until its frames have run."""
    if mode not in INPUT_MODES:
        return f"error: unknown mode {mode!r} (one of {', '.join(INPUT_MODES)})"
    frames = max(1, int(frames))
    client = _client()
    frame0 = client.status().get("frame", 0)
    result = client.request(
        "eval", f"return mcp_helper.input({_lua_str(action)}, {_lua_str(mode)}, {frames})", timeout=EVAL_TIMEOUT
    )
    if result.startswith("error") or mode != "hold":
        return result
    # the hold is released on the actor update `frames` frames later; wait for them to pass
    if not wait_for_frame(client, frame0 + frames + 1, frames / MIN_FPS + 2):
        return f"{result}\nheld, but the game did not run {frames} frames in time (paused?); the key is released on the next actor update"
    return f"{result}\nheld for {frames} frames"


def wait_for_frame(client: EngineClient, target: int, timeout: float) -> bool:
    """Poll status (no main-thread request) until its frame counter reaches `target`; False on timeout."""
    return wait_until(lambda: client.status().get("frame", 0) >= target, timeout, HOLD_POLL) is not None


def wait_frames(frames: int = 60, timeout: float | None = None) -> str:
    frames = max(1, int(frames))
    timeout = frames / MIN_FPS + 2 if timeout is None else float(timeout)
    client = _client()
    frame0 = client.status().get("frame", 0)
    start = time.monotonic()
    if wait_for_frame(client, frame0 + frames, timeout):
        return f"waited {frames} frames in {time.monotonic() - start:.2f} s"
    ran = client.status().get("frame", 0) - frame0
    return f"the game ran only {ran} of {frames} frames in {timeout:g} s (paused or loading?)"


SAVE_EXT = ".scop"
SAVE_WAIT = 10.0
BAD_SAVE_CHARS = '/\\:*?"<>|^()[]%'  # valid_saved_game_name() in console_commands.cpp
# console lines (from CCC_ALifeSave / CCC_ALifeLoadFrom) that mean the command did nothing
SAVE_LOAD_ERRORS = ("! Cannot", "! Save failed", "! ALife", "! Specify", "cannot make saved game", "for single-mode only")
PRE_LOAD = "_G.mcp_pre_load_actor = db.actor"
# the old level's actor is gone (rawequal: no metamethods on a possibly destroyed object)
LOAD_STARTED = "return db.actor == nil or not rawequal(db.actor, _G.mcp_pre_load_actor)"
POST_LOAD = "_G.mcp_pre_load_actor = nil"


def saves_dir() -> Path:
    return config.anomaly_dir() / "appdata" / "savedgames"


def _save_name_error(name: str) -> str | None:
    if not name.strip():
        return "error: empty save name"
    if name != name.strip() or any(c in BAD_SAVE_CHARS or ord(c) < 32 for c in name):
        return f"error: invalid save name {name!r} (no leading/trailing spaces, control characters or any of {BAD_SAVE_CHARS})"
    return None


def _console_error(output: str) -> bool:
    return any(line.lstrip().startswith(SAVE_LOAD_ERRORS) for line in output.splitlines())


def save_game(name: str) -> str:
    if err := _save_name_error(name):
        return err
    client = _client()
    if not client.status().get("level"):
        return "error: no level loaded (main menu); nothing to save"
    path = saves_dir() / f"{name}{SAVE_EXT}"
    before = path.stat().st_mtime_ns if path.is_file() else None
    output = client.request("console", f"save {name}", timeout=EVAL_TIMEOUT)
    if _console_error(output):
        return f"error: save {name} failed:\n{output}"
    last = {}

    def written():
        # the engine writes the file on the next frame; wait for a new mtime and a size that stopped changing
        try:
            st = path.stat()
        except OSError:
            return None
        if before is not None and st.st_mtime_ns <= before:
            return None
        done = st.st_size > 0 and last.get("size") == st.st_size
        last["size"] = st.st_size
        return st if done else None

    st = wait_until(written, SAVE_WAIT, 0.2)
    if st is None:
        return f"error: no new {path} after {SAVE_WAIT:.0f} s (game paused?)\nconsole: {output or '(no output)'}"
    return f"Saved {path} ({st.st_size} bytes).\nconsole: {output or '(no output)'}"


def load_game(name: str) -> str:
    if err := _save_name_error(name):
        return err
    path = saves_dir() / f"{name}{SAVE_EXT}"
    if not path.is_file():
        return f"error: no saved game {path}"
    client = _client()
    status = client.status()
    valid = client.request("eval", f"return valid_saved_game({_lua_str(name)})", timeout=EVAL_TIMEOUT)
    if valid != "true":
        return f"error: {path} is not a loadable saved game (version mismatch or corrupted)"
    frame0 = status.get("frame", 0)
    if status.get("level"):
        client.request("eval", PRE_LOAD, timeout=EVAL_TIMEOUT)
        cmds = [f"load {name}"]
    else:
        # no ALife at the main menu: start a server from the save, like ui_load_dialog.script
        cmds = ["disconnect", f"start server({name}/single/alife/load) client(localhost)"]
    outputs = []
    for cmd in cmds:
        try:
            outputs.append(client.request("console", cmd, timeout=EVAL_TIMEOUT))
        except EngineTimeout:
            outputs.append("")  # the load can swallow the reply; wait and see
    output = "\n".join(o for o in outputs if o)
    if _console_error(output):
        return f"error: load {name} failed:\n{output}"
    if status.get("level"):
        started = wait_until(lambda: _poll_eval(client, LOAD_STARTED) == "true", LEVEL_WAIT)
        client.request("eval", POST_LOAD, timeout=LEVEL_WAIT)
        if started is None:
            return f"error: load {name} did not start within {LEVEL_WAIT:.0f} s\nconsole: {output or '(no output)'}"
    level = wait_for_level(client, frame0)
    if level is None:
        return f"error: load {name} did not finish within {LEVEL_WAIT:.0f} s; check game_status and log_tail"
    pos = client.request("eval", ACTOR_POS, timeout=EVAL_TIMEOUT)
    return f"Loaded {name}: level {level}, actor at {pos}.\nconsole: {output or '(no output)'}"


def list_saves(limit: int = 20) -> str:
    folder = saves_dir()
    files = sorted(folder.glob(f"*{SAVE_EXT}"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return f"No saved games in {folder}."
    shown = files[: max(1, int(limit))]
    lines = [f"{len(shown)} of {len(files)} saved games in {folder}, newest first:"]
    for p in shown:
        st = p.stat()
        when = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"{p.stem}  {when}  {st.st_size / 1024:.0f} KiB")
    return "\n".join(lines)


def screenshot_dir() -> Path:
    return config.anomaly_dir() / "appdata" / "screenshots"


def _load(path: Path) -> Image.Image | None:
    """The image once it is fully written and readable, else None."""
    if not path.is_file():
        return None
    try:
        with Image.open(path) as im:
            return im.convert("RGB")  # backbuffer alpha is meaningless; drop it
    except OSError:
        return None


def screenshot(width: int, height: int) -> tuple[str, Path | None]:
    """Take a screenshot; return (report, png path or None on failure)."""
    name = "mcp_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    # take_screenshot resolves paths against $game_textures$ (<ANOMALY_DIR>\gamedata\textures\);
    # climb out so the file lands in <ANOMALY_DIR>\appdata\screenshots\ instead.
    rel = f"..\\..\\appdata\\screenshots\\{name}.dds"
    dds = screenshot_dir() / f"{name}.dds"
    client = _client()
    result = client.request("eval", f"return mcp_helper.screenshot({_lua_str(rel)}, {int(width)}, {int(height)})", timeout=EVAL_TIMEOUT)
    if result != "ok":
        return f"screenshot failed: {result}", None
    image = wait_until(lambda: _load(dds), SHOT_WAIT, 0.25)
    note = ""
    if image is None:
        root = config.anomaly_dir()
        found = [p for d in ("appdata", "gamedata") for p in (root / d).rglob(f"{name}*")]
        if not found:
            return (
                f"No screenshot file appeared at {dds} within {SHOT_WAIT:.0f} s (screenshots are "
                "disabled when HDR10 is on; check log_tail).",
                None,
            )
        dds = found[0]
        image = _load(dds)
        if image is None:
            return f"Screenshot file {dds} is not readable as DDS.", None
        note = f" (DDS was written to {dds}, not {screenshot_dir()})"
    png = screenshot_dir() / f"{name}.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    image.save(png)
    dds.unlink()
    return f"{png} ({image.width}x{image.height}){note}", png
