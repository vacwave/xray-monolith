# STATE
Scope: MT only (`dev-mt-mcp`). Plan: agent ⇄ Python MCP supervisor (`mcp/`) ⇄ TCP JSON-lines ⇄ engine (`-mcp_port`).

| # | Goal | Status |
|---|------|--------|
| 0 | Project notes (MAP + area files) | done |
| 1 | Engine IPC server: flag, listener, main-thread queue, `ping`, `engine.info` | done (757d973c) |
| 2 | Multi-instance: `-appdata <dir>`, appdata in presence-mutex key, `-mcp_port 0` → port file, no pause on focus loss | done (5ac6faea) |
| 3 | Log streaming: callback → queue → `log` events (seq), `log.tail` | done (6045689a) |
| 4 | Silent runs with `-mcp_port` (minimized, no focus steal, renders); `-showwindow` = normal | done (8795fb14) |
| 5 | `console.exec`, `lua.exec` (sync, results/errors) | in progress |
| 6 | Python supervisor MCP server (launch/attach/status/exec/log tools, instance caps 3 vanilla / 2 GAMMA) | queued |
| 7 | Crash capture (`crash` event, minidump + log collection, no crash MessageBoxes in MCP mode, `game_last_crash`, `game_restart`) | queued |

Later: state queries, input, screenshots, PyInstaller exe.
Vanilla instance ≈4.1 GB RAM / 1.6 GB VRAM. Baseline user.ltx has r__framelimit 30.
Supervisor gotchas: blocked 2nd instance exits 0 silently (detect quick exit); stale `mcp_port` file after crash (delete before launch, wait for fresh).
Testing: new game via `-start server(all/single/alife/new) client(localhost)` loads fine (~11 s). Loading saves `1`/`bench_g1` crashes in `CALifeSimulatorBase::register_object` even with -showwindow — save/mod mismatch, unresolved.
Next step: dev on goal 5.
