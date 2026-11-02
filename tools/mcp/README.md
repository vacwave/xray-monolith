# anomaly-mcp

MCP server (`anomaly`) that lets Claude talk to a running S.T.A.L.K.E.R. Anomaly
built with `MCP_SERVER`. It is a thin client of the engine's loopback TCP
endpoint; the wire format is in [PROTOCOL.md](PROTOCOL.md).

Tools: `game_status`, `lua_eval`, `console`, `log_tail`, `launch_game`, `kill_game`.

Environment (all optional): `ANOMALY_DIR` (default `C:\Anomaly`),
`ANOMALY_MCP_PORT` (default `28015`), `ANOMALY_EXE` (default `AnomalyDX11AVX.exe`).

```
uv sync
uv run pytest                 # fake engine, no game needed
uv run python smoke.py        # against the live game (start it first)
```

Register with Claude Code:

```
claude mcp add anomaly -- uv run --directory D:\projects\xray-monolith-wpo-mt-mcp\tools\mcp anomaly-mcp
```
