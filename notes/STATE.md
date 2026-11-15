# STATE
Scope: MT only (`dev-mt-mcp`). Plan: agent ⇄ Python MCP supervisor (`mcp/`) ⇄ TCP JSON-lines ⇄ engine (`-mcp_port`).

| # | Goal | Status |
|---|------|--------|
| 0 | Project notes (MAP + area files) | done |
| 1 | Engine IPC server skeleton: flag, listener thread, main-thread queue, `ping`, `engine.info`, vendor nlohmann/json | next |
| 2 | Log streaming: callback → queue → `log` events, `log.tail` | queued |
| 3 | `console.exec`, `lua.exec` (sync, results/errors) | queued |
| 4 | Python supervisor MCP server (launch/attach/status/exec/log tools) | queued |
| 5 | Crash capture (`crash` event, minidump + log collection, `game_last_crash`, `game_restart`) | queued |

Later: state queries, input, screenshots, PyInstaller exe.
Next step: brief a `dev` for goal 1.
