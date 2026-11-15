#include "pch_script.h"
#include "mcp_events.h"
#include "../xrEngine/mcp_server.h"
#include "../xrEngine/x_ray.h"
#include "ai_space.h"
#include "script_engine.h"
#include "Level.h"
#include "Actor.h"
#include "MainMenu.h"
#include "GameTask.h"
#include "string_table.h"
#include <mutex>
#include <chrono>
#include <atomic>

// Event "game": {"seq","type","time" (unix ms),"frame","data":{...}}; the last ring_size are kept for events.tail.
// Script errors are coalesced per kind+message: the first is sent at once (with traceback), repeats within
// window_ms are only counted and then sent as one event per window (count = total, since_last = folded in).
namespace
{
	bool enabled = false;

	std::mutex lock; // guards everything below; taken on any thread, held only for bookkeeping and send_event

	struct stored
	{
		u64 seq;
		xr_string json;
	};
	const u32 ring_size = 1000;
	xr_deque<stored> ring;
	u64 last_seq = 0;

	const u64 window_ms = 5000;
	const u32 max_new_per_window = 20; // more new messages per window are only counted ("overflow" event)
	const u32 max_keys = 256;
	struct error_state
	{
		xr_string kind, message, file, script;
		int line = 0;
		u64 count = 0, pending = 0, last_emit = 0;
	};
	xr_map<xr_string, error_state> errors; // key: kind + '\n' + message
	u64 window_start = 0, overflow = 0, overflow_emit = 0;
	u32 window_new = 0;

	// Main thread only (frame poll)
	IGame_Level* level_ptr = nullptr;
	xr_string level_name, prev_level;
	CActor* actor_ptr = nullptr;
	bool menu_active = false;
	std::atomic<bool> level_reload{ false }; // a save was loaded: loading the same level may reuse CLevel without a loading frame
	u64 last_flush = 0;

	u64 now_ms()
	{
		using namespace std::chrono;
		return (u64)duration_cast<milliseconds>(steady_clock::now().time_since_epoch()).count();
	}

	xr_string js(LPCSTR s) { return s ? mcp::to_json_string(s) : xr_string("null"); }
	xr_string js(const xr_string& s) { return mcp::to_json_string(s.c_str()); }
	xr_string num(u64 v) { return std::to_string(v).c_str(); }

	// Caller holds lock
	void emit_locked(LPCSTR type, const xr_string& data)
	{
		using namespace std::chrono;
		s64 t = duration_cast<milliseconds>(system_clock::now().time_since_epoch()).count();
		u64 seq = ++last_seq;
		xr_string ev = "{\"seq\":" + num(seq) + ",\"type\":\"" + type + "\",\"time\":" + num((u64)t) +
			",\"frame\":" + num(Device.dwFrame) + ",\"data\":" + data + "}";
		mcp::send_event("game", ev.c_str());
		ring.push_back({ seq, std::move(ev) });
		if (ring.size() > ring_size)
			ring.pop_front();
	}

	void emit(LPCSTR type, const xr_string& data)
	{
		std::lock_guard<std::mutex> g(lock);
		emit_locked(type, data);
	}

	xr_string error_json(const error_state& e, bool fatal, u64 since_last, const xr_string* traceback)
	{
		xr_string d = "{\"kind\":" + js(e.kind) + ",\"fatal\":" + (fatal ? "true" : "false") + ",\"message\":" + js(e.message);
		if (!e.file.empty())
			d += ",\"file\":" + js(e.file) + ",\"line\":" + num(e.line);
		if (!e.script.empty())
			d += ",\"script\":" + js(e.script);
		d += ",\"count\":" + num(e.count) + ",\"since_last\":" + num(since_last);
		if (traceback && !traceback->empty())
			d += ",\"traceback\":" + js(*traceback);
		return d + "}";
	}

	// "path:123: text" -> path, 123
	bool parse_position(const xr_string& m, xr_string& file, int& line)
	{
		for (size_t i = m.find(':'); i != xr_string::npos; i = m.find(':', i + 1))
		{
			size_t j = i + 1;
			while (j < m.size() && isdigit((unsigned char)m[j]))
				j++;
			if (j > i + 1 && j < m.size() && m[j] == ':')
			{
				file = m.substr(0, i);
				line = atoi(m.c_str() + i + 1);
				return true;
			}
		}
		return false;
	}

	// Errors raised where the failing Lua frames are still on L's stack (error handler, dead coroutine, luabind callbacks)
	bool has_stack(LPCSTR kind) { return xr_strcmp(kind, "load") && xr_strcmp(kind, "console") && xr_strcmp(kind, "thread_start"); }
	bool is_fatal(LPCSTR kind)
	{
		return !xr_strcmp(kind, "callback") || !xr_strcmp(kind, "luabind") || !xr_strcmp(kind, "cast") || !xr_strcmp(kind, "panic");
	}

	void flush_errors(u64 now)
	{
		std::lock_guard<std::mutex> g(lock);
		for (auto& it : errors)
		{
			error_state& e = it.second;
			if (e.pending && now - e.last_emit >= window_ms)
			{
				emit_locked("script_error", error_json(e, false, e.pending, nullptr));
				e.pending = 0;
				e.last_emit = now;
			}
		}
		if (overflow && now - overflow_emit >= window_ms)
		{
			error_state e;
			e.kind = "overflow";
			e.message = make_string("%llu script errors with new messages were not reported (more than %u new messages per %llu ms)",
				overflow, max_new_per_window, window_ms).c_str();
			e.count = overflow;
			emit_locked("script_error", error_json(e, false, overflow, nullptr));
			overflow = 0;
			overflow_emit = now;
		}
	}

	void poll()
	{
		// A level change can reuse the CLevel address, so a load in progress or a new name also ends the level
		bool loading = pApp && pApp->IsLoading();
		IGame_Level* lvl = g_pGameLevel;
		xr_string name = lvl && lvl->name().size() ? lvl->name().c_str() : "";
		bool reload = level_reload.exchange(false);
		if (!level_name.empty() && (lvl != level_ptr || loading || name != level_name || reload))
		{
			emit("level_unloaded", "{\"level\":" + js(level_name) + "}");
			prev_level = level_name;
			level_name.clear();
		}
		level_ptr = lvl;
		if (lvl && level_name.empty() && !loading && !name.empty())
		{
			level_name = name;
			emit("level_loaded", "{\"level\":" + js(level_name) + ",\"previous\":" + (prev_level.empty() ? xr_string("null") : js(prev_level)) + "}");
		}

		CActor* a = level_name.empty() ? nullptr : g_actor;
		if (a != actor_ptr)
		{
			actor_ptr = a;
			if (a)
				emit("actor_spawned", "{\"level\":" + js(level_name) + ",\"id\":" + num(a->ID()) + ",\"name\":" + js(a->cName().c_str()) +
					",\"health\":" + std::to_string(a->GetfHealth()).c_str() + ",\"alive\":" + (a->g_Alive() ? "true" : "false") + "}");
		}

		bool menu = MainMenu() && MainMenu()->IsActive();
		if (menu != menu_active)
		{
			menu_active = menu;
			emit(menu ? "menu_opened" : "menu_closed", "{\"level\":" + (level_name.empty() ? xr_string("null") : js(level_name)) + "}");
		}

		u64 now = now_ms();
		if (now - last_flush >= 250)
		{
			last_flush = now;
			flush_errors(now);
		}
	}

	struct frame_hook : public pureFrame
	{
		virtual void _BCL OnFrame() { poll(); }
	} frame;

	// {"since":S} events with seq > S still held, or {"n":K} the last K -> {"events":[...],"first_seq","last_seq"}
	bool events_tail(LPCSTR params, xr_string& out)
	{
		s64 since = -1, n = -1;
		if (!mcp::get_int_param(params, "since", since) && !mcp::get_int_param(params, "n", n))
		{
			out = "expected params {\"since\":S} or {\"n\":K}";
			return false;
		}
		std::lock_guard<std::mutex> g(lock);
		size_t first = 0;
		if (since >= 0)
			while (first < ring.size() && ring[first].seq <= (u64)since)
				first++;
		else if ((u64)n < ring.size())
			first = ring.size() - (size_t)n;
		out = "{\"events\":[";
		for (size_t i = first; i < ring.size(); i++)
		{
			if (i > first)
				out += ",";
			out += ring[i].json;
		}
		u64 first_seq = ring.empty() ? last_seq + 1 : ring.front().seq;
		out += "],\"first_seq\":" + num(first_seq) + ",\"last_seq\":" + num(last_seq) + "}";
		return true;
	}
}

void mcp_register_events()
{
	enabled = !!Core.ParamsData.test(ECoreParams::mcp_port);
	if (!enabled)
		return;
	mcp::register_method("events.tail", events_tail);
	Device.seqFrame.Add(&frame, REG_PRIORITY_LOW);
}

void mcp_script_error(lua_State* L, LPCSTR kind, LPCSTR script, LPCSTR msg)
{
	if (!enabled)
		return;

	xr_string message = msg ? msg : (L && lua_isstring(L, -1) ? lua_tostring(L, -1) : "(error object is not a string)");
	if (message.size() > 4096)
		message.resize(4096);
	bool fatal = is_fatal(kind);
	xr_string key = xr_string(kind) + "\n" + message;
	u64 now = now_ms();

	// Bookkeeping first, so a suppressed repeat costs no traceback
	u64 count = 1, since_last = 1;
	{
		std::lock_guard<std::mutex> g(lock);
		auto it = errors.find(key);
		if (it != errors.end())
		{
			error_state& e = it->second;
			e.count++;
			if (!fatal && now - e.last_emit < window_ms)
			{
				e.pending++;
				return;
			}
			since_last = e.pending + 1;
			e.pending = 0;
			e.last_emit = now;
			count = e.count;
		}
		else if (!fatal)
		{
			if (now - window_start >= window_ms)
			{
				window_start = now;
				window_new = 0;
			}
			if (errors.size() >= max_keys)
			{
				for (auto i = errors.begin(); i != errors.end();)
					i = (!i->second.pending && now - i->second.last_emit >= window_ms) ? errors.erase(i) : std::next(i);
			}
			if (window_new >= max_new_per_window || errors.size() >= max_keys)
			{
				overflow++;
				return;
			}
			window_new++;
			error_state& e = errors[key];
			e.kind = kind;
			e.message = message;
			e.count = 1;
			e.last_emit = now;
		}
	}

	error_state e;
	e.kind = kind;
	e.message = message;
	e.count = count;
	if (script && *script)
		e.script = script;
	bool has_pos = parse_position(message, e.file, e.line);

	xr_string traceback;
	if (L && has_stack(kind))
	{
		// A dead coroutine's frames are read from the main state; otherwise L itself (level 1 skips the error handler)
		bool thread = !xr_strcmp(kind, "thread");
		lua_State* host = thread && g_ai_space && ai().script_engine().lua() ? ai().script_engine().lua() : L;
		if (lua_checkstack(host, 4))
		{
			luaL_traceback(host, L, nullptr, !xr_strcmp(kind, "callback") ? 1 : 0);
			if (LPCSTR tb = lua_tostring(host, -1))
				traceback = tb;
			lua_pop(host, 1);
			if (traceback.size() > 8192)
				traceback.resize(8192);
		}
		lua_Debug ar;
		for (int i = 0; !has_pos && lua_getstack(L, i, &ar); i++)
		{
			if (lua_getinfo(L, "Sl", &ar) && ar.currentline > 0)
			{
				e.file = ar.short_src;
				e.line = ar.currentline;
				has_pos = true;
			}
		}
	}

	std::lock_guard<std::mutex> g(lock);
	auto it = errors.find(key);
	if (it != errors.end() && it->second.file.empty())
	{
		it->second.file = e.file;
		it->second.line = e.line;
		it->second.script = e.script;
	}
	emit_locked("script_error", error_json(e, fatal, since_last, &traceback));
}

void mcp_event_actor_died(CObject* killer)
{
	if (!enabled)
		return;
	xr_string d = "{\"level\":" + (g_pGameLevel ? js(g_pGameLevel->name().c_str()) : xr_string("null"));
	if (killer)
		d += ",\"killer_id\":" + num(killer->ID()) + ",\"killer_name\":" + js(killer->cName().c_str()) +
			",\"killer_section\":" + js(killer->cNameSect().c_str());
	else
		d += ",\"killer_id\":null";
	emit("actor_died", d + "}");
}

void mcp_event_game_saved(LPCSTR name, LPCSTR file)
{
	if (enabled)
		emit("game_saved", "{\"name\":" + js(name) + ",\"file\":" + js(file) + "}");
}

void mcp_event_game_loaded(LPCSTR name, LPCSTR file)
{
	if (!enabled)
		return;
	emit("game_loaded", "{\"name\":" + js(name) + ",\"file\":" + js(file) + "}");
	level_reload = true;
}

void mcp_event_task(CGameTask* t, LPCSTR change)
{
	if (!enabled || !t)
		return;
	emit(make_string("task_%s", change).c_str(), "{\"id\":" + js(t->m_ID.c_str()) + ",\"title\":" +
		js(CStringTable().translate(t->m_Title).c_str()) + ",\"title_id\":" + js(t->m_Title.c_str()) + "}");
}
