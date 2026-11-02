"""Smoke test against the live game: uv run python smoke.py (game must be running)."""

import sys

from anomaly_mcp import server


def check(name, value, ok):
    print(f"--- {name}: {'OK' if ok else 'FAIL'}\n{value}")
    return ok


def main() -> int:
    status = server.game_status()
    results = [
        check("game_status()", status, status.get("endpoint") == "ok"),
    ]
    value = server.lua_eval("return 1+1")
    results.append(check('lua_eval("return 1+1")', value, value == "2"))
    value = server.console("cfg_save")
    results.append(check('console("cfg_save")', value, value == "ok"))
    value = server.log_tail(20)
    results.append(check("log_tail(20)", value, bool(value) and not value.startswith("[")))
    failed = results.count(False)
    print(f"=== {len(results) - failed}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
