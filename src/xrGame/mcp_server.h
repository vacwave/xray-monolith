#pragma once

#ifdef MCP_SERVER
// MCP engine endpoint, loopback TCP 127.0.0.1:28015. Wire contract: tools/mcp/PROTOCOL.md
// Both run on the main thread (CGamePersistent::OnAppStart / OnAppEnd).
void mcp_server_start();
void mcp_server_stop();
#endif
