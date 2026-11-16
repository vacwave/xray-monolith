#pragma once

// MCP game events (only active with -mcp_port): structured "game" events + engine ring for events.tail.
// All hooks are thread-safe and return at once when MCP is off.
struct lua_State;
class CObject;
class CGameTask;

void mcp_register_events();
// Error message is the string on top of L unless msg is given; script = chunk/script name if known
void mcp_script_error(lua_State* L, LPCSTR kind, LPCSTR script = nullptr, LPCSTR msg = nullptr);
void mcp_event_actor_died(CObject* killer);
void mcp_event_game_saved(LPCSTR name, LPCSTR file);
void mcp_event_game_loaded(LPCSTR name, LPCSTR file);
void mcp_event_task(CGameTask* t, LPCSTR change);
void mcp_event_info(LPCSTR name, bool given);
// Emit a game event of another MCP file (data = JSON object text)
void mcp_event(LPCSTR type, LPCSTR data);

// Task stage poll and history (xrGame/mcp_tasks.cpp)
void mcp_register_tasks();
void mcp_tasks_poll(bool level_loaded);
void mcp_tasks_reset();
void mcp_tasks_changed(CGameTask* t, LPCSTR change);
