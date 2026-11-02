import re

import pytest
from mcp.server.mcpserver import Image
from PIL import Image as PILImage

from anomaly_mcp import game, process, server


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(process, "find_game", lambda: None)
    monkeypatch.setattr(process, "find_window", lambda pid: None)  # never touch real windows
    monkeypatch.setattr(game, "POLL", 0.01)
    monkeypatch.setattr(game, "MENU_WAIT", 2.0)
    monkeypatch.setattr(game, "LEVEL_WAIT", 2.0)
    monkeypatch.setattr(game, "SHOT_WAIT", 1.0)


class World:
    """Fake engine state: a level that switches after a few status polls once a change is requested."""

    def __init__(self, level=1, name="l01_escape"):
        self.frame = 100
        self.level = level
        self.name = name
        self.pending_change = None  # (polls left, new name)
        self.evals = []

    def handler(self, op, arg):
        if op == "status":
            self.frame += 1
            if self.pending_change:
                left, new = self.pending_change
                if left:
                    self.pending_change = (left - 1, new)
                    self.level = 0
                else:
                    self.pending_change = None
                    self.level, self.name = 1, new
            return f"+frame={self.frame}\npaused=0\nlevel={self.level}\npending=0".encode()
        if op != "eval":
            return b"+"
        self.evals.append(arg)
        if arg == game.LEVEL_NAME:
            return f"+{self.name if self.level else ''}".encode()
        if arg == game.MENU_READY:
            return b"+true"
        if arg == game.ACTOR_POS:
            return b"+vector{x = 1, y = 2, z = 3}"
        if arg == "return mcp_helper.start_test_map()":
            self.pending_change = (2, "fake_start")
            return b"+started"
        m = re.fullmatch(r'return mcp_helper\.jump_to_level\("(.*)"\)', arg)
        if m:
            self.pending_change = (3, m.group(1))
            return b"+changing"
        m = re.fullmatch(r"return mcp_helper\.teleport\((.*), (.*), (.*)\)", arg)
        if m:
            return f"+{float(m.group(1)):.2f} {float(m.group(2)):.2f} {float(m.group(3)):.2f} {self.name}".encode()
        return b"-unexpected eval"


@pytest.fixture
def world(engine):
    w = World()
    engine.handler = w.handler
    return w


def test_teleport_same_level(world):
    assert server.teleport(1.5, 2, -3) == "1.50 2.00 -3.00 l01_escape"
    assert world.evals == ["return mcp_helper.teleport(1.5, 2.0, -3.0)"]


def test_teleport_named_current_level(world):
    assert server.teleport(1, 2, 3, "l01_escape") == "1.00 2.00 3.00 l01_escape"
    assert not any("jump_to_level" in e for e in world.evals)


def test_teleport_error_passthrough(world, engine):
    engine.handler = lambda op, arg: b"+error: no AI node near 1,2,3 on level x"
    assert server.teleport(1, 2, 3) == "error: no AI node near 1,2,3 on level x"


def test_teleport_other_level(world):
    report = server.teleport(10, 0, 20, "l02_garbage")
    assert report == "Changed level l01_escape -> l02_garbage.\n10.00 0.00 20.00 l02_garbage"
    assert 'return mcp_helper.jump_to_level("l02_garbage")' in world.evals
    assert world.evals[-1] == "return mcp_helper.teleport(10.0, 0.0, 20.0)"


def test_teleport_unknown_level(world, engine):
    def handler(op, arg):
        if "jump_to_level" in arg:
            return b"+error: unknown level nope; known levels: l01_escape, l02_garbage"
        return world.handler(op, arg)

    engine.handler = handler
    assert server.teleport(0, 0, 0, "nope").startswith("error: unknown level nope")


def test_teleport_not_running(dead_port):
    assert server.teleport(0, 0, 0).startswith("Game is not running")


def test_start_test_map_from_menu(world):
    world.level, world.name = 0, ""
    assert server.start_test_map() == "Test map loaded: level fake_start, actor at vector{x = 1, y = 2, z = 3}."
    assert world.evals.index(game.MENU_READY) < world.evals.index("return mcp_helper.start_test_map()")


def test_start_test_map_level_timeout(world, engine, monkeypatch):
    monkeypatch.setattr(game, "LEVEL_WAIT", 0.2)

    def handler(op, arg):
        if arg == "return mcp_helper.start_test_map()":
            return b"+started"  # never loads
        return world.handler(op, arg)

    world.level = 0
    engine.handler = handler
    assert server.start_test_map().startswith("Test map started but no level")


def test_launch_game_test_map(world, tmp_path, monkeypatch):
    monkeypatch.setenv("ANOMALY_DIR", str(tmp_path))
    monkeypatch.setenv("ANOMALY_EXE", "Fake.exe")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "Fake.exe").write_bytes(b"")
    world.level = 0

    class FakePopen:
        pid = 99

        def __init__(self, *a, **k):
            pass

        def poll(self):
            return None

    monkeypatch.setattr(process.subprocess, "Popen", FakePopen)
    report = server.launch_game(test_map=True, minimize=False)
    assert "Endpoint answered" in report
    assert report.endswith("Test map loaded: level fake_start, actor at vector{x = 1, y = 2, z = 3}.")


def test_screenshot(engine, tmp_path, monkeypatch):
    monkeypatch.setenv("ANOMALY_DIR", str(tmp_path))
    seen = {}

    def handler(op, arg):
        m = re.fullmatch(r'return mcp_helper\.screenshot\("(.*)", (\d+), (\d+)\)', arg)
        assert m, arg
        rel = m.group(1).replace("\\\\", "\\")
        seen.update(rel=rel, w=int(m.group(2)), h=int(m.group(3)))
        # resolve like the engine: relative to $game_textures$
        path = (tmp_path / "gamedata" / "textures" / rel).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        PILImage.new("RGBA", (8, 4), (255, 0, 0, 0)).save(path)
        seen["dds"] = path
        return b"+ok"

    engine.handler = handler
    result = server.screenshot(640, 360)
    assert (seen["w"], seen["h"]) == (640, 360)
    assert seen["rel"].startswith("..\\..\\appdata\\screenshots\\mcp_")
    text, image = result
    png = tmp_path / "appdata" / "screenshots" / (seen["dds"].stem + ".png")
    assert text == f"{png} (8x4)"
    assert isinstance(image, Image) and image.path == png
    assert not seen["dds"].exists()
    with PILImage.open(png) as im:
        assert im.mode == "RGB" and im.size == (8, 4) and im.getpixel((0, 0)) == (255, 0, 0)


def test_screenshot_no_file(engine, tmp_path, monkeypatch):
    monkeypatch.setenv("ANOMALY_DIR", str(tmp_path))
    monkeypatch.setattr(game, "SHOT_WAIT", 0.2)
    engine.handler = lambda op, arg: b"+ok"
    [text] = server.screenshot()
    assert text.startswith("No screenshot file appeared") and "HDR10" in text


def test_screenshot_lua_error(engine):
    engine.handler = lambda op, arg: b"+error: attempt to call nil"
    assert server.screenshot() == ["screenshot failed: error: attempt to call nil"]


class FakeUser32:
    def __init__(self):
        self.calls = []
        self.iconic = False

    def ShowWindow(self, hwnd, cmd):
        self.calls.append(("show", hwnd, cmd))
        self.iconic = cmd == process.SW_MINIMIZE

    def IsIconic(self, hwnd):
        return self.iconic

    def SetForegroundWindow(self, hwnd):
        self.calls.append(("foreground", hwnd))
        return 1


@pytest.fixture
def user32(monkeypatch):
    fake = FakeUser32()
    monkeypatch.setattr(process, "_user32", lambda: fake)
    monkeypatch.setattr(process, "find_window", lambda pid: 777 if pid == 4242 else None)
    return fake


class Proc:
    pid = 4242


def test_game_window(user32, monkeypatch):
    assert server.game_window("minimize") == "AnomalyDX11AVX.exe is not running."
    monkeypatch.setattr(process, "find_game", lambda: Proc())
    assert server.game_window("minimize") == "Window minimized."
    assert server.game_window("restore") == "Window restored."
    assert server.game_window("focus") == "Window restored and focused."
    assert user32.calls == [("show", 777, 6), ("show", 777, 9), ("show", 777, 9), ("foreground", 777)]
    assert server.game_window("close").startswith("Unknown action")
    Proc.pid = 1
    try:
        assert server.game_window("minimize") == "Game window not found (pid 1)."
    finally:
        Proc.pid = 4242


def test_launch_game_test_map_minimize(world, engine, user32, tmp_path, monkeypatch):
    monkeypatch.setenv("ANOMALY_DIR", str(tmp_path))
    monkeypatch.setenv("ANOMALY_EXE", "Fake.exe")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "Fake.exe").write_bytes(b"")
    world.level = 0

    class FakePopen:
        pid = 4242

        def __init__(self, *a, **k):
            pass

        def poll(self):
            return None

    monkeypatch.setattr(process.subprocess, "Popen", FakePopen)
    report = server.launch_game(test_map=True, minimize=True).split("\n")
    assert report[-3] == "Ran g_always_active on, keypress_on_start off."
    assert report[-2] == "Test map loaded: level fake_start, actor at vector{x = 1, y = 2, z = 3}."
    assert report[-1] == "Window minimized."
    consoles = [arg for op, arg in engine.requests if op == "console"]
    assert consoles == ["g_always_active on", "keypress_on_start off"]
