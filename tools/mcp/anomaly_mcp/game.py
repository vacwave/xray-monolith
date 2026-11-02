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

    return wait_until(ready, LEVEL_WAIT)


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
