#include "pch_script.h"
#include "../xrEngine/mcp_server.h"
#include "ai_space.h"
#include "script_engine.h"

// MCP lua.exec: runs on the main thread from seqFrame. Worker-thread Lua (GameThread scheduler, seqFrameMT,
// parallel GC) is submitted after FrameMove and joined at the end of on_idle, so nothing else touches the state then.
namespace
{
	// Message handler fallback when debug.traceback is missing or replaced by a script
	int traceback(lua_State* L)
	{
		const char* msg = lua_tostring(L, 1);
		luaL_traceback(L, L, msg ? msg : "(error object is not a string)", 1);
		return 1;
	}

	// print() replacement during lua.exec: tostring'd args, tab-separated, to the engine log
	int log_print(lua_State* L)
	{
		int n = lua_gettop(L);
		xr_string s;
		for (int i = 1; i <= n; i++)
		{
			lua_getglobal(L, "tostring");
			lua_pushvalue(L, i);
			lua_call(L, 1, 1);
			const char* v = lua_tostring(L, -1);
			if (!v)
				return luaL_error(L, "'tostring' must return a string to 'print'");
			if (i > 1)
				s += "	";
			s += v;
			lua_pop(L, 1);
		}
		Log(s.c_str());
		return 0;
	}

	// tostring() on the value at idx; never raises
	xr_string to_string(lua_State* L, int idx)
	{
		if (idx < 0)
			idx = lua_gettop(L) + idx + 1;
		if (!lua_checkstack(L, 2))
			return "<stack overflow>";
		lua_getglobal(L, "tostring");
		lua_pushvalue(L, idx);
		xr_string s;
		if (lua_pcall(L, 1, 1, 0) == 0 && lua_isstring(L, -1))
			s = lua_tostring(L, -1);
		else
			s = make_string("<%s>", luaL_typename(L, idx)).c_str();
		lua_pop(L, 1);
		return s;
	}

	// REPL: try <code> as an expression list first, else <code> as is. On failure error is the message (+ traceback)
	bool run(lua_State* L, const xr_string& code, xr_vector<xr_string>& results, xr_string& error)
	{
		int top = lua_gettop(L);
		lua_getglobal(L, "debug");
		if (lua_istable(L, -1))
			lua_getfield(L, -1, "traceback");
		else
			lua_pushnil(L);
		lua_remove(L, -2);
		if (!lua_isfunction(L, -1))
		{
			lua_pop(L, 1);
			lua_pushcfunction(L, traceback);
		}
		int handler = top + 1;

		// Swap print for the log version; the old one stays on the stack and is restored after the pcall
		lua_getglobal(L, "print");
		lua_pushcfunction(L, log_print);
		lua_setglobal(L, "print");
		int old_print = handler + 1;

		// Passing the values through a function keeps the chunk frame (a plain "return f()" is a tail call, which drops it from error positions and tracebacks)
		xr_string ret = "return (function(...) return ... end)(" + code + "\n)";
		int err = luaL_loadbuffer(L, ret.c_str(), ret.size(), "=lua.exec");
		if (err)
		{
			lua_pop(L, 1);
			err = luaL_loadbuffer(L, code.c_str(), code.size(), "=lua.exec");
		}
		if (!err)
			err = lua_pcall(L, 0, LUA_MULTRET, handler);

		bool ok = !err;
		if (ok)
		{
			for (int i = old_print + 1; i <= lua_gettop(L); i++)
				results.push_back(to_string(L, i));
		}
		else
			error = to_string(L, -1);
		lua_pushvalue(L, old_print);
		lua_setglobal(L, "print");
		lua_settop(L, top);
		return ok;
	}

	// {"code":"..."} -> {"results":[...],"lines":[...]}
	bool lua_exec(LPCSTR params, xr_string& out)
	{
		xr_string code;
		if (!mcp::get_string_param(params, "code", code))
		{
			out = "expected params {\"code\":\"...\"}";
			return false;
		}
		if (!g_ai_space || !ai().script_engine().lua())
		{
			out = "script engine is not ready";
			return false;
		}

		lua_State* L = ai().script_engine().lua();
		xr_vector<xr_string> results;
		xr_string error;
		bool ok = false;
		xr_string lines = mcp::run_logged([&]() { ok = run(L, code, results, error); });
		if (!ok)
		{
			out = error;
			return false;
		}

		out = "{\"results\":[";
		for (u32 i = 0; i < results.size(); i++)
		{
			if (i)
				out += ",";
			out += mcp::to_json_string(results[i].c_str());
		}
		out += "]," + lines + "}";
		return true;
	}
}

void mcp_register_lua()
{
	mcp::register_method("lua.exec", lua_exec);
}
