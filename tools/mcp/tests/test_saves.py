import os
import re

import pytest

from anomaly_mcp import game, process, server


@pytest.fixture(autouse=True)
def fast(monkeypatch, tmp_path):
    monkeypatch.setenv("ANOMALY_DIR", str(tmp_path))
    monkeypatch.setattr(process, "find_game", lambda: None)
    monkeypatch.setattr(game, "POLL", 0.01)
    monkeypatch.setattr(game, "HOLD_POLL", 0.001)
    monkeypatch.setattr(game, "LEVEL_WAIT", 2.0)
    monkeypatch.setattr(game, "SAVE_WAIT", 1.0)


class World:
    """Fake engine with saves: `save` writes the file, `load` swaps the actor and reloads the level."""

    def __init__(self, saves, level=1, name="l01_escape"):
        self.saves = saves
        self.frame = 100
        self.level = level
        self.name = name
        self.actor = 1  # bumped by a load; the pre-load marker remembers the old one
        self.marker = None
        self.loading = 0  # status polls left until a requested load is done
        self.load_to = None
        self.frame_step = 1
        self.requests = []

    def _load(self, save):
        self.loading, self.load_to = 3, save

    def handler(self, op, arg):
        self.requests.append((op, arg))
        if op == "status":
            self.frame += self.frame_step
            if self.loading:
                self.loading -= 1
                self.level = 0
                if not self.loading:
                    self.level, self.name, self.actor = 1, f"level_of_{self.load_to}", self.actor + 1
            return f"+frame={self.frame}\npaused=0\nlevel={self.level}\npending=0".encode()
        if op == "console":
            if arg.startswith("save "):
                (self.saves / f"{arg[5:]}.scop").write_bytes(b"x" * 2048)
                return b"+"
            if arg.startswith("load "):
                self._load(arg[5:])
                return b"+"
            if arg == "disconnect":
                return b"+"
            m = re.fullmatch(r"start server\((.*)/single/alife/load\) client\(localhost\)", arg)
            if m:
                self._load(m.group(1))
                return b"+"
            return b"-unexpected console"
        if arg == game.LEVEL_NAME:
            return f"+{self.name if self.level and not self.loading else ''}".encode()
        if arg.startswith("return valid_saved_game("):
            return b"+false" if "corrupt" in arg else b"+true"
        if arg == game.PRE_LOAD:
            self.marker = self.actor
            return b"+"
        if arg == game.LOAD_STARTED:
            return b"+true" if self.loading or self.actor != self.marker else b"+false"
        if arg in (game.POST_LOAD, game.UNPAUSE):
            return b"+"
        if arg == game.ACTOR_POS:
            return b"+vector{x = 1, y = 2, z = 3}"
        return b"-unexpected eval"


@pytest.fixture
def saves(tmp_path):
    d = tmp_path / "appdata" / "savedgames"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def world(engine, saves):
    w = World(saves)
    engine.handler = w.handler
    return w


def consoles(world):
    return [arg for op, arg in world.requests if op == "console"]


def test_save_game(world, saves):
    report = server.save_game("my save 1")
    assert report == f"Saved {saves / 'my save 1.scop'} (2048 bytes).\nconsole: (no output)"
    assert consoles(world) == ["save my save 1"]


def test_save_game_overwrite_waits_for_new_file(world, saves):
    path = saves / "old.scop"
    path.write_bytes(b"x")
    os.utime(path, (1, 1))
    assert server.save_game("old").startswith(f"Saved {path} (2048 bytes)")


def test_save_game_file_never_written(world, engine):
    engine.handler = lambda op, arg: b"+" if op == "console" else world.handler(op, arg)
    report = server.save_game("x")
    assert report.startswith("error: no new ") and "after 1 s" in report


@pytest.mark.parametrize("name", ["", "  ", "a/b", "..\\x", "c:x", "a(1)", "50%", " lead", "tab\tname"])
def test_invalid_names(world, name):
    assert server.save_game(name).startswith("error:")
    assert server.load_game(name).startswith("error:")
    assert world.requests == []


def test_save_game_main_menu(world):
    world.level = 0
    assert server.save_game("x") == "error: no level loaded (main menu); nothing to save"
    assert consoles(world) == []


def test_save_game_dead_actor(world, engine):
    def handler(op, arg):
        if op == "console":
            return b"+cannot make saved game because actor is dead :("
        return world.handler(op, arg)

    engine.handler = handler
    assert server.save_game("x") == "error: save x failed:\ncannot make saved game because actor is dead :("


def test_save_game_not_running(dead_port):
    assert server.save_game("x").startswith("Game is not running")


def test_load_game_in_level(world, saves):
    (saves / "a b.scop").write_bytes(b"x")
    report = server.load_game("a b")
    assert report == "Loaded a b: level level_of_a b, actor at vector{x = 1, y = 2, z = 3}.\nconsole: (no output)"
    assert consoles(world) == ["load a b"]
    evals = [arg for op, arg in world.requests if op == "eval"]
    assert evals.index(game.PRE_LOAD) < evals.index(game.LOAD_STARTED) < evals.index(game.POST_LOAD)
    assert evals[-2:] == [game.UNPAUSE, game.ACTOR_POS]


def test_load_game_from_main_menu(world, saves):
    (saves / "s1.scop").write_bytes(b"x")
    world.level, world.name = 0, ""
    assert server.load_game("s1").startswith("Loaded s1: level level_of_s1,")
    assert consoles(world) == ["disconnect", "start server(s1/single/alife/load) client(localhost)"]
    assert (("eval", game.PRE_LOAD)) not in world.requests


def test_load_game_missing(world, saves):
    assert server.load_game("nope") == f"error: no saved game {saves / 'nope.scop'}"
    assert world.requests == []


def test_load_game_corrupt(world, saves):
    (saves / "corrupt.scop").write_bytes(b"x")
    report = server.load_game("corrupt")
    assert report.startswith("error: ") and "not a loadable saved game" in report
    assert consoles(world) == []


def test_load_game_console_error(world, saves, engine):
    (saves / "s.scop").write_bytes(b"x")

    def handler(op, arg):
        if op == "console":
            return b"+! ALife simulator has not been started yet"
        return world.handler(op, arg)

    engine.handler = handler
    assert server.load_game("s") == "error: load s failed:\n! ALife simulator has not been started yet"


def test_load_game_never_starts(world, saves, engine, monkeypatch):
    monkeypatch.setattr(game, "LEVEL_WAIT", 0.2)
    (saves / "s.scop").write_bytes(b"x")
    engine.handler = lambda op, arg: b"+" if op == "console" else world.handler(op, arg)  # load ignored
    assert server.load_game("s").startswith("error: load s did not start")


def test_list_saves(saves):
    for i, name in enumerate(["old", "mid", "new"]):
        p = saves / f"{name}.scop"
        p.write_bytes(b"x" * 2048 * (i + 1))
        os.utime(p, (1_700_000_000 + i * 60,) * 2)
    (saves / "new.dds").write_bytes(b"")
    lines = server.list_saves().split("\n")
    assert lines[0] == f"3 of 3 saved games in {saves}, newest first:"
    assert [line.split("  ")[0] for line in lines[1:]] == ["new", "mid", "old"]
    assert lines[1].endswith("  6 KiB") and re.search(r"  \d{4}-\d\d-\d\d \d\d:\d\d:\d\d  ", lines[1])
    assert server.list_saves(2).split("\n")[0] == f"2 of 3 saved games in {saves}, newest first:"


def test_list_saves_empty(tmp_path):
    assert server.list_saves() == f"No saved games in {tmp_path / 'appdata' / 'savedgames'}."


def test_wait_frames(world):
    frame0 = world.frame
    assert re.fullmatch(r"waited 5 frames in \d+\.\d\d s", server.wait_frames(5))
    assert world.frame >= frame0 + 5
    assert {op for op, arg in world.requests} == {"status"}


def test_wait_frames_stuck(world):
    world.frame_step = 0
    assert server.wait_frames(5, timeout=0.1) == "the game ran only 0 of 5 frames in 0.1 s (paused or loading?)"


def test_wait_frames_not_running(dead_port):
    assert server.wait_frames().startswith("Game is not running")
