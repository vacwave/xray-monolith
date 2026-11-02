"""Smoke test against the live game: uv run python smoke.py [--test-map] [--saves] (game must be running).

--test-map also loads the test map, teleports and takes a screenshot.
--saves saves as mcp_smoke, lists the saves, loads it back and waits 30 frames.
"""

import re
import sys

from anomaly_mcp import server


def check(name, value, ok):
    print(f"--- {name}: {'OK' if ok else 'FAIL'}\n{value}")
    return ok


def test_map_checks() -> list[bool]:
    value = server.start_test_map()
    results = [check("start_test_map()", value, value.startswith("Test map loaded"))]
    m = re.search(r"x = ([-\d.e]+), y = ([-\d.e]+), z = ([-\d.e]+)", value)
    if m:
        x, y, z = map(float, m.groups())
        name = f"teleport({x + 5:g}, {y:g}, {z + 5:g})"
        value = server.teleport(x + 5, y, z + 5)
        if value.startswith("error: no AI node"):
            # fake_start is tiny: +5 can be off the AI map; snap in place from 2 m up instead
            print(f"--- {name}: {value}")
            name = f"teleport({x:g}, {y + 2:g}, {z:g})"
            value = server.teleport(x, y + 2, z)
        results.append(check(name, value, not value.startswith(("error", "Lua error", "Timed out", "Game is not"))))
    value = server.screenshot()
    results.append(check("screenshot()", value, len(value) == 2 and str(value[0]).endswith(")")))
    before = server.lua_eval("return db.actor:position()")
    value = server.game_input("kFWD", "hold", 60)
    after = server.lua_eval("return db.actor:position()")
    results.append(check('game_input("kFWD", "hold", 60)', f"{value}\n{before} -> {after}", value.startswith("hold") and before != after))
    value = server.game_input("kINVENTORY")
    shown = server.lua_eval("return ui_inventory.GUI ~= nil and ui_inventory.GUI:IsShown()")
    server.game_input("kQUIT")
    results.append(check('game_input("kINVENTORY")', f"{value}; inventory shown: {shown}", value.startswith("tap") and shown == "true"))
    return results


def save_checks() -> list[bool]:
    value = server.save_game("mcp_smoke")
    results = [check('save_game("mcp_smoke")', value, value.startswith("Saved"))]
    value = server.list_saves(5)
    results.append(check("list_saves(5)", value, "\nmcp_smoke  " in value))
    value = server.load_game("mcp_smoke")
    results.append(check('load_game("mcp_smoke")', value, value.startswith("Loaded mcp_smoke")))
    value = server.wait_frames(30)
    results.append(check("wait_frames(30)", value, value.startswith("waited 30 frames")))
    return results


def main() -> int:
    status = server.game_status()
    results = [
        check("game_status()", status, status.get("endpoint") == "ok"),
    ]
    value = server.lua_eval("return 1+1")
    results.append(check('lua_eval("return 1+1")', value, value == "2"))
    value = server.console("cfg_save")
    results.append(check('console("cfg_save")', value, "saved" in value))
    value = server.console("no_such_command_xyz")
    results.append(check('console("no_such_command_xyz")', value, value.startswith("! Unknown command")))
    value = server.log_tail(20)
    results.append(check("log_tail(20)", value, bool(value) and not value.startswith("[")))
    if "--test-map" in sys.argv[1:]:
        results += test_map_checks()
    if "--saves" in sys.argv[1:]:
        results += save_checks()
    failed = results.count(False)
    print(f"=== {len(results) - failed}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
