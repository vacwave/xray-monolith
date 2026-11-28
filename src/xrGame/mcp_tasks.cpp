#include "pch_script.h"
#include "mcp_events.h"
#include "../xrEngine/mcp_server.h"
#include "../xrEngine/x_ray.h"
#include "ai_space.h"
#include "script_engine.h"
#include "game_graph.h"
#include "Level.h"
#include "Actor.h"
#include "GametaskManager.h"
#include "GameTask.h"
#include "string_table.h"
#include "date_time.h"
#include "InventoryOwner.h"
#include "entity_alive.h"
#include "alife_registry_container.h"
#include "alife_registry_wrappers.h"
#include "alife_simulator.h"
#include "alife_object_registry.h"
#include "xrServer_Objects_ALife_Monsters.h"
#include <mutex>
#include <chrono>

// MCP quest state: tasks.get (engine task registry + script task_manager facts), infos.get (actor's info portions,
// give/remove) and a 1 Hz poll of the script-side task stage that emits task_stage events.
LPCSTR mcp_world_kind(u16 id); // xrGame/mcp_world.cpp

namespace
{
	xr_string js(LPCSTR s) { return s ? mcp::to_json_string(s) : xr_string("null"); }

	xr_string fnum(double v, int prec = 3)
	{
		if (!_valid((float)v))
			return "null";
		string64 b;
		xr_sprintf(b, "%.*f", prec, v);
		char* e = b + xr_strlen(b) - 1;
		if (strchr(b, '.'))
		{
			while (*e == '0')
				*e-- = 0;
			if (*e == '.')
				*e = 0;
		}
		return !xr_strcmp(b, "-0") ? xr_string("0") : xr_string(b);
	}

	// Flat JSON object builder; values are JSON text
	struct jobj
	{
		xr_string s;
		jobj& raw(LPCSTR k, const xr_string& v)
		{
			s += s.empty() ? "{\"" : ",\"";
			s += k;
			s += "\":";
			s += v;
			return *this;
		}
		jobj& str(LPCSTR k, LPCSTR v) { return raw(k, js(v)); }
		jobj& num(LPCSTR k, double v, int prec = 3) { return raw(k, fnum(v, prec)); }
		jobj& i(LPCSTR k, s64 v) { return raw(k, xr_string(std::to_string(v).c_str())); }
		jobj& b(LPCSTR k, bool v) { return raw(k, v ? "true" : "false"); }
		jobj& vec(LPCSTR k, const Fvector& v) { return raw(k, "[" + fnum(v.x, 2) + "," + fnum(v.y, 2) + "," + fnum(v.z, 2) + "]"); }
		xr_string done() const { return s.empty() ? xr_string("{}") : s + "}"; }
	};

	const u32 max_history = 50;
	const u64 poll_ms = 1000;

	// Script facts per task id. Every access is guarded: no task_manager / task_info entry -> false for that id.
	// Args: full (also the task ltx condition strings), ids...
	const char* task_chunk =
		"local full = ... local ids = { select(2, ...) }\n"
		"local tm = rawget(_G, 'task_manager') if type(tm) ~= 'table' then return nil end\n"
		"local mgr = rawget(tm, 'random_task') if mgr == nil and rawget(tm, 'get_task_manager') then mgr = tm.get_task_manager() end\n"
		"local ti = mgr and mgr.task_info if type(ti) ~= 'table' then return nil end\n"
		"local ini = full and rawget(tm, 'task_ini')\n"
		"local keys = { 'stage_complete', 'target_functor', 'status_functor', 'on_complete', 'on_fail' }\n"
		"local r = {}\n"
		"for i, id in ipairs(ids) do\n"
		"  local ok, o = pcall(function()\n"
		"    local t = ti[id] if not t then return false end\n"
		"    local o = { stage = t.stage, giver = t.task_giver_id, status = t.status, last_check = t.last_check_task, forced = t.forced_status }\n"
		"    if ini and ini:section_exist(id) then\n"
		"      local d = {}\n"
		"      for _, k in ipairs(keys) do if ini:line_exist(id, k) then d[k] = ini:r_string(id, k) end end\n"
		"      local n = 0 while n < 20 and ini:line_exist(id, 'condlist_' .. n) do d['condlist_' .. n] = ini:r_string(id, 'condlist_' .. n) n = n + 1 end\n"
		"      o.def = d\n"
		"    end\n"
		"    return o end)\n"
		"  r[i] = ok and o or false\n"
		"end\n"
		"return r\n";

	struct lua_task
	{
		bool found = false;
		s64 stage = -1, giver = -1;
		xr_string status, last_check, forced, def; // def = JSON object text or empty
	};

	lua_State* lua() { return g_ai_space ? ai().script_engine().lua() : nullptr; }

	xr_string field_str(lua_State* L, int t, LPCSTR k)
	{
		lua_getfield(L, t, k);
		xr_string r = lua_type(L, -1) == LUA_TSTRING ? lua_tostring(L, -1) : "";
		lua_pop(L, 1);
		return r;
	}

	s64 field_int(lua_State* L, int t, LPCSTR k)
	{
		lua_getfield(L, t, k);
		s64 r = lua_type(L, -1) == LUA_TNUMBER ? (s64)lua_tonumber(L, -1) : -1;
		lua_pop(L, 1);
		return r;
	}

	// false -> err (chunk error or no script engine); out[i] stays !found for tasks the script does not know
	bool run_chunk(const xr_vector<shared_str>& ids, bool full, xr_vector<lua_task>& out, xr_string& err)
	{
		out.assign(ids.size(), lua_task());
		lua_State* L = lua();
		if (!L || !lua_checkstack(L, (int)ids.size() + 8))
		{
			err = "no script engine";
			return false;
		}
		int top = lua_gettop(L);
		bool ok = !luaL_loadbuffer(L, task_chunk, xr_strlen(task_chunk), "=tasks.get");
		if (ok)
		{
			lua_pushboolean(L, full);
			for (const shared_str& id : ids)
				lua_pushstring(L, id.c_str());
			ok = !lua_pcall(L, (int)ids.size() + 1, 1, 0);
		}
		if (!ok)
		{
			LPCSTR m = lua_tostring(L, -1);
			err = m ? m : "error";
		}
		else if (lua_istable(L, -1))
		{
			int r = lua_gettop(L);
			for (u32 i = 0; i < ids.size(); i++)
			{
				lua_rawgeti(L, r, i + 1);
				int t = lua_gettop(L);
				if (lua_istable(L, t))
				{
					lua_task& o = out[i];
					o.found = true;
					o.stage = field_int(L, t, "stage");
					o.giver = field_int(L, t, "giver");
					o.status = field_str(L, t, "status");
					o.last_check = field_str(L, t, "last_check");
					o.forced = field_str(L, t, "forced");
					lua_getfield(L, t, "def");
					if (lua_istable(L, -1))
					{
						jobj d;
						int dt = lua_gettop(L);
						lua_pushnil(L);
						while (lua_next(L, dt))
						{
							if (lua_type(L, -2) == LUA_TSTRING && lua_type(L, -1) == LUA_TSTRING)
								d.str(lua_tostring(L, -2), lua_tostring(L, -1));
							lua_pop(L, 1);
						}
						o.def = d.done();
					}
				}
				lua_settop(L, r);
			}
		}
		lua_settop(L, top);
		return ok;
	}

	// Stage poll state (main thread; task hooks may come from other threads, hence the lock)
	std::mutex lock;
	struct known
	{
		s64 stage = -1, giver = -1;
	};
	xr_map<shared_str, known> seen; // in-progress tasks of the loaded game (cleared on level unload / save load)
	xr_map<shared_str, known> last; // last known stage/giver of tasks that ended in this session (history)
	struct stage_change
	{
		s64 old, stage;
		xr_string title;
	};
	xr_map<shared_str, stage_change> pending; // detected by the last poll, reported by the next
	u64 next_poll = 0;
	float poll_us = 0.f, poll_max_us = 0.f;

	u64 now_ms()
	{
		using namespace std::chrono;
		return (u64)duration_cast<milliseconds>(steady_clock::now().time_since_epoch()).count();
	}

	CSE_ALifeDynamicObject* server_object(u16 id)
	{
		return g_ai_space && ai().get_alife() ? ai().alife().objects().object(id, true) : nullptr;
	}

	LPCSTR level_of(CSE_ALifeDynamicObject* se)
	{
		if (!se || !ai().get_game_graph() || !ai().game_graph().valid_vertex_id(se->m_tGraphID))
			return nullptr;
		const auto& levels = ai().game_graph().header().levels();
		auto it = levels.find(ai().game_graph().vertex(se->m_tGraphID)->level_id());
		return it != levels.end() ? it->second.name().c_str() : nullptr;
	}

	// Object brief: id, name, character (NPC display name), section, kind, level, alive, online; with place also
	// position and, only on the actor's level, distance and bearing (deg to turn right) from the camera
	xr_string object_json(u16 id, bool place)
	{
		CSE_ALifeDynamicObject* se = server_object(id);
		CObject* o = g_pGameLevel ? Level().Objects.net_Find(id) : nullptr;
		jobj e;
		e.i("id", id);
		if (!se && !o)
			return e.b("exists", false).done();
		e.str("name", se ? se->name_replace() : o->cName().c_str());
		if (CInventoryOwner* io = smart_cast<CInventoryOwner*>(o))
			e.str("character", io->Name());
		else if (CSE_ALifeTraderAbstract* tr = smart_cast<CSE_ALifeTraderAbstract*>(se))
			e.str("character", tr->m_character_name.c_str());
		e.str("section", se ? se->s_name.c_str() : o->cNameSect().c_str());
		if (LPCSTR k = mcp_world_kind(id))
			e.str("kind", k);
		LPCSTR lvl = o ? Level().name().c_str() : level_of(se);
		e.str("level", lvl);
		if (CEntityAlive* ea = smart_cast<CEntityAlive*>(o))
			e.b("alive", !!ea->g_Alive());
		else if (CSE_ALifeCreatureAbstract* cr = smart_cast<CSE_ALifeCreatureAbstract*>(se))
			e.b("alive", cr->g_Alive());
		e.b("online", !!o);
		if (place)
		{
			Fvector p = o ? o->Position() : se->o_Position;
			e.vec("position", p);
			if (lvl && g_actor && !xr_strcmp(lvl, Level().name().c_str()))
			{
				Fvector d;
				d.sub(p, Device.vCameraPosition);
				float yaw = rad2deg(atan2f(Device.vCameraDirection.x, Device.vCameraDirection.z));
				float b = rad2deg(atan2f(d.x, d.z)) - yaw;
				while (b > 180.f) b -= 360.f;
				while (b <= -180.f) b += 360.f;
				e.num("distance", g_actor->Position().distance_to(p), 1).num("bearing", b, 1);
			}
		}
		return e.done();
	}

	xr_string game_time(ALife::_TIME_ID t)
	{
		u32 y, mo, d, h, mi, s, ms;
		split_time(t, y, mo, d, h, mi, s, ms);
		string64 b;
		xr_sprintf(b, "%04u-%02u-%02u %02u:%02u:%02u", y, mo, d, h, mi, s);
		return b;
	}

	// PDA marker object; m_map_object_id is 0 (the actor) until a map location is set
	bool has_target(CGameTask* t) { return t->m_map_location.size() && t->m_map_object_id != u16(-1); }

	LPCSTR translated(const shared_str& s) { return s.size() ? CStringTable().translate(s).c_str() : nullptr; }

	bool level_ready(LPCSTR& reason)
	{
		reason = nullptr;
		if ((pApp && pApp->IsLoading()) || (g_pGameLevel && !g_pGameLevel->name().size()))
			reason = "loading";
		else if (!g_pGameLevel)
			reason = "no_level";
		else if (!g_actor)
			reason = "no_actor";
		return !reason;
	}

	void in_progress(xr_vector<CGameTask*>& out, LPCSTR only)
	{
		for (SGameTaskKey& k : Level().GameTaskManager().GetGameTasks())
			if (k.game_task && k.game_task->GetTaskState() == eTaskStateInProgress && (!only || k.task_id == only))
				out.push_back(k.game_task);
	}

	// {"id"?, "history"?: 0/1} -> {available, tasks: [...], history?: [...], script_error?, cost_ms}
	bool tasks_get(LPCSTR params, xr_string& out)
	{
		CTimer timer;
		timer.Start();
		LPCSTR reason;
		if (!level_ready(reason))
		{
			out = jobj().b("available", false).str("reason", reason).done();
			return true;
		}
		xr_string only;
		mcp::get_string_param(params, "id", only);
		s64 history = 0;
		mcp::get_int_param(params, "history", history);

		xr_vector<CGameTask*> tasks;
		in_progress(tasks, only.empty() ? nullptr : only.c_str());
		xr_vector<shared_str> ids;
		for (CGameTask* t : tasks)
			ids.push_back(t->m_ID);
		xr_vector<lua_task> lt;
		xr_string err;
		if (!ids.empty())
			run_chunk(ids, true, lt, err);

		CGameTask* active = Level().GameTaskManager().ActiveTask();
		jobj root;
		root.b("available", true).str("level", Level().name().c_str());
		xr_string list = "[";
		for (u32 n = 0; n < tasks.size(); n++)
		{
			CGameTask* t = tasks[n];
			jobj e;
			e.str("id", t->m_ID.c_str()).str("title", translated(t->m_Title)).str("description", translated(t->m_Description));
			e.str("type", t->GetTaskType() == eTaskTypeStoryline ? "storyline" : "additional");
			if (t == active)
				e.b("active", true);
			e.str("state", "in_progress").str("received", game_time(t->m_ReceiveTime).c_str());
			const lua_task* l = n < lt.size() && lt[n].found ? &lt[n] : nullptr;
			if (l)
			{
				if (l->stage >= 0)
					e.i("stage", l->stage);
				if (!l->status.empty())
					e.str("status", l->status.c_str());
				if (!l->last_check.empty())
					e.str("pending", l->last_check.c_str());
				if (!l->forced.empty())
					e.str("forced_status", l->forced.c_str());
			}
			e.raw("target", has_target(t) ? object_json(t->m_map_object_id, true) : xr_string("null"));
			if (l && l->giver >= 0 && l->giver < u16(-1))
				e.raw("giver", object_json((u16)l->giver, false));
			if (l && !l->def.empty())
				e.raw("def", l->def);
			list += (n ? "," : "") + e.done();
		}
		root.raw("tasks", list + "]");

		if (history)
		{
			xr_vector<CGameTask*> done;
			for (SGameTaskKey& k : Level().GameTaskManager().GetGameTasks())
				if (k.game_task && (k.game_task->GetTaskState() == eTaskStateCompleted || k.game_task->GetTaskState() == eTaskStateFail) &&
					(only.empty() || k.task_id == only.c_str()))
					done.push_back(k.game_task);
			std::stable_sort(done.begin(), done.end(), [](CGameTask* a, CGameTask* b) { return a->m_FinishTime > b->m_FinishTime; });
			xr_string h = "[";
			for (u32 n = 0; n < done.size() && n < max_history; n++)
			{
				CGameTask* t = done[n];
				jobj e;
				e.str("id", t->m_ID.c_str()).str("title", translated(t->m_Title)).str("description", translated(t->m_Description));
				e.str("state", t->GetTaskState() == eTaskStateCompleted ? "completed" : "failed");
				e.str("finished", game_time(t->m_FinishTime).c_str()).i("finished_game_ms", (s64)t->m_FinishTime);
				known k;
				bool have;
				{
					std::lock_guard<std::mutex> g(lock);
					auto it = last.find(t->m_ID);
					have = it != last.end();
					if (have)
						k = it->second;
				}
				if (have && k.stage >= 0)
					e.i("last_stage", k.stage);
				if (have && k.giver >= 0 && k.giver < u16(-1))
					e.raw("giver", object_json((u16)k.giver, false));
				h += (n ? "," : "") + e.done();
			}
			root.raw("history", h + "]");
			if (done.size() > max_history)
				root.i("history_omitted", done.size() - max_history);
		}
		if (!err.empty())
			root.str("script_error", err.c_str());
		root.num("stage_poll_us", poll_us, 1).num("stage_poll_max_us", poll_max_us, 1);
		root.num("cost_ms", timer.GetElapsed_sec() * 1000.f, 3);
		out = root.done();
		return true;
	}

	// "a,b" -> names (trimmed, empties dropped)
	xr_vector<xr_string> csv(const xr_string& s)
	{
		xr_vector<xr_string> r;
		for (size_t p = 0; p <= s.size();)
		{
			size_t e = s.find(',', p);
			if (e == xr_string::npos)
				e = s.size();
			xr_string t = s.substr(p, e - p);
			while (!t.empty() && t.front() == ' ')
				t.erase(0, 1);
			while (!t.empty() && t.back() == ' ')
				t.pop_back();
			if (!t.empty())
				r.push_back(t);
			p = e + 1;
		}
		return r;
	}

	xr_string names_json(const xr_vector<xr_string>& v)
	{
		xr_string r = "[";
		for (u32 n = 0; n < v.size(); n++)
			r += (n ? "," : "") + js(v[n].c_str());
		return r + "]";
	}

	// {"give"?: "a,b", "remove"?: "c"} -> applies them (same path as give_info_portion / disable_info_portion),
	// then {available, count, infos: [all known, registry order], given, removed}
	bool infos_get(LPCSTR params, xr_string& out)
	{
		LPCSTR reason;
		if (!level_ready(reason))
		{
			out = jobj().b("available", false).str("reason", reason).done();
			return true;
		}
		xr_string give, remove;
		mcp::get_string_param(params, "give", give);
		mcp::get_string_param(params, "remove", remove);
		CActor* a = g_actor;
		xr_vector<xr_string> given, removed;
		for (const xr_string& s : csv(give))
			if (!a->HasInfo(s.c_str()))
			{
				a->TransferInfo(s.c_str(), true);
				given.push_back(s);
			}
		for (const xr_string& s : csv(remove))
			if (a->HasInfo(s.c_str()))
			{
				a->TransferInfo(s.c_str(), false);
				removed.push_back(s);
			}
		jobj root;
		root.b("available", true);
		xr_string list = "[";
		u32 n = 0;
		if (a->m_known_info_registry)
			if (const KNOWN_INFO_VECTOR* v = a->m_known_info_registry->registry().objects_ptr())
				for (const shared_str& s : *v)
					list += (n++ ? "," : "") + js(s.c_str());
		root.i("count", n).raw("infos", list + "]");
		if (!give.empty())
			root.raw("given", names_json(given));
		if (!remove.empty())
			root.raw("removed", names_json(removed));
		out = root.done();
		return true;
	}
}

void mcp_register_tasks()
{
	if (!Core.ParamsData.test(ECoreParams::mcp_port))
		return;
	mcp::register_method("tasks.get", tasks_get);
	mcp::register_method("infos.get", infos_get);
}

// Last polled script stage of an in-progress task (<= 1 s old), -1 if unknown
s64 mcp_tasks_stage(const shared_str& id)
{
	std::lock_guard<std::mutex> g(lock);
	auto it = seen.find(id);
	return it != seen.end() ? it->second.stage : -1;
}

// Level unloaded or a save loaded: stages come back from the save, not from a change
void mcp_tasks_reset()
{
	std::lock_guard<std::mutex> g(lock);
	seen.clear();
	pending.clear();
}

void mcp_tasks_changed(CGameTask* t, LPCSTR change)
{
	std::lock_guard<std::mutex> g(lock);
	if (!xr_strcmp(change, "given"))
		seen[t->m_ID].stage = 0;
	else if (!xr_strcmp(change, "completed") || !xr_strcmp(change, "failed"))
	{
		auto it = seen.find(t->m_ID);
		if (it != seen.end())
		{
			last[t->m_ID] = it->second;
			seen.erase(it);
		}
	}
}

// Frame poll (main thread, from mcp_events): once per poll_ms compare the script stage of in-progress tasks.
// A change is reported one poll later: the script retargets the task on its next check (0.5-1 s after the stage).
void mcp_tasks_poll(bool level_loaded)
{
	u64 now = now_ms();
	if (now < next_poll)
		return;
	next_poll = now + poll_ms;
	LPCSTR reason;
	if (!level_loaded || !level_ready(reason) || !g_actor->g_Alive())
		return;
	xr_vector<CGameTask*> tasks;
	in_progress(tasks, nullptr);
	xr_vector<shared_str> ids;
	for (CGameTask* t : tasks)
		ids.push_back(t->m_ID);
	xr_vector<lua_task> lt;
	xr_string err;
	bool ok = true;
	if (!tasks.empty())
	{
		CTimer timer;
		timer.Start();
		ok = run_chunk(ids, false, lt, err);
		poll_us = timer.GetElapsed_sec() * 1e6f;
		poll_max_us = _max(poll_max_us, poll_us);
	}
	xr_map<shared_str, stage_change> due;
	{
		std::lock_guard<std::mutex> g(lock);
		due.swap(pending);
		for (u32 n = 0; ok && n < tasks.size(); n++)
		{
			if (!lt[n].found || lt[n].stage < 0)
				continue; // unknown to the script (e.g. task_info not loaded yet)
			known& k = seen[ids[n]];
			if (k.stage >= 0 && k.stage != lt[n].stage)
			{
				auto it = due.find(ids[n]);
				pending[ids[n]] = stage_change{ it != due.end() ? it->second.old : k.stage, lt[n].stage, tasks[n]->m_Title.size() ? translated(tasks[n]->m_Title) : "" };
				if (it != due.end())
					due.erase(it); // changed again before it was reported: one event old -> newest
			}
			k.stage = lt[n].stage;
			k.giver = lt[n].giver;
		}
	}
	for (auto& c : due)
	{
		CGameTask* t = Level().GameTaskManager().HasGameTask(c.first, true);
		jobj d;
		d.str("id", c.first.c_str()).str("title", c.second.title.c_str()).i("old_stage", c.second.old).i("stage", c.second.stage);
		d.raw("target", t && has_target(t) ? object_json(t->m_map_object_id, false) : xr_string("null"));
		mcp_event("task_stage", d.done().c_str());
	}
}
