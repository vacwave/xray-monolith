#include "pch_script.h"
#include "../xrEngine/mcp_server.h"
#include "../xrEngine/mcp_time.h"
#include "../xrEngine/x_ray.h"
#include "../xrEngine/XR_IOConsole.h"
#include "../xrEngine/xr_input.h"
#include "../xrEngine/Environment.h"
#include "ai_space.h"
#include "script_engine.h"
#include "Level.h"
#include "game_cl_base.h"
#include "Actor.h"
#include "Actor_Flags.h"
#include "ai_object_location.h"
#include "ActorCondition.h"
#include "CharacterPhysicsSupport.h"
#include "PHMovementControl.h"
#include "Inventory.h"
#include "inventory_item.h"
#include "InventoryOwner.h"
#include "character_info.h"
#include "relation_registry.h"
#include "Weapon.h"
#include "WeaponAmmo.h"
#include "Torch.h"
#include "CustomDevice.h"
#include "HUDManager.h"
#include "UIGameCustom.h"
#include "UIDialogHolder.h"
#include "MainMenu.h"
#include "GamePersistent.h"
#include "ui_base.h"
#include "UICursor.h"
#include "ui/UIActorMenu.h"
#include "ui/UIPdaWnd.h"
#include "ui/UITalkWnd.h"
#include "ui/UIScriptWnd.h"
#include "ui/UIMessageBoxEx.h"
#include "ui/UIGameTutorial.h"
#include "GametaskManager.h"
#include "GameTask.h"
#include "string_table.h"
#include "date_time.h"
#include <luabind/detail/object_rep.hpp>
#include <luabind/detail/class_rep.hpp>
#include <typeinfo>

// MCP state.get {"sections":"inventory,tasks,stats"|"all"}: one structured snapshot of the game for agents.
// Engine-owned facts are read in C++; script-owned ones (Anomaly surge/psi-storm managers, thirst/sleep, safe cover)
// come from a small guarded Lua chunk and are simply absent when a mod lacks them.
extern bool g_bDisableAllInput;
extern CUISequencer* g_tutorial;
xr_string mcp_ui_cursor_json(); // xrGame/mcp_ui.cpp

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
		bool empty() const { return s.empty(); }
		xr_string done() const { return s.empty() ? xr_string("{}") : s + "}"; }
	};

	// "class CUIActorMenu" -> "CUIActorMenu"
	xr_string class_name(const std::type_info& t)
	{
		LPCSTR n = t.name();
		if (!strncmp(n, "class ", 6))
			n += 6;
		else if (!strncmp(n, "struct ", 7))
			n += 7;
		return n;
	}

	lua_State* lua()
	{
		return g_ai_space ? ai().script_engine().lua() : nullptr;
	}

	struct script_wnd
	{
		xr_string name, mode; // Lua class name; string field "mode" (Anomaly UIInventory: inventory/loot/trade/repair)
		s64 npc_id = -1; // number field "npc_id" (UIInventory partner)
	};

	// Lua side of a script-made window (class "UISleep" (CUIScriptWnd)); empty for C++ windows
	script_wnd script_class(CUIDialogWnd* w)
	{
		script_wnd r;
		luabind::wrap_base* wb = dynamic_cast<luabind::wrap_base*>(w);
		lua_State* L = lua();
		if (!wb || !L)
			return r;
		const luabind::weak_ref& self = luabind::detail::wrap_access::ref(*wb);
		if (!*(void* const*)&self) // weak_ref::m_impl (private) is null until luabind binds the Lua object
			return r;
		int top = lua_gettop(L);
		if (lua_checkstack(L, 8))
		{
			self.get(L);
			luabind::detail::object_rep* o = luabind::detail::is_class_object(L, -1);
			if (o && o->crep() && o->crep()->name())
			{
				r.name = o->crep()->name();
				static const char* fields = "local o = ... local m, n = o.mode, o.npc_id\n"
					"return type(m) == 'string' and m or nil, type(n) == 'number' and n or nil";
				if (!luaL_loadbuffer(L, fields, xr_strlen(fields), "=state.get"))
				{
					lua_pushvalue(L, -2);
					if (!lua_pcall(L, 1, 2, 0))
					{
						if (lua_type(L, -2) == LUA_TSTRING)
							r.mode = lua_tostring(L, -2);
						if (lua_type(L, -1) == LUA_TNUMBER)
							r.npc_id = (s64)lua_tonumber(L, -1);
					}
				}
			}
		}
		lua_settop(L, top);
		return r;
	}

	LPCSTR item_name(CInventoryItem* it)
	{
		return it->NameItem();
	}

	jobj dialog_info(CUIDialogWnd* w)
	{
		jobj d;
		LPCSTR kind = "other";
		LPCSTR mode = nullptr;
		CInventoryOwner* partner = nullptr;
		if (CUIActorMenu* am = smart_cast<CUIActorMenu*>(w))
		{
			kind = "actor_menu";
			switch (am->GetMenuMode())
			{
			case mmInventory: mode = "inventory"; break;
			case mmTrade: mode = "trade"; break;
			case mmUpgrade: mode = "upgrade"; break;
			case mmDeadBodySearch: mode = am->GetPartner() ? "loot_body" : "loot_box"; break;
			default: mode = "undefined"; break;
			}
			partner = am->GetPartner();
		}
		else if (smart_cast<CUIPdaWnd*>(w))
			kind = "pda";
		else if (CUITalkWnd* t = smart_cast<CUITalkWnd*>(w))
		{
			kind = "talk";
			partner = t->OthersInvOwner();
		}
		else if (smart_cast<CUIMessageBoxEx*>(w))
			kind = "message_box";
		else if (smart_cast<CUIDialogWndEx*>(w))
			kind = "script";
		d.str("kind", kind);
		if (mode)
			d.str("menu_mode", mode);
		d.str("class", class_name(typeid(*w)).c_str());
		script_wnd sc = script_class(w);
		if (!sc.name.empty())
			d.str("script_class", sc.name.c_str());
		if (!sc.mode.empty())
			d.str("script_mode", sc.mode.c_str());
		CGameObject* p = smart_cast<CGameObject*>(partner);
		if (!p && sc.npc_id >= 0 && sc.npc_id < 0xffff && g_pGameLevel)
			p = smart_cast<CGameObject*>(Level().Objects.net_Find((u16)sc.npc_id));
		if (p)
		{
			CInventoryOwner* io = smart_cast<CInventoryOwner*>(p);
			d.i("partner_id", p->ID());
			d.str("partner_name", io ? io->Name() : p->Name());
			d.str("partner_section", p->cNameSect().c_str());
		}
		return d;
	}

	LPCSTR move_state(u32 m)
	{
		if (m & mcClimb) return "climb";
		if (m & (mcJump | mcFall)) return "jump";
		if (m & (mcLanding | mcLanding2)) return "landing";
		if (m & mcSprint) return "sprint";
		if (m & mcCrouch) return (m & mcAccel) ? "crouch_low" : "crouch";
		if (m & mcAnyMove) return (m & mcAccel) ? "walk" : "run";
		return "stand";
	}

	LPCSTR hud_state(u32 s, bool weapon)
	{
		static LPCSTR base[] = { "idle", "showing", "hiding", "hidden", "bore" };
		static LPCSTR wpn[] = { "fire", "fire2", "reload", "misfire", "switch", "switch_mode", "aim_start", "aim_end" };
		if (s <= CHUDState::eLastBaseState)
			return base[s];
		if (weapon && s - CWeapon::eFire < sizeof(wpn) / sizeof(wpn[0]))
			return wpn[s - CWeapon::eFire];
		return nullptr;
	}

	LPCSTR relation_name(ALife::ERelationType r)
	{
		switch (r)
		{
		case ALife::eRelationTypeFriend: return "friend";
		case ALife::eRelationTypeNeutral: return "neutral";
		case ALife::eRelationTypeEnemy: return "enemy";
		case ALife::eRelationTypeWorstEnemy: return "worst_enemy";
		default: return nullptr;
		}
	}

	jobj item_brief(CInventoryItem* it)
	{
		jobj o;
		o.i("id", it->object().ID());
		o.str("section", it->object().cNameSect().c_str());
		o.str("name", item_name(it));
		o.num("condition", it->GetCondition(), 3);
		return o;
	}

	// Script-owned facts; every access is guarded (missing managers/modules -> nil -> field absent)
	const char* script_chunk =
		"local function f(fn) local ok, v = pcall(fn) if ok then return v end end\n"
		"local function g(n) return rawget(_G, n) end\n"
		"local function m(mod, n) local t = g(mod) return type(t) == 'table' and rawget(t, n) or nil end\n"
		"local sm, pm = m('surge_manager', 'SurgeManager'), m('psi_storm_manager', 'PsiStormManager')\n"
		"return f(function() return sm and sm.started == true end),\n"
		"  f(function() return pm and pm.started == true end),\n"
		"  f(function() local e = g('GetEvent') return e and (e('current_safe_cover') and true or false) end),\n"
		"  f(function() local m = g('actor_status_thirst') return m and m.get_water_deprivation() end),\n"
		"  f(function() local m = g('actor_status_sleep') return m and m.get_sleep_deprivation() end)\n";

	struct script_facts
	{
		xr_string surge, psi_storm, in_cover, thirst, sleepiness, error;
	};

	xr_string lua_value(lua_State* L, int i)
	{
		switch (lua_type(L, i))
		{
		case LUA_TBOOLEAN: return lua_toboolean(L, i) ? "true" : "false";
		case LUA_TNUMBER: return fnum(lua_tonumber(L, i), 3);
		default: return "";
		}
	}

	void run_script(script_facts& r)
	{
		lua_State* L = lua();
		if (!L)
			return;
		int top = lua_gettop(L);
		if (luaL_loadbuffer(L, script_chunk, xr_strlen(script_chunk), "=state.get") || lua_pcall(L, 0, 5, 0))
		{
			LPCSTR m = lua_tostring(L, -1);
			r.error = m ? m : "error";
		}
		else
		{
			r.surge = lua_value(L, top + 1);
			r.psi_storm = lua_value(L, top + 2);
			r.in_cover = lua_value(L, top + 3);
			r.thirst = lua_value(L, top + 4);
			r.sleepiness = lua_value(L, top + 5);
		}
		lua_settop(L, top);
	}

	bool has(const xr_string& sections, LPCSTR name)
	{
		if (sections == "all")
			return true;
		size_t n = xr_strlen(name);
		for (size_t p = 0; (p = sections.find(name, p)) != xr_string::npos; p += n)
			if ((p == 0 || sections[p - 1] == ',') && (p + n == sections.size() || sections[p + n] == ','))
				return true;
		return false;
	}

	const u32 max_list = 100; // cap per inventory list / task list

	jobj inventory_info(CActor* a)
	{
		CInventory& inv = a->inventory();
		jobj o;
		xr_string slots = "[";
		for (u16 s = 1; s <= inv.LastSlot(); s++)
			if (PIItem it = inv.ItemFromSlot(s))
			{
				jobj e = item_brief(it);
				e.i("slot", s);
				slots += (slots.size() > 1 ? "," : "") + e.done();
			}
		o.raw("slots", slots + "]");
		xr_string belt = "[";
		u32 n = 0;
		for (PIItem it : inv.m_belt)
			if (n++ < max_list)
				belt += (n > 1 ? "," : "") + item_brief(it).done();
		o.raw("belt", belt + "]");

		// Backpack grouped by section: count of items (ammo: rounds too)
		struct group { u32 count = 0, rounds = 0; float condition = 0.f; LPCSTR name = nullptr; };
		xr_map<shared_str, group> ruck;
		for (PIItem it : inv.m_ruck)
		{
			group& g = ruck[it->object().cNameSect()];
			if (!g.count)
				g.name = item_name(it);
			g.count++;
			g.condition = _max(g.condition, it->GetCondition());
			if (CWeaponAmmo* am = smart_cast<CWeaponAmmo*>(it))
				g.rounds += am->m_boxCurr;
		}
		xr_string bag = "[";
		n = 0;
		for (auto& kv : ruck)
		{
			if (n++ >= max_list)
				break;
			jobj e;
			e.str("section", kv.first.c_str()).str("name", kv.second.name).i("count", kv.second.count);
			if (kv.second.rounds)
				e.i("rounds", kv.second.rounds);
			e.num("max_condition", kv.second.condition, 3);
			bag += (n > 1 ? "," : "") + e.done();
		}
		o.raw("backpack", bag + "]");
		o.i("backpack_items", inv.m_ruck.size());
		if (ruck.size() > max_list)
			o.i("backpack_sections_omitted", ruck.size() - max_list);
		return o;
	}

	xr_string tasks_info()
	{
		CGameTaskManager& tm = Level().GameTaskManager();
		CGameTask* active = tm.ActiveTask();
		xr_string out = "[";
		u32 n = 0;
		for (SGameTaskKey& k : tm.GetGameTasks())
		{
			CGameTask* t = k.game_task;
			if (!t || t->GetTaskState() != eTaskStateInProgress || n >= max_list)
				continue;
			jobj e;
			e.str("id", t->m_ID.c_str());
			e.str("title", t->m_Title.size() ? CStringTable().translate(t->m_Title).c_str() : nullptr);
			e.str("type", t->GetTaskType() == eTaskTypeStoryline ? "storyline" : "additional");
			if (t == active)
				e.b("active", true);
			out += (n++ ? "," : "") + e.done();
		}
		return out + "]";
	}

	bool state_get(LPCSTR params, xr_string& out)
	{
		CTimer timer;
		timer.Start();
		xr_string sections;
		mcp::get_string_param(params, "sections", sections);

		bool loading = pApp && pApp->IsLoading();
		bool level = g_pGameLevel && g_pGameLevel->name().size() && !loading;
		CMainMenu* mm = g_pGamePersistent ? MainMenu() : nullptr;
		bool menu = mm && mm->IsActive();
		bool console = Console && Console->bVisible;
		CUIGameCustom* gui = level ? CurrentGameUI() : nullptr;
		CUIDialogWnd* top = gui ? gui->TopInputReceiver() : nullptr;
		CActor* a = level ? g_actor : nullptr;
		bool dead = a && !a->g_Alive();
		bool tutorial = g_tutorial && g_tutorial->IsActive();

		LPCSTR mode;
		if (loading || (g_pGameLevel && !level))
			mode = "loading";
		else if (menu)
			mode = level ? "in_game_menu" : "main_menu";
		else if (!g_pGameLevel)
			mode = "main_menu";
		else if (!a)
			mode = "loading"; // level up, actor not spawned yet
		else if (dead)
			mode = "dead";
		else if (top)
			mode = "dialog";
		else if (tutorial)
			mode = "tutorial";
		else if (Device.Paused())
			mode = "paused";
		else
			mode = "in_game";

		jobj root;
		root.str("mode", console ? "console" : mode);
		if (console)
			root.str("under_console", mode);
		root.i("frame", Device.dwFrame);

		IInputReceiver* ir = pInput ? pInput->CurrentIR() : nullptr;
		jobj ui;
		ui.b("main_menu", menu).b("console", console).b("paused", Device.Paused()).b("mcp_paused", mcp_time::paused()).b("loading", loading);
		ui.b("cursor", g_pGamePersistent && GamePersistent().m_pUI_core && GetUICursor().IsVisible());
		ui.raw("cursor_pos", mcp_ui_cursor_json());
		ui.str("input_receiver", ir ? class_name(typeid(*ir)).c_str() : nullptr);
		if (top)
			ui.raw("dialog", dialog_info(top).done());
		if (menu && mm->TopInputReceiver())
			ui.raw("menu_dialog", dialog_info(mm->TopInputReceiver()).done());
		if (tutorial)
			ui.str("tutorial", g_tutorial->m_name);
		if (g_bDisableAllInput)
			ui.b("input_disabled", true);
		bool control = a && !dead && !menu && !console && !tutorial && !g_bDisableAllInput && !Device.Paused() &&
			(!top || !top->StopAnyMove()) && Level().CurrentControlEntity() == a && ir == g_pGameLevel;
		ui.b("actor_control", control);
		root.raw("ui", ui.done());

		if (level)
		{
			jobj w;
			w.str("level", g_pGameLevel->name().c_str());
			if (Level().game)
			{
				u32 y, mo, d, h, mi, s, ms;
				split_time(Level().GetGameTime(), y, mo, d, h, mi, s, ms);
				string64 t;
				xr_sprintf(t, "%04u-%02u-%02u %02u:%02u:%02u", y, mo, d, h, mi, s);
				w.str("game_time", t).num("game_time_factor", Level().GetGameTimeFactor(), 2);
			}
			w.num("time_factor", Device.time_factor(), 3);
			if (g_pGamePersistent)
				w.str("weather", g_pGamePersistent->Environment().GetWeather().c_str());

			script_facts sf;
			if (a)
				run_script(sf);
			if (!sf.surge.empty()) w.raw("surge", sf.surge);
			if (!sf.psi_storm.empty()) w.raw("psi_storm", sf.psi_storm);
			if (!sf.in_cover.empty()) w.raw("in_cover", sf.in_cover);

			if (a)
			{
				CActorCondition& c = a->conditions();
				jobj ac;
				ac.i("id", a->ID()).str("name", a->Name());
				ac.vec("position", a->Position());
				const Fvector& dir = Device.vCameraDirection;
				ac.num("yaw", rad2deg(atan2f(dir.x, dir.z)), 1).num("pitch", rad2deg(asinf(_max(-1.f, _min(1.f, dir.y)))), 1);
				ac.i("level_vertex", a->ai_location().level_vertex_id()).i("game_vertex", a->ai_location().game_vertex_id());
				ac.b("alive", !dead);
				ac.num("health", a->GetfHealth()).num("stamina", c.GetPower()).num("radiation", c.GetRadiation());
				ac.num("bleeding", c.BleedingSpeed()).num("psy_health", c.GetPsyHealth()).num("satiety", c.GetSatiety());
				if (!sf.thirst.empty()) ac.raw("thirst", sf.thirst);
				if (!sf.sleepiness.empty()) ac.raw("sleepiness", sf.sleepiness);
				u32 ms = a->MovingState();
				ac.str("move", move_state(ms));
				if (CPHMovementControl* mc = a->character_physics_support() ? a->character_physics_support()->movement() : nullptr)
					ac.num("speed", mc->GetVelocity().magnitude(), 2);
				ac.num("weight", a->inventory().TotalWeight(), 2).num("max_weight", a->MaxCarryWeight(), 2).num("max_walk_weight", a->MaxWalkWeight(), 2);
				ac.i("money", a->get_money());
				ac.str("community", a->CharacterInfo().Community().id().c_str());
				ac.i("rank", a->Rank()).i("reputation", a->Reputation());
				ac.b("god_mode", !!GodMode());
				if (a->Holder())
					ac.b("in_vehicle", true);
				if (PIItem o = a->inventory().ItemFromSlot(OUTFIT_SLOT))
					ac.raw("outfit", item_brief(o).done());
				if (PIItem hl = a->inventory().ItemFromSlot(HELMET_SLOT))
					ac.raw("helmet", item_brief(hl).done());
				root.raw("actor", ac.done());

				// Hands: active slot item, plus devices
				jobj hd;
				CInventory& inv = a->inventory();
				hd.i("active_slot", inv.GetActiveSlot());
				if (PIItem it = inv.ActiveItem())
				{
					jobj wi = item_brief(it);
					CWeapon* wp = smart_cast<CWeapon*>(it);
					if (CHudItem* hi = smart_cast<CHudItem*>(it))
					{
						LPCSTR st = hud_state(hi->GetState(), !!wp);
						if (st)
							wi.str("state", st);
						else
							wi.i("state", hi->GetState());
					}
					if (wp)
					{
						wi.i("ammo", wp->GetAmmoElapsed()).i("mag_size", wp->GetAmmoMagSize());
						if (wp->GetAmmoType() < wp->m_ammoTypes.size())
							wi.str("ammo_type", wp->m_ammoTypes[wp->GetAmmoType()].c_str());
						if (wp->unlimited_ammo()) // god mode / unlimited ammo cheat: GetSuitableAmmoTotal returns a fake 999
							wi.b("unlimited_ammo", true);
						else if (wp->GetAmmoMagSize() > 0)
							wi.i("spare_ammo", _max(0, wp->GetSuitableAmmoTotal() - wp->GetAmmoElapsed()));
						wi.i("fire_mode", wp->GetCurrentFireMode());
						wi.b("zoomed", wp->IsZoomed()).b("jammed", wp->IsMisfire());
					}
					hd.raw("item", wi.done());
				}
				if (CTorch* t = smart_cast<CTorch*>(inv.ItemFromSlot(TORCH_SLOT)))
					hd.b("torch", t->torch_active());
				hd.b("night_vision", a->GetNightVisionStatus());
				if (CCustomDevice* dv = smart_cast<CCustomDevice*>(inv.ItemFromSlot(DETECTOR_SLOT)))
				{
					jobj de = item_brief(dv);
					de.b("working", dv->IsWorking());
					hd.raw("detector", de.done());
				}
				root.raw("hands", hd.done());

				// Crosshair target
				if (g_hud)
				{
					const collide::rq_result& rq = HUD().GetPick().result;
					CGameObject* go = rq.O ? smart_cast<CGameObject*>(rq.O) : nullptr;
					jobj tg;
					if (rq.range > 0.f)
						tg.num("distance", rq.range, 2);
					if (go)
					{
						tg.i("id", go->ID()).str("section", go->cNameSect().c_str()).str("object", go->Name());
						CInventoryOwner* io = smart_cast<CInventoryOwner*>(go);
						if (io)
							tg.str("name", io->Name());
						else if (CInventoryItem* ii = smart_cast<CInventoryItem*>(go))
							tg.str("name", item_name(ii));
						if (CEntityAlive* ea = smart_cast<CEntityAlive*>(go))
							tg.b("alive", !!ea->g_Alive());
						if (io && io != static_cast<CInventoryOwner*>(a))
							if (LPCSTR r = relation_name(RELATION_REGISTRY().GetRelationType(io, static_cast<CInventoryOwner*>(a))))
								tg.str("relation", r);
					}
					root.raw("target", tg.done());
				}

				if (has(sections, "inventory"))
					root.raw("inventory", inventory_info(a).done());
			}
			root.raw("world", w.done());
			if (!sf.error.empty())
				root.str("script_error", sf.error.c_str());
			if (has(sections, "tasks") && a)
				root.raw("tasks", tasks_info());
		}

		if (has(sections, "stats"))
		{
			jobj st;
			st.i("frame", Device.dwFrame).i("rendered", Device.dwRenderedFrames);
			st.num("fps", Device.fTimeDelta > 0.f ? 1.f / Device.fTimeDelta : 0.f, 1).num("frame_ms", Device.fTimeDelta * 1000.f, 2);
			st.i("time_global_ms", Device.dwTimeGlobal);
			if (level && Level().game)
				st.i("game_time_ms", (s64)Level().GetGameTime());
			root.raw("stats", st.done());
		}
		root.num("cost_ms", timer.GetElapsed_sec() * 1000.f, 3);
		out = root.done();
		return true;
	}
}

void mcp_register_state()
{
	mcp::register_method("state.get", state_get);
}
