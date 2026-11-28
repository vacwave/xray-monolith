#include "pch_script.h"
#include "mcp_events.h"
#include "../xrEngine/mcp_server.h"
#include "../Include/xrRender/Kinematics.h"
#include "ai_space.h"
#include "script_engine.h"
#include "Level.h"
#include "Actor.h"
#include "Entity.h"
#include "entity_alive.h"
#include "CustomMonster.h"
#include "InventoryOwner.h"
#include "monster_community.h"
#include "character_community.h"
#include "alife_simulator.h"
#include "alife_object_registry.h"
#include "xrServer_Objects_ALife.h"
#include "Hit.h"
#include <mutex>
#include <chrono>

// Combat/death/relation "game" events (see mcp/README.md). Policies:
// - hit: per attacker+victim pair the first hit is sent at once; further hits within hit_window_ms of the last event
//   are only summed and sent as one summary per window (count, damage = summed hit power, health before the window ->
//   after the last hit). At most max_hits_per_sec hit events in total: over budget, hits wait as pending summaries.
// - combat: enter at once; enemy switches at most one per switch_min_ms per NPC (the enemy held then, `changes` = selection
//   changes folded in); leave only after the NPC had no enemy for leave_delay_ms (re-acquiring in that time sends nothing).
// - relation: only real changes (personal goodwill only from/to the actor); per key the same window policy as hits (old = before the window, new = latest); more
//   than max_relations_per_sec are only counted into one {"kind":"bulk"} event (scripts set whole relation tables at once).
// Hooks run on the main thread and on the GameThread (scheduler: enemy selection); one mutex guards the state.
namespace
{
	bool enabled = false;
	std::mutex lock; // guards everything below; mcp_event is called under it (lock order combat -> events -> out)
	DWORD main_tid = 0;
	enum { h_hit, h_death, h_enemy, h_relation, h_count };
	u32 off_thread[h_count] = {};
	bool off_logged[h_count] = {};
	LPCSTR hook_names[h_count] = { "hit", "death", "enemy", "relation" };

	const u64 hit_window_ms = 1000;
	const u32 max_hits_per_sec = 30;
	const u64 switch_min_ms = 3000;
	const u64 leave_delay_ms = 3000;
	const u64 relation_window_ms = 1000;
	const u32 max_relations_per_sec = 20;

	u64 now_ms()
	{
		using namespace std::chrono;
		return (u64)duration_cast<milliseconds>(steady_clock::now().time_since_epoch()).count();
	}

	xr_string js(LPCSTR s) { return s ? mcp::to_json_string(s) : xr_string("null"); }
	xr_string num(s64 v) { return std::to_string(v).c_str(); }
	xr_string flt(float v) { return make_string("%.4g", v).c_str(); }

	// Caller holds lock
	bool on_main(int hook)
	{
		if (!main_tid || GetCurrentThreadId() == main_tid)
			return true;
		off_thread[hook]++;
		return false;
	}

	// {"id","name","section"} of an online object, else of the ALife object, else {"id"}; null for an invalid id
	xr_string brief(u16 id)
	{
		if (id == u16(-1))
			return "null";
		if (g_pGameLevel)
			if (CObject* o = Level().Objects.net_Find(id))
				return "{\"id\":" + num(id) + ",\"name\":" + js(o->cName().c_str()) + ",\"section\":" + js(o->cNameSect().c_str()) + "}";
		if (ai().get_alife())
			if (CSE_ALifeDynamicObject* s = ai().alife().objects().object(id, true))
				return "{\"id\":" + num(id) + ",\"name\":" + js(s->name_replace()) + ",\"section\":" + js(s->s_name.c_str()) + "}";
		return "{\"id\":" + num(id) + "}";
	}

	LPCSTR community_name(int index)
	{
		CHARACTER_COMMUNITY c;
		c.set(index);
		return c.id().c_str();
	}

	LPCSTR bone_name(CObject* o, u16 bone)
	{
		IKinematics* k = o && o->Visual() ? o->Visual()->dcast_PKinematics() : nullptr;
		return k && bone < k->LL_BoneCount() ? k->LL_BoneName_dbg(bone) : nullptr;
	}

	// Weapon id of a hit, or -1 when it names no separate weapon (melee/monster hits carry 0 or the attacker itself)
	u16 hit_weapon(const SHit* h) { return h->weaponID == 0 || h->weaponID == h->whoID ? u16(-1) : h->weaponID; }

	LPCSTR hit_type_name(ALife::EHitType t) { return t < ALife::eHitTypeMax ? ALife::g_cafHitType2String(t) : nullptr; }

	struct hit_state
	{
		u64 last_emit = 0, last_hit = 0;
		u32 pending = 0;
		float damage = 0, health_before = 0, health_after = 0;
		ALife::EHitType type = ALife::eHitTypeMax;
		xr_string bone;
		u16 weapon = u16(-1);
		xr_string attacker, victim; // briefs
	};
	xr_map<u32, hit_state> hits; // key: attacker << 16 | victim
	u64 budget_start = 0;
	u32 budget_used = 0;

	struct last_hit
	{
		u16 who, weapon;
		ALife::EHitType type;
		xr_string bone;
		u64 time;
	};
	xr_map<u16, last_hit> last_hits; // per victim, for the death event

	struct combat_state
	{
		u16 enemy = u16(-1), current = u16(-1); // reported enemy, currently selected one
		xr_string npc, enemy_brief, current_brief;
		u64 last_emit = 0, leave_since = 0;
		u32 changes = 0;
	};
	xr_map<u16, combat_state> combat;

	struct relation_state
	{
		u64 last_emit = 0;
		u32 pending = 0;
		int old_v = 0, new_v = 0;
		xr_string head; // JSON members before old/new
	};
	xr_map<xr_string, relation_state> relations;
	u64 rel_budget_start = 0;
	u32 rel_budget_used = 0;
	u32 rel_bulk[3] = {};

	bool take_budget(u64 now, u64& start, u32& used, u32 max)
	{
		if (now - start >= 1000)
		{
			start = now;
			used = 0;
		}
		if (used >= max)
			return false;
		used++;
		return true;
	}

	// Caller holds lock
	void emit_hit(hit_state& h, u32 count, u64 now)
	{
		xr_string d = "{\"attacker\":" + h.attacker + ",\"victim\":" + h.victim + ",\"count\":" + num(count) + ",\"damage\":" + flt(h.damage) +
			",\"health_before\":" + flt(h.health_before) + ",\"health_after\":" + flt(h.health_after) + ",\"hit_type\":" + js(hit_type_name(h.type)) +
			",\"bone\":" + (h.bone.empty() ? xr_string("null") : js(h.bone.c_str())) + ",\"weapon\":" + brief(h.weapon) + "}";
		mcp_event("hit", d.c_str());
		h.last_emit = now;
		h.pending = 0;
		h.damage = 0;
	}

	void flush_relation(relation_state& r, int kind, u64 now)
	{
		if (take_budget(now, rel_budget_start, rel_budget_used, max_relations_per_sec))
		{
			mcp_event("relation", (r.head + ",\"old\":" + num(r.old_v) + ",\"new\":" + num(r.new_v) + ",\"count\":" + num(r.pending) + "}").c_str());
			r.last_emit = now;
		}
		else
			rel_bulk[kind] += r.pending;
		r.pending = 0;
	}

	// Caller holds lock: flush pending hit summaries of one victim (or all due ones when victim is -1)
	void flush_hits(u64 now, int victim)
	{
		for (auto it = hits.begin(); it != hits.end();)
		{
			hit_state& h = it->second;
			bool mine = victim >= 0 && u16(it->first & 0xffff) == u16(victim);
			if (h.pending && (mine || (now - h.last_emit >= hit_window_ms && take_budget(now, budget_start, budget_used, max_hits_per_sec))))
				emit_hit(h, h.pending, now);
			if (!h.pending && (mine || now - h.last_hit > 10000))
				it = hits.erase(it);
			else
				++it;
		}
	}
}

void mcp_combat_init()
{
	enabled = !!Core.ParamsData.test(ECoreParams::mcp_port);
}

void mcp_combat_hit(CEntity* victim, const SHit* h, float health_before)
{
	if (!enabled || !victim || health_before <= 0)
		return;
	u64 now = now_ms();
	u16 who = h->whoID;
	float after = victim->GetfHealth();
	LPCSTR bn = h->boneID != BI_NONE ? bone_name(victim, h->boneID) : nullptr;
	xr_string bone = bn ? bn : "";
	std::lock_guard<std::mutex> g(lock);
	on_main(h_hit);
	last_hits[victim->ID()] = { who, hit_weapon(h), h->hit_type, bone, now };

	hit_state& s = hits[(u32(who) << 16) | victim->ID()];
	if (s.attacker.empty())
	{
		s.attacker = brief(who);
		s.victim = "{\"id\":" + num(victim->ID()) + ",\"name\":" + js(victim->cName().c_str()) + ",\"section\":" + js(victim->cNameSect().c_str()) + "}";
	}
	if (!s.pending)
		s.health_before = health_before;
	s.pending++;
	s.last_hit = now;
	s.damage += h->damage();
	s.health_after = after;
	s.type = h->hit_type;
	s.bone = bone;
	s.weapon = hit_weapon(h);
	if (s.pending == 1 && now - s.last_emit >= hit_window_ms && take_budget(now, budget_start, budget_used, max_hits_per_sec))
		emit_hit(s, 1, now);
}

void mcp_combat_death(CEntity* victim, CObject* killer)
{
	if (!enabled || !victim)
		return;
	u64 now = now_ms();
	u16 id = victim->ID();
	std::lock_guard<std::mutex> g(lock);
	bool main = on_main(h_death);
	flush_hits(now, id);
	combat.erase(id);

	xr_string v = "{\"id\":" + num(id) + ",\"name\":" + js(victim->cName().c_str()) + ",\"section\":" + js(victim->cNameSect().c_str());
	if (CInventoryOwner* io = smart_cast<CInventoryOwner*>(victim))
		v += ",\"char_name\":" + js(io->Name()) + ",\"community\":" + js(community_name(io->Community()));
	else if (CEntityAlive* ea = smart_cast<CEntityAlive*>(victim))
		v += ",\"community\":" + js(ea->monster_community->id().c_str());
	// Anomaly's story ids live in Lua (_g.get_object_story_id); main thread only
	if (main && g_ai_space && ai().script_engine().lua())
	{
		lua_State* L = ai().script_engine().lua();
		int top = lua_gettop(L);
		lua_getglobal(L, "get_object_story_id");
		if (lua_isfunction(L, -1))
		{
			lua_pushnumber(L, id);
			if (!lua_pcall(L, 1, 1, 0) && lua_isstring(L, -1))
				v += ",\"story_id\":" + js(lua_tostring(L, -1));
		}
		lua_settop(L, top);
	}
	v += "}";

	xr_string d = "{\"victim\":" + v + ",\"killer\":" + (killer ? brief(killer->ID()) : xr_string("null"));
	auto it = last_hits.find(id);
	if (it != last_hits.end() && now - it->second.time < 5000 && (!killer || it->second.who == killer->ID()))
		d += ",\"weapon\":" + brief(it->second.weapon) + ",\"hit_type\":" + js(hit_type_name(it->second.type)) +
			",\"bone\":" + (it->second.bone.empty() ? xr_string("null") : js(it->second.bone.c_str()));
	else
		d += ",\"weapon\":null,\"hit_type\":null,\"bone\":null";
	if (it != last_hits.end())
		last_hits.erase(it);
	const Fvector& p = victim->Position();
	d += ",\"level\":" + (g_pGameLevel ? js(g_pGameLevel->name().c_str()) : xr_string("null")) +
		",\"position\":[" + flt(p.x) + "," + flt(p.y) + "," + flt(p.z) + "]}";
	mcp_event("death", d.c_str());
}

void mcp_combat_enemy(CCustomMonster* npc, const CEntityAlive* previous, const CEntityAlive* enemy)
{
	if (!enabled || !npc)
		return;
	u64 now = now_ms();
	std::lock_guard<std::mutex> g(lock);
	on_main(h_enemy);
	combat_state& c = combat[npc->ID()];
	if (c.npc.empty())
		c.npc = brief(npc->ID());
	if (!enemy)
	{
		c.current = u16(-1);
		if (c.enemy != u16(-1) && !c.leave_since)
			c.leave_since = now;
		return;
	}
	u16 eid = enemy->ID();
	c.leave_since = 0;
	if (eid != c.current)
	{
		c.current = eid;
		c.current_brief = brief(eid);
	}
	if (c.enemy == u16(-1))
	{
		mcp_event("combat", ("{\"state\":\"enter\",\"npc\":" + c.npc + ",\"enemy\":" + c.current_brief + "}").c_str());
		c.enemy = eid;
		c.enemy_brief = c.current_brief;
		c.last_emit = now;
		c.changes = 0;
	}
	else
		c.changes++;
}

void mcp_combat_relation(int kind, int from, int to, int old_v, int new_v)
{
	if (!enabled || old_v == new_v || kind < 0 || kind > 2)
		return;
	// NPC-to-NPC personal goodwill changes on every hit/kill a squad witnesses: only the actor's are reported
	if (kind == 0 && !(g_actor && (from == g_actor->ID() || to == g_actor->ID())))
		return;
	u64 now = now_ms();
	std::lock_guard<std::mutex> g(lock);
	on_main(h_relation);
	xr_string key = num(kind) + ":" + num(from) + ":" + num(to);
	relation_state& r = relations[key];
	if (r.head.empty())
	{
		if (kind == 0)
			r.head = "{\"kind\":\"personal\",\"from\":" + brief(u16(from)) + ",\"to\":" + brief(u16(to));
		else if (kind == 1)
			r.head = "{\"kind\":\"community_goodwill\",\"community\":" + js(community_name(from)) + ",\"to\":" + brief(u16(to));
		else
			r.head = "{\"kind\":\"community_relation\",\"community\":" + js(community_name(from)) + ",\"to_community\":" + js(community_name(to));
	}
	if (!r.pending)
		r.old_v = old_v;
	r.pending++;
	r.new_v = new_v;
	if (r.pending == 1 && now - r.last_emit >= relation_window_ms)
		flush_relation(r, kind, now);
}

void mcp_combat_poll(bool level_loaded)
{
	if (!enabled)
		return;
	if (!main_tid)
		main_tid = GetCurrentThreadId();
	u64 now = now_ms();
	std::lock_guard<std::mutex> g(lock);
	for (int i = 0; i < h_count; i++)
	{
		if (off_thread[i] && !off_logged[i])
		{
			off_logged[i] = true;
			Msg("* [MCP] combat hook '%s' runs off the main thread", hook_names[i]);
		}
	}
	if (!level_loaded)
	{
		hits.clear();
		last_hits.clear();
		combat.clear();
		relations.clear();
		return;
	}
	flush_hits(now, -1);
	for (auto it = combat.begin(); it != combat.end();)
	{
		combat_state& c = it->second;
		if (c.leave_since && now - c.leave_since >= leave_delay_ms)
		{
			mcp_event("combat", ("{\"state\":\"leave\",\"npc\":" + c.npc + ",\"enemy\":" + c.enemy_brief + "}").c_str());
			it = combat.erase(it);
			continue;
		}
		if (c.current != u16(-1) && c.current != c.enemy && now - c.last_emit >= switch_min_ms)
		{
			mcp_event("combat", ("{\"state\":\"switch\",\"npc\":" + c.npc + ",\"enemy\":" + c.current_brief + ",\"previous\":" + c.enemy_brief +
				",\"changes\":" + num(c.changes) + "}").c_str());
			c.enemy = c.current;
			c.enemy_brief = c.current_brief;
			c.last_emit = now;
			c.changes = 0;
		}
		++it;
	}
	for (auto it = relations.begin(); it != relations.end();)
	{
		relation_state& r = it->second;
		if (r.pending && now - r.last_emit >= relation_window_ms)
			flush_relation(r, it->first[0] - '0', now);
		if (!r.pending && now - r.last_emit > 10000)
			it = relations.erase(it);
		else
			++it;
	}
	if ((rel_bulk[0] || rel_bulk[1] || rel_bulk[2]) && now - rel_budget_start >= 1000)
	{
		mcp_event("relation", ("{\"kind\":\"bulk\",\"count\":" + num(rel_bulk[0] + rel_bulk[1] + rel_bulk[2]) + ",\"personal\":" + num(rel_bulk[0]) +
			",\"community_goodwill\":" + num(rel_bulk[1]) + ",\"community_relation\":" + num(rel_bulk[2]) + "}").c_str());
		rel_bulk[0] = rel_bulk[1] = rel_bulk[2] = 0;
	}
	for (auto it = last_hits.begin(); it != last_hits.end();)
		it = now - it->second.time > 10000 ? last_hits.erase(it) : std::next(it);
}
