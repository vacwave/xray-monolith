import re

from anomaly_mcp import server

STATE = "level fake_start  pos 1.00 2.00 3.00  yaw 90.0\ninventory: 1 sections\n  bandage x2"


def actor_handler(op, arg):
    if op != "eval":
        return b"+"
    if arg == "return mcp_helper.actor_state()":
        return ("+" + STATE).encode()
    m = re.fullmatch(r'return mcp_helper\.give_item\("(.*)", (\d+)\)', arg)
    if m:
        if m.group(1) == "nope":
            return b"+error: unknown section nope"
        n = int(m.group(2))
        return f"+created {n}x {m.group(1)} (ids {', '.join(str(1000 + i) for i in range(n))})".encode()
    return b"-unexpected eval"


def test_actor_state(engine):
    engine.handler = actor_handler
    assert server.actor_state() == STATE
    assert engine.requests == [("eval", "return mcp_helper.actor_state()")]


def test_actor_state_lua_error(engine):
    engine.handler = lambda op, arg: b"-[string \"mcp\"]:1: boom"
    assert server.actor_state() == 'Lua error: [string "mcp"]:1: boom'


def test_actor_state_not_running(dead_port):
    assert server.actor_state().startswith("Game is not running")


def test_give_item(engine):
    engine.handler = actor_handler
    assert server.give_item("bandage", 2) == "created 2x bandage (ids 1000, 1001)"
    assert server.give_item("medkit") == "created 1x medkit (ids 1000)"
    assert engine.requests[0] == ("eval", 'return mcp_helper.give_item("bandage", 2)')


def test_give_item_quotes_section(engine):
    engine.handler = actor_handler
    server.give_item(r'a"b\c')
    assert engine.requests == [("eval", r'return mcp_helper.give_item("a\"b\\c", 1)')]


def test_give_item_error_passthrough(engine):
    engine.handler = actor_handler
    assert server.give_item("nope") == "error: unknown section nope"


def test_give_item_bad_count(engine):
    engine.handler = actor_handler
    assert server.give_item("bandage", 0) == "error: count must be 1..50"
    assert server.give_item("bandage", 51) == "error: count must be 1..50"
    assert engine.requests == []


def test_give_item_not_running(dead_port):
    assert server.give_item("bandage").startswith("Game is not running")
