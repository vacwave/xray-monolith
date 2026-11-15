# MCP layer
- Decided: agent ⇄ MCP stdio ⇄ Python supervisor (`mcp/`, official `mcp` SDK, PyInstaller single exe later) ⇄ TCP newline-delimited JSON-RPC ⇄ engine.
- Decided: engine server is localhost-only, one client, off unless `-mcp_port N` is passed.
- Decided: engine JSON = nlohmann/json single header in `src/3rd party/nlohmann/` (no vcxproj include edit needed).
- Decided: net thread only parses/queues; all game/Lua/console work runs on the main thread in a pureFrame.
- Decided: engine pushes every log line as an event so the supervisor survives crashes with full history.
- Decided: MT branch only (`dev-mt-mcp`); supervisor lives here permanently, no non-MT port.
- Planned methods: `ping`, `engine.info` (incl. protocol_version), `log.tail`, `console.exec`, `lua.exec`; events: `log`, `crash`.
