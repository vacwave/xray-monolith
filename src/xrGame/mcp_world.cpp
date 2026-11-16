#include "pch_script.h"
#include "../xrEngine/mcp_server.h"
#include "../xrEngine/x_ray.h"
#include "../xrEngine/GameMtlLib.h"
#include "ai_space.h"
#include "Level.h"
#include "Actor.h"
#include "HUDManager.h"
#include "entity_alive.h"
#include "CustomMonster.h"
#include "memory_manager.h"
#include "enemy_manager.h"
#include "visual_memory_manager.h"
#include "monster_community.h"
#include "ai/stalker/ai_stalker.h"
#include "InventoryOwner.h"
#include "Inventory.h"
#include "inventory_item.h"
#include "character_info.h"
#include "relation_registry.h"
#include "Weapon.h"
#include "WeaponAmmo.h"
#include "Artefact.h"
#include "CustomZone.h"
#include "ZoneCampfire.h"
#include "level_changer.h"
#include "space_restrictor.h"
#include "InventoryBox.h"
#include "holder_custom.h"
#include "Helicopter.h"
#include "PhysicObject.h"
#include "ClimableObject.h"
#include "smart_cover_object.h"
#include "xrServer_Objects_Alife_Smartcovers.h"
#include "alife_simulator.h"
#include "alife_object_registry.h"
#include "alife_graph_registry.h"
#include "alife_level_registry.h"
#include "xrServer_Objects_ALife_Monsters.h"
#include "xrServer_Objects_ALife_Items.h"
#include "script_engine.h"
#include "string_table.h"
#include <typeinfo>

// MCP world.objects / world.object: find things around the actor without screenshots (read-only).
// Online objects come from Level().Objects (only top-level ones: items in inventories are skipped),
// optional offline ones from the alife level registry (server entities without a client object).
LPCSTR mcp_door_state(CGameObject* o, xr_string* json, bool* usable);

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

	enum kind_t
	{
		k_stalker, k_monster, k_corpse, k_item, k_weapon, k_ammo, k_artefact, k_anomaly, k_campfire, k_container,
		k_door, k_physic, k_vehicle, k_ladder, k_level_changer, k_smart_terrain, k_restrictor, k_cover, k_squad, k_other,
		k_count
	};

	LPCSTR kind_names[k_count] = {
		"stalker", "monster", "corpse", "item", "weapon", "ammo", "artefact", "anomaly", "campfire", "container",
		"door", "physic", "vehicle", "ladder", "level_changer", "smart_terrain", "restrictor", "cover", "squad", "other"
	};

	const u32 max_limit = 100, default_limit = 30, max_rays = 64, max_inventory = 50, max_offline_scan = 20000;

	// kinds csv (with aliases) -> bit mask; empty -> everything but restrictors and smart covers; false + bad name on error
	bool parse_kinds(const xr_string& csv, u32& mask, xr_string& bad)
	{
		mask = 0;
		if (csv.empty())
		{
			mask = ((1u << k_count) - 1) & ~(1u << k_restrictor) & ~(1u << k_cover);
			return true;
		}
		for (size_t p = 0; p <= csv.size();)
		{
			size_t e = csv.find(',', p);
			if (e == xr_string::npos)
				e = csv.size();
			xr_string t = csv.substr(p, e - p);
			p = e + 1;
			if (t.empty())
				continue;
			if (t == "all") mask |= (1u << k_count) - 1;
			else if (t == "npc") mask |= 1u << k_stalker;
			else if (t == "box") mask |= 1u << k_container;
			else if (t == "zone") mask |= 1u << k_anomaly;
			else if (t == "loot") mask |= (1u << k_item) | (1u << k_weapon) | (1u << k_ammo) | (1u << k_artefact);
			else if (t == "creature") mask |= (1u << k_stalker) | (1u << k_monster) | (1u << k_corpse);
			else
			{
				int k = 0;
				while (k < k_count && t != kind_names[k])
					k++;
				if (k == k_count)
				{
					bad = t;
					return false;
				}
				mask |= 1u << k;
			}
		}
		return true;
	}

	bool contains(LPCSTR s, LPCSTR sub) { return s && strstr(s, sub); }

	kind_t classify(CGameObject* o, CEntityAlive*& ea)
	{
		ea = smart_cast<CEntityAlive*>(o);
		if (ea)
			return !ea->g_Alive() ? k_corpse : smart_cast<CInventoryOwner*>(o) ? k_stalker : k_monster;
		if (CInventoryItem* it = smart_cast<CInventoryItem*>(o))
		{
			if (smart_cast<CWeapon*>(it)) return k_weapon;
			if (smart_cast<CWeaponAmmo*>(it)) return k_ammo;
			if (smart_cast<CArtefact*>(it)) return k_artefact;
			return k_item;
		}
		if (smart_cast<CZoneCampfire*>(o)) return k_campfire;
		if (smart_cast<CCustomZone*>(o)) return k_anomaly;
		if (smart_cast<CLevelChanger*>(o)) return k_level_changer;
		if (smart_cast<CSpaceRestrictor*>(o))
			return o->CLS_ID == TEXT2CLSID("SMRTTRRN") ? k_smart_terrain : k_restrictor;
		if (smart_cast<CInventoryBox*>(o)) return k_container;
		if (smart_cast<CHolderCustom*>(o) || smart_cast<CHelicopter*>(o)) return k_vehicle;
		if (smart_cast<CClimableObject*>(o)) return k_ladder;
		if (dynamic_cast<smart_cover::object*>(o)) return k_cover;
		if (smart_cast<CPhysicObject*>(o))
			return contains(o->cNameVisual().c_str(), "door") || contains(o->cNameSect_str(), "door") ? k_door : k_physic;
		return k_other;
	}

	kind_t classify_se(CSE_ALifeDynamicObject* se)
	{
		if (CSE_ALifeCreatureAbstract* c = smart_cast<CSE_ALifeCreatureAbstract*>(se))
			return !c->g_Alive() ? k_corpse : smart_cast<CSE_ALifeHumanAbstract*>(se) ? k_stalker : k_monster;
		if (smart_cast<CSE_ALifeOnlineOfflineGroup*>(se)) return k_squad;
		if (smart_cast<CSE_ALifeItemWeapon*>(se)) return k_weapon;
		if (smart_cast<CSE_ALifeItemAmmo*>(se)) return k_ammo;
		if (smart_cast<CSE_ALifeItemArtefact*>(se)) return k_artefact;
		if (smart_cast<CSE_ALifeInventoryItem*>(se)) return k_item;
		if (smart_cast<CSE_ALifeAnomalousZone*>(se)) return k_anomaly;
		if (smart_cast<CSE_ALifeLevelChanger*>(se)) return k_level_changer;
		if (smart_cast<CSE_ALifeSmartZone*>(se)) return k_smart_terrain;
		if (smart_cast<CSE_ALifeSpaceRestrictor*>(se)) return k_restrictor;
		if (smart_cast<CSE_ALifeInventoryBox*>(se)) return k_container;
		if (smart_cast<CSE_ALifeCar*>(se) || smart_cast<CSE_ALifeHelicopter*>(se)) return k_vehicle;
		if (smart_cast<CSE_ALifeObjectPhysic*>(se)) return k_physic;
		if (smart_cast<CSE_ALifeObjectClimable*>(se)) return k_ladder;
		if (smart_cast<CSE_SmartCover*>(se)) return k_cover;
		return k_other;
	}

	// case-insensitive glob (* and ?)
	bool glob(LPCSTR p, LPCSTR s)
	{
		if (!s)
			return false;
		for (; *p; p++, s++)
		{
			if (*p == '*')
			{
				while (p[1] == '*')
					p++;
				if (!p[1])
					return true;
				for (; *s; s++)
					if (glob(p + 1, s))
						return true;
				return false;
			}
			if (!*s || (*p != '?' && tolower((u8)*p) != tolower((u8)*s)))
				return false;
		}
		return !*s;
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

	xr_string class_name(const std::type_info& t)
	{
		LPCSTR n = t.name();
		if (!strncmp(n, "class ", 6))
			n += 6;
		return n;
	}

	float num_param(LPCSTR params, LPCSTR key, float def)
	{
		s64 v;
		if (mcp::get_int_param(params, key, v))
			return (float)v;
		xr_string s;
		if (mcp::get_string_param(params, key, s) && !s.empty())
			return (float)atof(s.c_str());
		return def;
	}

	// -1 absent, else 0/1
	int flag_param(LPCSTR params, LPCSTR key)
	{
		s64 v;
		return mcp::get_int_param(params, key, v) ? (v != 0) : -1;
	}

	CSE_ALifeDynamicObject* server_object(u16 id)
	{
		return g_ai_space && ai().get_alife() ? ai().alife().objects().object(id, true) : nullptr;
	}

	struct ctx
	{
		Fvector origin, eye;
		float yaw, pitch;
		CActor* actor;
		u32 rays = 0;
	};

	// Unobstructed ray from the camera to p (static geometry and other objects block; the target and actor do not)
	bool los_clear(ctx& c, const Fvector& p, CObject* target)
	{
		c.rays++;
		Fvector d;
		d.sub(p, c.eye);
		float len = d.magnitude();
		if (len < 0.3f)
			return true;
		d.div(len);
		xr_vector<CObject*> ignore;
		if (c.actor)
			ignore.push_back(c.actor);
		if (target)
			ignore.push_back(target);
		collide::rq_result r;
		r.O = nullptr;
		return !Level().ObjectSpace.RayPick(c.eye, d, len - 0.2f, collide::rqtBoth, r, ignore);
	}

	void bearing(const ctx& c, const Fvector& p, jobj& e)
	{
		Fvector d;
		d.sub(p, c.eye);
		float b = rad2deg(atan2f(d.x, d.z)) - c.yaw;
		while (b > 180.f) b -= 360.f;
		while (b <= -180.f) b += 360.f;
		e.num("bearing", b, 1).num("elevation", rad2deg(atan2f(d.y, _sqrt(d.x * d.x + d.z * d.z))) - c.pitch, 1);
	}

	// Inventory grouped by section (count, rounds for ammo), capped
	template <typename F>
	xr_string inventory_summary(F each, u32& total)
	{
		struct group { u32 count = 0, rounds = 0; LPCSTR name = nullptr; };
		xr_map<shared_str, group> g;
		total = 0;
		each([&](PIItem it)
		{
			total++;
			group& gr = g[it->object().cNameSect()];
			if (!gr.count)
				gr.name = it->NameItem();
			gr.count++;
			if (CWeaponAmmo* am = smart_cast<CWeaponAmmo*>(it))
				gr.rounds += am->m_boxCurr;
		});
		xr_string out = "[";
		u32 n = 0;
		for (auto& kv : g)
		{
			if (n >= max_inventory)
				break;
			jobj e;
			e.str("section", kv.first.c_str()).str("name", kv.second.name).i("count", kv.second.count);
			if (kv.second.rounds)
				e.i("rounds", kv.second.rounds);
			out += (n++ ? "," : "") + e.done();
		}
		return out + "]";
	}

	void squad_fields(jobj& e, u16 id)
	{
		CSE_ALifeMonsterAbstract* m = smart_cast<CSE_ALifeMonsterAbstract*>(server_object(id));
		if (!m)
			return;
		if (m->m_group_id != u16(-1))
		{
			e.i("squad_id", m->m_group_id);
			if (CSE_ALifeDynamicObject* sq = server_object(m->m_group_id))
				e.str("squad", sq->name_replace());
		}
		if (m->m_smart_terrain_id != u16(-1))
			if (CSE_ALifeDynamicObject* sm = server_object(m->m_smart_terrain_id))
				e.str("smart", sm->name_replace());
	}

	xr_string object_entry(ctx& c, CGameObject* o, kind_t k, CEntityAlive* ea, float dist, bool los, bool detail)
	{
		jobj e;
		e.i("id", o->ID()).str("kind", kind_names[k]).str("section", o->cNameSect_str()).str("name", o->cName().c_str());
		CInventoryOwner* io = smart_cast<CInventoryOwner*>(o);
		CInventoryItem* ii = smart_cast<CInventoryItem*>(o);
		if (io)
			e.str("display", io->Name());
		else if (ii)
			e.str("display", ii->NameItem());
		e.vec("position", o->Position()).num("distance", dist, 2);
		Fvector ctr;
		o->Center(ctr);
		bearing(c, ctr, e);
		if (los)
		{
			if (c.rays < max_rays || detail)
				e.b("los", los_clear(c, ctr, o));
		}

		if (ea)
		{
			bool alive = !!ea->g_Alive();
			e.b("alive", alive);
			if (k == k_corpse)
				e.str("of", io ? "stalker" : "monster");
			if (alive)
				e.num("health", ea->GetfHealth(), 2);
			if (io)
				e.str("community", io->CharacterInfo().Community().id().c_str()).i("rank", io->Rank());
			else if (ea->monster_community)
				e.str("community", ea->monster_community->id().c_str());
			if (alive && c.actor && ea->monster_community && c.actor->monster_community)
				if (LPCSTR r = relation_name(ea->tfGetRelationType(c.actor)))
					e.str("relation", r);
			if (alive)
			{
				if (CAI_Stalker* st = smart_cast<CAI_Stalker*>(o))
					if (st->wounded())
						e.b("wounded", true);
				if (CCustomMonster* cm = smart_cast<CCustomMonster*>(o))
				{
					const CEntityAlive* en = cm->memory().enemy().selected();
					e.b("in_combat", !!en);
					if (en)
						e.i("enemy_id", en->ID());
					if (c.actor)
						e.b("sees_actor", cm->memory().visual().visible_now(c.actor));
				}
			}
			squad_fields(e, o->ID());
			if (io && (k == k_corpse || detail))
			{
				u32 total;
				xr_string inv = inventory_summary([&](auto f) { for (PIItem it : io->inventory().m_all) f(it); }, total);
				e.i("items", total);
				if (k == k_corpse)
					e.b("has_loot", total > 0);
				if (detail)
				{
					e.raw("inventory", inv);
					if (PIItem act = io->inventory().ActiveItem())
						e.str("active_item", act->object().cNameSect_str());
					e.i("money", io->get_money());
				}
			}
		}
		else if (ii)
		{
			e.num("condition", ii->GetCondition(), 2).i("cost", ii->Cost());
			if (CWeapon* w = smart_cast<CWeapon*>(o))
			{
				e.i("ammo", w->GetAmmoElapsed()).i("mag_size", w->GetAmmoMagSize());
				if (w->GetAmmoType() < w->m_ammoTypes.size())
					e.str("ammo_type", w->m_ammoTypes[w->GetAmmoType()].c_str());
			}
			else if (CWeaponAmmo* am = smart_cast<CWeaponAmmo*>(o))
				e.i("rounds", am->m_boxCurr);
		}
		else if (CCustomZone* z = smart_cast<CCustomZone*>(o))
		{
			xr_string t = class_name(typeid(*z));
			e.str("type", t.c_str()).num("radius", z->Radius(), 2);
			if (CZoneCampfire* cf = smart_cast<CZoneCampfire*>(z))
				e.b("lit", cf->is_on());
			else
				e.b("enabled", z->IsEnabled());
			if (detail)
				e.num("effective_radius", z->GetEffectiveRadius(), 2).num("max_power", z->GetMaxPower(), 3);
		}
		else if (CInventoryBox* box = smart_cast<CInventoryBox*>(o))
		{
			e.b("empty", box->IsEmpty()).i("items", box->m_items.size());
			if (box->closed())
				e.b("closed", true);
			if (detail)
			{
				u32 total;
				e.raw("inventory", inventory_summary([&](auto f)
				{
					for (u16 id : box->m_items)
						if (PIItem it = smart_cast<PIItem>(Level().Objects.net_Find(id)))
							f(it);
				}, total));
			}
		}
		else if (k == k_level_changer)
		{
			if (CSE_ALifeLevelChanger* lc = smart_cast<CSE_ALifeLevelChanger*>(server_object(o->ID())))
				e.str("destination", lc->m_caLevelToChange.c_str());
		}
		else if (k == k_restrictor || k == k_smart_terrain)
			e.num("radius", o->Radius(), 2);
		else if (k == k_door)
		{
			xr_string d;
			mcp_door_state(o, &d, nullptr);
			e.raw("door", d);
		}
		return e.done();
	}

	// Door logic facts (xr_logic ph_door storage, ph_door.script); every access guarded, nil = no logic. Arg: object id
	const char* door_chunk =
		"local id = ...\n"
		"local db = rawget(_G, 'db') local st = type(db) == 'table' and type(db.storage) == 'table' and db.storage[id]\n"
		"if type(st) ~= 'table' or not st.active_section then return nil end\n"
		"local r = { section = st.active_section, scheme = st.active_scheme }\n"
		"local pd = st.active_scheme == 'ph_door' and st.ph_door\n"
		"if type(pd) == 'table' then\n"
		"  r.door = true r.locked = pd.locked == true r.closed = pd.closed == true r.not_for_npc = pd.not_for_npc == true\n"
		"  local a = pd.door_action\n"
		"  pcall(function() if a and a.initialized then r.at_open = a:is_open() r.at_closed = a:is_closed() end end)\n"
		"end\n"
		"pcall(function() local ini = st.ini if not ini then return end\n"
		"  for _, k in ipairs({ 'on_use', 'on_info', 'on_info2', 'on_actor_in_zone', 'on_actor_not_in_zone', 'on_timer' }) do\n"
		"    if ini:line_exist(r.section, k) then r[k] = ini:r_string_ex(r.section, k) end end end)\n"
		"return r\n";

	const char* door_keys[] = { "on_use", "on_info", "on_info2", "on_actor_in_zone", "on_actor_not_in_zone", "on_timer" };

	bool lua_flag(lua_State* L, int t, LPCSTR k, int& v)
	{
		lua_getfield(L, t, k);
		bool has = lua_type(L, -1) == LUA_TBOOLEAN;
		if (has)
			v = lua_toboolean(L, -1);
		lua_pop(L, 1);
		return has;
	}

	xr_string lua_str(lua_State* L, int t, LPCSTR k)
	{
		lua_getfield(L, t, k);
		xr_string r = lua_type(L, -1) == LUA_TSTRING ? lua_tostring(L, -1) : "";
		lua_pop(L, 1);
		return r;
	}

	xr_string offline_entry(ctx& c, CSE_ALifeDynamicObject* se, kind_t k, float dist)
	{
		jobj e;
		e.i("id", se->ID).str("kind", kind_names[k]).str("section", se->s_name.c_str()).str("name", se->name_replace());
		e.vec("position", se->o_Position).num("distance", dist, 2);
		bearing(c, se->o_Position, e);
		e.b("offline", true);
		if (CSE_ALifeCreatureAbstract* cr = smart_cast<CSE_ALifeCreatureAbstract*>(se))
			e.b("alive", cr->g_Alive());
		squad_fields(e, se->ID);
		return e.done();
	}

	// level + actor/origin readiness; false -> out holds {"available":false,...}
	bool ready(LPCSTR params, ctx& c, xr_string& out)
	{
		bool loading = pApp && pApp->IsLoading();
		LPCSTR reason = nullptr;
		if (loading || (g_pGameLevel && !g_pGameLevel->name().size()))
			reason = "loading";
		else if (!g_pGameLevel)
			reason = "no_level";
		c.actor = reason ? nullptr : g_actor;
		xr_string pos;
		if (!reason && mcp::get_string_param(params, "position", pos))
		{
			if (sscanf(pos.c_str(), "%f,%f,%f", &c.origin.x, &c.origin.y, &c.origin.z) != 3)
				reason = "bad_position";
		}
		else if (!reason && c.actor)
			c.origin = c.actor->Position();
		else if (!reason)
			reason = "no_actor";
		if (reason)
		{
			out = jobj().b("available", false).str("reason", reason).done();
			return false;
		}
		c.eye = Device.vCameraPosition;
		const Fvector& dir = Device.vCameraDirection;
		c.yaw = rad2deg(atan2f(dir.x, dir.z));
		c.pitch = rad2deg(asinf(_max(-1.f, _min(1.f, dir.y))));
		return true;
	}

	// What is under the crosshair (HUD pick): object or static geometry hit point + material
	xr_string crosshair(ctx& c)
	{
		if (!g_hud || !c.actor)
			return "null";
		const SPickParam& pp = HUD().GetPick();
		const collide::rq_result& rq = pp.result;
		jobj t;
		if (rq.range <= 0.f)
			return "null";
		t.num("distance", rq.range, 2);
		Fvector pt;
		pt.mad(pp.defs.start, pp.defs.dir, rq.range);
		t.vec("point", pt);
		if (CGameObject* go = rq.O ? smart_cast<CGameObject*>(rq.O) : nullptr)
		{
			CEntityAlive* ea;
			t.i("id", go->ID()).str("kind", kind_names[classify(go, ea)]).str("section", go->cNameSect_str());
		}
		else if (!rq.O && rq.element >= 0 && rq.element < Level().ObjectSpace.GetStaticModel()->get_tris_count())
		{
			u16 m = (u16)Level().ObjectSpace.GetStaticTris()[rq.element].material;
			if (m < GMLib.CountMaterial())
				t.str("material", GMLib.GetMaterialByIdx(m)->m_Name.c_str());
		}
		return t.done();
	}

	bool world_objects(LPCSTR params, xr_string& out)
	{
		CTimer timer;
		timer.Start();
		ctx c;
		if (!ready(params, c, out))
			return true;
		float radius = _max(0.f, num_param(params, "radius", 100.f));
		s64 lim = default_limit;
		mcp::get_int_param(params, "limit", lim);
		u32 limit = (u32)_max<s64>(0, _min<s64>(lim, max_limit));
		xr_string kinds, pattern;
		mcp::get_string_param(params, "kinds", kinds);
		mcp::get_string_param(params, "pattern", pattern);
		u32 mask;
		xr_string bad;
		if (!parse_kinds(kinds, mask, bad))
		{
			out = "unknown kind '" + bad + "'; valid: npc box zone loot creature all";
			for (LPCSTR n : kind_names)
				out = out + " " + n;
			return false;
		}
		int alive = flag_param(params, "alive");
		bool hostile = flag_param(params, "hostile_only") == 1, visible = flag_param(params, "visible_only") == 1;
		bool los = flag_param(params, "los") == 1, summary = flag_param(params, "summary") == 1;
		bool offline = flag_param(params, "offline") == 1;
		float r2 = radius * radius;

		struct cand { CGameObject* o; CEntityAlive* ea; float d; kind_t k; };
		xr_vector<cand> found;
		u32 counts[k_count] = {};
		u32 scanned = 0;
		CObjectList& list = Level().Objects;
		for (u32 n = 0, cnt = list.o_count(); n < cnt; n++)
		{
			CObject* ob = list.o_get_by_iterator(n);
			scanned++;
			if (!ob || ob->getDestroy() || ob->H_Parent() || ob == c.actor)
				continue;
			float d2 = ob->Position().distance_to_sqr(c.origin);
			if (d2 > r2)
				continue;
			CGameObject* o = smart_cast<CGameObject*>(ob);
			if (!o)
				continue;
			CEntityAlive* ea;
			kind_t k = classify(o, ea);
			if (!(mask & (1u << k)))
				continue;
			if (alive >= 0 && (!ea || !!ea->g_Alive() != (alive == 1)))
				continue;
			if (pattern.size() && !glob(pattern.c_str(), o->cNameSect_str()) && !glob(pattern.c_str(), o->cName().c_str()))
			{
				CInventoryOwner* io = smart_cast<CInventoryOwner*>(o);
				if (!io || !glob(pattern.c_str(), io->Name()))
					continue;
			}
			if (hostile)
			{
				if (!ea || !ea->g_Alive() || !c.actor || !ea->monster_community || !c.actor->monster_community)
					continue;
				ALife::ERelationType rt = ea->tfGetRelationType(c.actor);
				if (rt != ALife::eRelationTypeEnemy && rt != ALife::eRelationTypeWorstEnemy)
					continue;
			}
			if (visible)
			{
				Fvector ctr;
				o->Center(ctr);
				if (!::Render->ViewBase.testSphere_dirty(ctr, _max(o->Radius(), 0.1f)))
					continue;
			}
			counts[k]++;
			found.push_back({ o, ea, _sqrt(d2), k });
		}

		// offline: server entities on this level with no client object (hostile/visible filters cannot apply)
		struct ocand { CSE_ALifeDynamicObject* se; float d; kind_t k; };
		xr_vector<ocand> ofound;
		u32 ocounts[k_count] = {};
		if (offline && !hostile && !visible && g_ai_space && ai().get_alife())
		{
			u32 seen = 0;
			for (auto& kv : ai().alife().graph().level().objects())
			{
				CSE_ALifeDynamicObject* se = kv.second;
				if (++seen > max_offline_scan)
					break;
				if (!se || se->ID_Parent != u16(-1) || list.net_Find(se->ID))
					continue;
				float d2 = se->o_Position.distance_to_sqr(c.origin);
				if (d2 > r2)
					continue;
				kind_t k = classify_se(se);
				if (!(mask & (1u << k)))
					continue;
				CSE_ALifeCreatureAbstract* cr = smart_cast<CSE_ALifeCreatureAbstract*>(se);
				if (alive >= 0 && (!cr || cr->g_Alive() != (alive == 1)))
					continue;
				if (pattern.size() && !glob(pattern.c_str(), se->s_name.c_str()) && !glob(pattern.c_str(), se->name_replace()))
					continue;
				ocounts[k]++;
				ofound.push_back({ se, _sqrt(d2), k });
			}
		}

		jobj root;
		root.b("available", true).str("level", g_pGameLevel->name().c_str()).vec("origin", c.origin).num("radius", radius, 1);
		root.i("total", found.size());
		jobj cj;
		for (int k = 0; k < k_count; k++)
			if (counts[k])
				cj.i(kind_names[k], counts[k]);
		root.raw("counts", cj.done());
		if (offline)
		{
			jobj oc;
			for (int k = 0; k < k_count; k++)
				if (ocounts[k])
					oc.i(kind_names[k], ocounts[k]);
			root.i("offline_total", ofound.size()).raw("offline_counts", oc.done());
		}
		if (!summary)
		{
			u32 m = _min((u32)found.size(), limit);
			std::partial_sort(found.begin(), found.begin() + m, found.end(), [](const cand& a, const cand& b) { return a.d < b.d; });
			xr_string arr = "[";
			for (u32 n = 0; n < m; n++)
				arr += (n ? "," : "") + object_entry(c, found[n].o, found[n].k, found[n].ea, found[n].d, los, false);
			root.raw("objects", arr + "]");
			if (found.size() > m)
				root.i("omitted", found.size() - m);
			if (offline)
			{
				u32 om = _min((u32)ofound.size(), limit);
				std::partial_sort(ofound.begin(), ofound.begin() + om, ofound.end(), [](const ocand& a, const ocand& b) { return a.d < b.d; });
				xr_string oa = "[";
				for (u32 n = 0; n < om; n++)
					oa += (n ? "," : "") + offline_entry(c, ofound[n].se, ofound[n].k, ofound[n].d);
				root.raw("offline", oa + "]");
				if (ofound.size() > om)
					root.i("offline_omitted", ofound.size() - om);
			}
			if (los)
				root.i("rays", c.rays);
			root.raw("target", crosshair(c));
		}
		root.i("scanned", scanned);
		root.num("cost_ms", timer.GetElapsed_sec() * 1000.f, 3);
		out = root.done();
		return true;
	}

	bool world_object(LPCSTR params, xr_string& out)
	{
		CTimer timer;
		timer.Start();
		ctx c;
		if (!ready(params, c, out))
			return true;
		s64 id;
		if (!mcp::get_int_param(params, "id", id) || id < 0 || id >= 0xffff)
		{
			out = "id (0..65534) required";
			return false;
		}
		jobj root;
		root.b("available", true);
		CObject* ob = Level().Objects.net_Find((u16)id);
		CGameObject* o = ob && !ob->getDestroy() ? smart_cast<CGameObject*>(ob) : nullptr;
		if (o)
		{
			CEntityAlive* ea;
			kind_t k = classify(o, ea);
			root.raw("object", object_entry(c, o, k, ea, o->Position().distance_to(c.origin), true, true));
			if (CObject* p = o->H_Parent())
				root.i("parent_id", p->ID()).str("parent_section", p->cNameSect_str());
		}
		else if (CSE_ALifeDynamicObject* se = server_object((u16)id))
		{
			root.raw("object", offline_entry(c, se, classify_se(se), se->o_Position.distance_to(c.origin)));
			if (se->ID_Parent != u16(-1))
				root.i("parent_id", se->ID_Parent);
		}
		else
			root.raw("object", "null");
		root.num("cost_ms", timer.GetElapsed_sec() * 1000.f, 3);
		out = root.done();
		return true;
	}
}

void mcp_register_world()
{
	mcp::register_method("world.objects", world_objects);
	mcp::register_method("world.object", world_object);
}

bool mcp_world_is_door(CGameObject* o)
{
	CEntityAlive* ea;
	return o && classify(o, ea) == k_door;
}

// Door state from its ph_door logic: open, opening, closed, closing, locked, scripted (other scheme), no_logic, unknown;
// json = the "door" object (state, logic section, locked, not_for_npc, condlists, use tip); usable = the logic reacts to
// use (on_use)
LPCSTR mcp_door_state(CGameObject* o, xr_string* json, bool* usable)
{
	lua_State* L = g_ai_space ? ai().script_engine().lua() : nullptr;
	LPCSTR state = "no_logic";
	jobj d;
	bool use = false;
	if (L && lua_checkstack(L, 8))
	{
		int top = lua_gettop(L);
		if (!luaL_loadbuffer(L, door_chunk, xr_strlen(door_chunk), "=world.door"))
		{
			lua_pushinteger(L, o->ID());
			if (!lua_pcall(L, 1, 1, 0) && lua_istable(L, -1))
			{
				int t = lua_gettop(L);
				int door = 0, locked = 0, closed = 0, nfn = 0, at_open = -1, at_closed = -1;
				lua_flag(L, t, "door", door);
				lua_flag(L, t, "locked", locked);
				lua_flag(L, t, "closed", closed);
				lua_flag(L, t, "not_for_npc", nfn);
				bool ready = lua_flag(L, t, "at_open", at_open) && lua_flag(L, t, "at_closed", at_closed);
				if (!door)
					state = "scripted";
				else if (!ready)
					state = "unknown";
				else if (closed)
					state = locked ? "locked" : at_closed ? "closed" : "closing";
				else
					state = at_open ? "open" : "opening";
				d.str("state", state).str("logic", lua_str(L, t, "section").c_str());
				if (!door)
					d.str("scheme", lua_str(L, t, "scheme").c_str());
				else
				{
					d.b("locked", !!locked);
					if (nfn)
						d.b("not_for_npc", true);
				}
				for (LPCSTR k : door_keys)
				{
					xr_string v = lua_str(L, t, k);
					if (v.size())
						d.str(k, v.c_str());
				}
				use = lua_str(L, t, "on_use").size() > 0;
			}
		}
		lua_settop(L, top);
	}
	if (d.s.empty())
		d.str("state", state);
	if (LPCSTR tip = o->tip_text())
		d.str("tip", CStringTable().translate(tip).c_str());
	if (json)
		*json = d.done();
	if (usable)
		*usable = use;
	return state;
}

// Kind name (game_objects kinds) of an alife server entity; nullptr when there is none
LPCSTR mcp_world_kind(u16 id)
{
	CSE_ALifeDynamicObject* se = server_object(id);
	return se ? kind_names[classify_se(se)] : nullptr;
}
