#include "pch_script.h"
#include "../xrEngine/mcp_server.h"
#include "../xrEngine/x_ray.h"
#include "../Include/xrRender/DebugRender.h"
#include "ai_space.h"
#include "level_graph.h"
#include "game_graph.h"
#include "game_level_cross_table.h"
#include "graph_engine.h"
#include "ai_object_location.h"
#include "Level.h"
#include "Actor.h"
#include "level_changer.h"
#include "alife_simulator.h"
#include "alife_object_registry.h"
#include "xrServer_Objects_ALife.h"
#include "../xrEngine/mcp_input.h"
#include "../xrEngine/XR_IOConsole.h"
#include "../xrEngine/CameraBase.h"
#include "xr_level_controller.h"
#include "MainMenu.h"
#include "UIGameCustom.h"
#include "HUDManager.h"
#include "entity_alive.h"
#include "InventoryOwner.h"
#include "Inventory.h"
#include "Weapon.h"
#include "WeaponKnife.h"
#include "WeaponBinoculars.h"
#include "WeaponMagazinedWGrenade.h"
#include "Actor_Flags.h"
#include "Hit.h"
#include "../xrEngine/GameMtlLib.h"
#include "../Include/xrRender/Kinematics.h"
#include <queue>

// MCP nav.*: AI-map (level graph) and game-graph queries for agents (read-only), plus a debug overlay of nodes / a path
// drawn into the 3D view (same line path as Lua debug gizmos: DRender->add_lines, flushed by CLevel::OnRender).
bool mcp_world_is_door(CGameObject* o); // xrGame/mcp_world.cpp
LPCSTR mcp_door_state(CGameObject* o, xr_string* json, bool* usable);
extern void (*g_mcp_on_hit)(CEntity* victim, const SHit* hit); // xrGame/Entity.cpp

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

	xr_string fvec(const Fvector& v) { return "[" + fnum(v.x, 2) + "," + fnum(v.y, 2) + "," + fnum(v.z, 2) + "]"; }

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
		jobj& vec(LPCSTR k, const Fvector& v) { return raw(k, fvec(v)); }
		xr_string done() const { return s.empty() ? xr_string("{}") : s + "}"; }
	};

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

	s64 int_param(LPCSTR params, LPCSTR key, s64 def)
	{
		s64 v;
		return mcp::get_int_param(params, key, v) ? v : def;
	}

	// "x,y,z" -> 1 found, 0 absent, -1 malformed
	int vec_param(LPCSTR params, LPCSTR key, Fvector& out)
	{
		xr_string s;
		if (!mcp::get_string_param(params, key, s) || s.empty())
			return 0;
		return sscanf(s.c_str(), "%f,%f,%f", &out.x, &out.y, &out.z) == 3 ? 1 : -1;
	}

	// "x,y,z;x,y,z;..." (capped)
	bool vec_list_param(LPCSTR params, LPCSTR key, xr_vector<Fvector>& out, u32 cap)
	{
		xr_string s;
		if (!mcp::get_string_param(params, key, s))
			return false;
		out.clear();
		for (LPCSTR p = s.c_str(); *p && out.size() < cap;)
		{
			Fvector v;
			if (sscanf(p, "%f,%f,%f", &v.x, &v.y, &v.z) == 3)
				out.push_back(v);
			LPCSTR n = strchr(p, ';');
			if (!n)
				break;
			p = n + 1;
		}
		return true;
	}

	float wrap180(float a)
	{
		while (a > 180.f) a -= 360.f;
		while (a <= -180.f) a += 360.f;
		return a;
	}

	// world yaw of a horizontal direction, same convention as state.get yaw (0 = +z, 90 = +x)
	float heading(const Fvector& from, const Fvector& to) { return rad2deg(atan2f(to.x - from.x, to.z - from.z)); }

	float camera_yaw()
	{
		const Fvector& d = Device.vCameraDirection;
		return rad2deg(atan2f(d.x, d.z));
	}

	const u32 invalid = u32(-1);

	// AI-map helpers over the sorted node array (nodes are ordered by xz = x * row_length + z)
	struct navmap
	{
		const CLevelGraph& lg;
		float cell;
		u32 row;
		Fbox box;

		navmap(const CLevelGraph& g) : lg(g), cell(g.header().cell_size()), row(g.row_length()), box(g.header().box()) {}

		int cx(float x) const { return iFloor((x - box.min.x) / cell + .5f); }
		int cz(float z) const { return iFloor((z - box.min.z) / cell + .5f); }
		bool in_grid(int x, int z) const { return x >= 0 && z >= 0 && x <= (int)lg.max_x() && z <= (int)lg.max_z(); }

		// [first, last) node ids at grid cell (x, z) (several when floors overlap)
		bool cell_nodes(int x, int z, u32& first, u32& last) const
		{
			if (!in_grid(x, z))
				return false;
			u32 xz = u32(x) * row + u32(z);
			const CLevelGraph::CVertex* B = lg.begin();
			const CLevelGraph::CVertex* E = lg.end();
			const CLevelGraph::CVertex* I = std::lower_bound(B, E, xz);
			if (I == E || I->position().xz() != xz)
				return false;
			first = u32(I - B);
			for (last = first + 1; B + last != E && B[last].position().xz() == xz; ++last);
			return true;
		}

		float plane_y(u32 id, float x, float z) const { return lg.vertex_plane_y(lg.vertex(id), x, z); }
		Fvector centre(u32 id) const { return lg.vertex_position(id); }

		// node at p's x/z whose plane is closest to p.y (prefer a node at most `above` metres above p)
		u32 node_at(const Fvector& p, float* dy = nullptr) const
		{
			u32 f, l;
			if (!cell_nodes(cx(p.x), cz(p.z), f, l))
				return invalid;
			u32 best = invalid;
			float bd = flt_max;
			for (u32 id = f; id < l; ++id)
			{
				float d = p.y - plane_y(id, p.x, p.z);
				float score = d >= -.5f ? d : 1000.f - d; // nodes below (or barely above) first
				if (score < bd)
				{
					bd = score;
					best = id;
					if (dy)
						*dy = d;
				}
			}
			return best;
		}

		// node at cell (x,z) closest in height to y
		u32 node_near_y(int x, int z, float y, float wx, float wz) const
		{
			u32 f, l;
			if (!cell_nodes(x, z, f, l))
				return invalid;
			u32 best = f;
			float bd = flt_max;
			for (u32 id = f; id < l; ++id)
			{
				float d = _abs(plane_y(id, wx, wz) - y);
				if (d < bd)
				{
					bd = d;
					best = id;
				}
			}
			return best;
		}

		// nearest node centre in 3D (only nodes with comp_of[id] == comp when comp_of is given): rings of cells up to
		// 50 m, then a full scan; yw > 1 weighs height differences more (keeps a target on its own floor)
		u32 nearest(const Fvector& p, float& dist, bool& scanned, const u32* comp_of = nullptr, u32 comp = 0,
		            float yw = 1.f) const
		{
			auto d2 = [&](u32 id)
			{
				Fvector c = centre(id);
				return _sqr(c.x - p.x) + _sqr(c.z - p.z) + _sqr(yw * (c.y - p.y));
			};
			scanned = false;
			int x0 = cx(p.x), z0 = cz(p.z);
			int max_ring = iCeil(50.f / cell);
			u32 best = invalid;
			float bd = flt_max;
			for (int r = 0; r <= max_ring; ++r)
			{
				if (best != invalid && (r - 1) * cell > bd)
					break;
				for (int x = x0 - r; x <= x0 + r; ++x)
				{
					int step = (x == x0 - r || x == x0 + r) ? 1 : 2 * r;
					for (int z = z0 - r; z <= z0 + r; z += step ? step : 1)
					{
						u32 f, l;
						if (!cell_nodes(x, z, f, l))
							continue;
						for (u32 id = f; id < l; ++id)
						{
							if (comp_of && comp_of[id] != comp)
								continue;
							float d = _sqrt(d2(id));
							if (d < bd)
							{
								bd = d;
								best = id;
							}
						}
					}
				}
			}
			if (best == invalid)
			{
				scanned = true;
				for (u32 id = 0, n = lg.header().vertex_count(); id < n; ++id)
				{
					if (comp_of && comp_of[id] != comp)
						continue;
					float d = d2(id);
					if (d < bd)
					{
						bd = d;
						best = id;
					}
				}
				bd = _sqrt(bd);
			}
			dist = bd;
			return best;
		}

		// How far a straight walk from p (inside node start) along unit dir stays on linked nodes (max maxd)
		float ray_walk(u32 start, const Fvector& p, float dx, float dz, float maxd, u32& end) const
		{
			float half = cell * .5f;
			u32 cur = start;
			end = start;
			for (u32 steps = 0, cap = u32(maxd / cell) * 2 + 8; steps < cap; ++steps)
			{
				int ix, iz;
				lg.unpack_xz(lg.vertex(cur), ix, iz);
				float wx = box.min.x + ix * cell, wz = box.min.z + iz * cell;
				float tx = dx > EPS ? (wx + half - p.x) / dx : dx < -EPS ? (wx - half - p.x) / dx : flt_max;
				float tz = dz > EPS ? (wz + half - p.z) / dz : dz < -EPS ? (wz - half - p.z) / dz : flt_max;
				float texit = _min(tx, tz);
				if (texit >= maxd)
					return maxd;
				int nx[2] = { ix, ix }, nz[2] = { iz, iz };
				int cand = 1;
				if (_abs(tx - tz) < 1e-4f)
				{
					nx[0] += dx > 0 ? 1 : -1;
					nz[1] += dz > 0 ? 1 : -1;
					cand = 2;
				}
				else if (tx < tz)
					nx[0] += dx > 0 ? 1 : -1;
				else
					nz[0] += dz > 0 ? 1 : -1;
				u32 next = invalid;
				const CLevelGraph::CVertex* v = lg.vertex(cur);
				for (int c = 0; c < cand && next == invalid; ++c)
					for (int k = 0; k < 4; ++k)
					{
						u32 n = v->link(k);
						if (!lg.valid_vertex_id(n))
							continue;
						int ux, uz;
						lg.unpack_xz(lg.vertex(n), ux, uz);
						if (ux == nx[c] && uz == nz[c])
						{
							next = n;
							break;
						}
					}
				if (next == invalid)
					return _max(0.f, texit);
				cur = end = next;
			}
			return maxd;
		}

		// b is a linked neighbour of a, or two links away (diagonal cell)
		bool near_linked(u32 a, u32 b) const
		{
			if (a == b)
				return true;
			const CLevelGraph::CVertex* v = lg.vertex(a);
			for (int k = 0; k < 4; ++k)
			{
				u32 n = v->link(k);
				if (!lg.valid_vertex_id(n))
					continue;
				if (n == b)
					return true;
				const CLevelGraph::CVertex* w = lg.vertex(n);
				for (int j = 0; j < 4; ++j)
					if (w->link(j) == b)
						return true;
			}
			return false;
		}

		// node at (x, z) within 1 m of y and linked to `from` (not across a thin wall), else invalid
		u32 floor_node(float x, float y, float z, u32 from) const
		{
			u32 id = node_near_y(cx(x), cz(z), y, x, z);
			if (id == invalid || _abs(plane_y(id, x, z) - y) > 1.f || (from != invalid && !near_linked(from, id)))
				return invalid;
			return id;
		}

		// a straight walk a-b keeps r metres of AI map on both sides (sampled every half cell, not within r of the
		// ends: those are corners)
		bool clear(const Fvector& a, const Fvector& b, float r) const
		{
			float dx = b.x - a.x, dz = b.z - a.z, len = _sqrt(dx * dx + dz * dz);
			if (len < 2 * r)
				return true;
			dx /= len;
			dz /= len;
			for (float t = r; t <= len - r; t += cell * .5f)
			{
				float x = a.x + dx * t, z = a.z + dz * t, y = a.y + (b.y - a.y) * t / len;
				u32 c = floor_node(x, y, z, invalid);
				if (c == invalid)
					return false;
				y = plane_y(c, x, z);
				if (floor_node(x - dz * r, y, z + dx * r, c) == invalid || floor_node(x + dz * r, y, z - dx * r, c) == invalid)
					return false;
			}
			return true;
		}
	};
	const float clearance = .32f; // kept between a walked line and the AI map edge (the actor capsule grazed walls)

	// Per-node visit stamps (no clearing between searches)
	struct stamps
	{
		xr_vector<u32> mark;
		u32 epoch = 0;
		void begin(u32 n)
		{
			if (mark.size() != n)
			{
				mark.assign(n, 0);
				epoch = 0;
			}
			if (++epoch == 0)
			{
				mark.assign(n, 0);
				epoch = 1;
			}
		}
		bool seen(u32 id) const { return mark[id] == epoch; }
		void set(u32 id) { mark[id] = epoch; }
	};
	stamps g_reach;

	// BFS over links from start, nodes whose centre lies within radius (horizontal) of origin; marks g_reach
	u32 flood(const navmap& m, u32 start, const Fvector& origin, float radius, u32 cap, xr_vector<u32>* out = nullptr)
	{
		g_reach.begin(m.lg.header().vertex_count());
		if (start == invalid)
			return 0;
		xr_vector<u32> q;
		q.reserve(4096);
		q.push_back(start);
		g_reach.set(start);
		float r2 = radius * radius;
		for (size_t h = 0; h < q.size() && q.size() < cap; ++h)
		{
			const CLevelGraph::CVertex* v = m.lg.vertex(q[h]);
			for (int k = 0; k < 4; ++k)
			{
				u32 n = v->link(k);
				if (!m.lg.valid_vertex_id(n) || g_reach.seen(n))
					continue;
				Fvector c = m.centre(n);
				if (_sqr(c.x - origin.x) + _sqr(c.z - origin.z) > r2)
					continue;
				g_reach.set(n);
				q.push_back(n);
			}
		}
		u32 n = (u32)q.size();
		if (out)
			out->swap(q);
		return n;
	}

	// Connected components of the AI map (BFS over links), built once per loaded level: exact "is there any route"
	struct components_t
	{
		const CLevelGraph* lg = nullptr;
		shared_str level;
		xr_vector<u32> of; // node -> component
		xr_vector<u32> size; // component -> nodes
		float build_ms = 0;
	};
	components_t g_comp;

	const components_t& components(const navmap& m)
	{
		u32 n = m.lg.header().vertex_count();
		if (g_comp.lg == &m.lg && g_comp.of.size() == n && g_comp.level == g_pGameLevel->name())
			return g_comp;
		CTimer t;
		t.Start();
		g_comp.lg = &m.lg;
		g_comp.level = g_pGameLevel->name();
		g_comp.of.assign(n, invalid);
		g_comp.size.clear();
		xr_vector<u32> q;
		q.reserve(n);
		for (u32 s = 0; s < n; ++s)
		{
			if (g_comp.of[s] != invalid)
				continue;
			u32 c = (u32)g_comp.size.size();
			q.clear();
			q.push_back(s);
			g_comp.of[s] = c;
			for (size_t h = 0; h < q.size(); ++h)
			{
				const CLevelGraph::CVertex* v = m.lg.vertex(q[h]);
				for (int k = 0; k < 4; ++k)
				{
					u32 nb = v->link(k);
					if (nb < n && g_comp.of[nb] == invalid)
					{
						g_comp.of[nb] = c;
						q.push_back(nb);
					}
				}
			}
			g_comp.size.push_back((u32)q.size());
		}
		g_comp.build_ms = t.GetElapsed_sec() * 1000.f;
		return g_comp;
	}

	struct ctx
	{
		const CLevelGraph* lg = nullptr;
		Fvector origin;
		bool from_actor = false;
		float yaw = 0;
	};

	// level + AI map readiness; origin = position param or the actor; false -> out holds {"available":false,...}
	bool ready(LPCSTR params, ctx& c, xr_string& out, LPCSTR key = "position")
	{
		LPCSTR reason = nullptr;
		if ((pApp && pApp->IsLoading()) || (g_pGameLevel && !g_pGameLevel->name().size()))
			reason = "loading";
		else if (!g_pGameLevel)
			reason = "no_level";
		else if (!g_ai_space || !ai().get_level_graph())
			reason = "no_ai_map";
		if (!reason)
		{
			int r = vec_param(params, key, c.origin);
			if (r < 0)
				reason = "bad_position";
			else if (r == 0 && g_actor)
			{
				c.origin = g_actor->Position();
				c.from_actor = true;
			}
			else if (r == 0)
				reason = "no_actor";
		}
		if (reason)
		{
			out = jobj().b("available", false).str("reason", reason).done();
			return false;
		}
		c.lg = ai().get_level_graph();
		c.yaw = camera_yaw();
		return true;
	}

	// start node for queries at c.origin: the actor's own node, else the node under the point, else the nearest
	u32 origin_node(const navmap& m, const ctx& c)
	{
		if (c.from_actor && g_actor)
		{
			u32 id = g_actor->ai_location().level_vertex_id();
			if (m.lg.valid_vertex_id(id))
				return id;
		}
		float dy;
		u32 id = m.node_at(c.origin, &dy);
		if (id != invalid && _abs(dy) <= 1.5f)
			return id;
		float d;
		bool sc;
		return m.nearest(c.origin, d, sc);
	}

	// game vertex for a level node (cross table), invalid without a game graph
	u32 game_vertex(u32 level_vid)
	{
		if (!ai().get_game_graph() || level_vid == invalid)
			return invalid;
		const CGameLevelCrossTable& ct = ai().cross_table();
		if (level_vid >= ct.header().level_vertex_count())
			return invalid;
		return ct.vertex(level_vid).game_vertex_id();
	}

	LPCSTR level_name(u32 level_id)
	{
		if (!ai().get_game_graph())
			return nullptr;
		const auto& levels = ai().game_graph().header().levels();
		auto it = levels.find((GameGraph::_LEVEL_ID)level_id);
		return it != levels.end() ? it->second.name().c_str() : nullptr;
	}

	// {id, position, horizontal, vertical, distance} of a node relative to p
	xr_string node_json(const navmap& m, u32 id, const Fvector& p)
	{
		if (id == invalid)
			return "null";
		Fvector ctr = m.centre(id);
		jobj e;
		e.i("id", id).vec("position", ctr).num("horizontal", _sqrt(_sqr(ctr.x - p.x) + _sqr(ctr.z - p.z)), 2);
		e.num("vertical", p.y - ctr.y, 2).num("distance", ctr.distance_to(p), 2);
		return e.done();
	}

	// Snap a point to the AI map: node under it (|dy| <= 1.5) or the nearest one
	struct snap_t
	{
		u32 id = invalid;
		bool on_mesh = false;
		float dy = 0, dist = 0;
		Fvector point;
	};

	snap_t snap(const navmap& m, const Fvector& p)
	{
		snap_t s;
		float dy;
		u32 id = m.node_at(p, &dy);
		if (id != invalid && _abs(dy) <= 1.5f)
		{
			s.id = id;
			s.on_mesh = true;
			s.dy = dy;
			s.point.set(p.x, m.plane_y(id, p.x, p.z), p.z);
			return s;
		}
		bool sc;
		s.id = m.nearest(p, s.dist, sc, nullptr, 0, 3.f);
		if (s.id != invalid)
		{
			s.point = m.centre(s.id);
			s.dist = s.point.distance_to(p);
		}
		return s;
	}

	// ------------------------------------------------------------------ nav.info

	bool nav_info(LPCSTR params, xr_string& out)
	{
		CTimer t;
		t.Start();
		ctx c;
		if (!ready(params, c, out))
			return true;
		navmap m(*c.lg);
		const CLevelGraph::CHeader& h = c.lg->header();
		jobj r;
		r.b("available", true).str("level", g_pGameLevel->name().c_str()).i("nodes", h.vertex_count());
		r.num("cell_size", h.cell_size(), 3).num("factor_y", h.factor_y(), 2);
		r.raw("box", "{\"min\":" + fvec(h.box().min) + ",\"max\":" + fvec(h.box().max) + "}");
		r.i("grid_x", c.lg->max_x() + 1).i("grid_z", c.lg->max_z() + 1);
		if (CGameGraph* gg = ai().get_game_graph())
		{
			u32 lid = c.lg->level_id();
			r.i("level_id", lid);
			u32 count = 0;
			xr_string exits = "[";
			u32 ne = 0;
			for (u32 v = 0, n = gg->header().vertex_count(); v < n; ++v)
			{
				const CGameGraph::CVertex* gv = gg->vertex(v);
				if (gv->level_id() != lid)
					continue;
				count++;
				CGameGraph::const_iterator I, E;
				gg->begin(v, I, E);
				for (; I != E; ++I)
				{
					u32 to = gg->value(v, I);
					const CGameGraph::CVertex* tv = gg->vertex(to);
					if (tv->level_id() == lid || ne >= 32)
						continue;
					jobj e;
					e.str("to_level", level_name(tv->level_id())).i("game_vertex", v).i("to_game_vertex", to);
					e.vec("position", gv->level_point()).vec("to_position", tv->level_point());
					if (c.from_actor)
						e.num("distance", gv->level_point().distance_to(c.origin), 1);
					exits += (ne++ ? "," : "") + e.done();
				}
			}
			r.i("game_vertices", count).raw("exits", exits + "]");
		}
		r.num("cost_ms", t.GetElapsed_sec() * 1000.f, 3);
		out = r.done();
		return true;
	}

	// ------------------------------------------------------------------ nav.at

	bool nav_at(LPCSTR params, xr_string& out)
	{
		CTimer t;
		t.Start();
		ctx c;
		if (!ready(params, c, out))
			return true;
		navmap m(*c.lg);
		const Fvector& p = c.origin;
		jobj r;
		r.b("available", true).str("level", g_pGameLevel->name().c_str()).vec("point", p);
		r.b("inside_bounds", !!c.lg->valid_vertex_position(p)).num("cell_size", m.cell, 3);
		float dy = 0;
		u32 under = m.node_at(p, &dy);
		bool on = under != invalid && _abs(dy) <= 1.5f;
		r.b("on_mesh", on);
		if (under != invalid)
			r.i("node_under", under).num("height_above_node", dy, 2);
		u32 id = on ? under : invalid;
		float dist = 0;
		bool scanned = false;
		if (!on)
			id = m.nearest(p, dist, scanned);
		r.raw("node", node_json(m, id, p));
		if (scanned)
			r.b("full_scan", true);
		if (c.from_actor && g_actor)
		{
			r.i("actor_node", g_actor->ai_location().level_vertex_id());
			r.i("actor_game_vertex", g_actor->ai_location().game_vertex_id());
		}
		if (id != invalid)
		{
			r.num("plane_y", m.plane_y(id, p.x, p.z), 2);
			const CLevelGraph::CVertex* v = c.lg->vertex(id);
			r.raw("cover", jobj().num("high", c.lg->vertex_high_cover(v), 2).num("low", c.lg->vertex_low_cover(v), 2).done());
			u32 links = 0;
			for (int k = 0; k < 4; ++k)
				links += c.lg->valid_vertex_id(v->link(k));
			r.i("links", links);
			const components_t& cc = components(m);
			u32 a = cc.of[id];
			u32 largest = u32(std::max_element(cc.size.begin(), cc.size.end()) - cc.size.begin());
			r.raw("area", jobj().i("id", a).i("nodes", cc.size[a]).b("main", a == largest).i("areas_on_level", cc.size.size()).done());
			u32 gv = game_vertex(id);
			if (gv != invalid)
			{
				jobj g;
				g.i("id", gv);
				const CGameGraph::CVertex* gvx = ai().game_graph().vertex(gv);
				g.vec("position", gvx->level_point()).num("distance", gvx->level_point().distance_to(p), 1);
				r.raw("game_vertex", g.done());
			}
		}
		r.num("cost_ms", t.GetElapsed_sec() * 1000.f, 3);
		out = r.done();
		return true;
	}

	// ------------------------------------------------------------------ grid sampling (around text grid, nav.grid image)

	// Sample the AI map on a w*h grid around origin (top = forward along yaw), cell metres apart. Per sample:
	// 0 = no node, else bit7 = reachable (g_reach), bits0-6 = 1 + height bucket ((y - origin.y) / 0.5 + 63, 0..126)
	void sample_grid(const navmap& m, const Fvector& o, float yaw, float cell, int w, int h, xr_vector<u8>& out)
	{
		out.assign(size_t(w) * h, 0);
		float s = _sin(deg2rad(yaw)), co = _cos(deg2rad(yaw));
		for (int r = 0; r < h; ++r)
			for (int col = 0; col < w; ++col)
			{
				float fwd = ((h - 1) * .5f - r) * cell, right = (col - (w - 1) * .5f) * cell;
				float wx = o.x + s * fwd + co * right, wz = o.z + co * fwd - s * right;
				int ix = m.cx(wx), iz = m.cz(wz);
				u32 f, l;
				if (!m.cell_nodes(ix, iz, f, l))
					continue;
				// prefer a reachable node, then the one closest in height to the origin
				u32 best = invalid;
				float bd = flt_max;
				for (u32 id = f; id < l; ++id)
				{
					float d = _abs(m.plane_y(id, wx, wz) - o.y) - (g_reach.seen(id) ? 1000.f : 0.f);
					if (d < bd)
					{
						bd = d;
						best = id;
					}
				}
				float y = m.plane_y(best, wx, wz);
				int bucket = iFloor((y - o.y) / .5f + 63.5f);
				clamp(bucket, 0, 126);
				out[size_t(r) * w + col] = u8((g_reach.seen(best) ? 0x80 : 0) | (1 + bucket));
			}
	}

	// ------------------------------------------------------------------ nav.around

	bool nav_around(LPCSTR params, xr_string& out)
	{
		CTimer t;
		t.Start();
		ctx c;
		if (!ready(params, c, out))
			return true;
		navmap m(*c.lg);
		float radius = _max(1.f, _min(num_param(params, "radius", 30.f), 200.f));
		int rays = (int)_max<s64>(0, _min<s64>(int_param(params, "rays", 16), 64));
		bool grid = int_param(params, "grid", 1) != 0;
		float gcell = _max(m.cell, _min(num_param(params, "grid_cell", 2.f), 20.f));
		float gradius = _max(gcell, _min(num_param(params, "grid_radius", _min(radius, 30.f)), 200.f));
		float yaw = c.yaw;
		xr_string orient;
		if (mcp::get_string_param(params, "orient", orient) && orient == "north")
			yaw = 0;

		jobj r;
		r.b("available", true).str("level", g_pGameLevel->name().c_str()).vec("origin", c.origin).num("radius", radius, 1);
		u32 start = origin_node(m, c);
		if (start == invalid)
		{
			out = r.b("on_mesh", false).done();
			return true;
		}
		float dy = c.origin.y - m.plane_y(start, c.origin.x, c.origin.z);
		Fvector sp = c.origin;
		if (!c.lg->inside(start, sp))
			sp = m.centre(start); // off the node's cell: walk from its centre
		r.raw("node", node_json(m, start, c.origin)).b("on_mesh", c.lg->inside(start, c.origin) && _abs(dy) <= 1.5f);
		r.num("camera_yaw", c.yaw, 1);

		// rays: bearing relative to the camera yaw (+ = right), same as game_input look
		xr_string ra = "[";
		for (int k = 0; k < rays; ++k)
		{
			float b = wrap180(360.f * k / rays);
			float a = deg2rad(c.yaw + b);
			u32 end;
			float d = m.ray_walk(start, sp, _sin(a), _cos(a), radius, end);
			Fvector ep;
			ep.set(sp.x + _sin(a) * d, 0, sp.z + _cos(a) * d);
			ep.y = m.plane_y(end, ep.x, ep.z);
			jobj e;
			e.num("bearing", b, 1).num("distance", d, 1).b("open", d >= radius - EPS_L).vec("end", ep);
			ra += (k ? "," : "") + e.done();
		}
		r.raw("rays", ra + "]");

		// nodes in radius / reachable from the start node within radius
		u32 in_radius = 0;
		Fbox bb;
		bb.invalidate();
		{
			int x0 = m.cx(c.origin.x - radius), x1 = m.cx(c.origin.x + radius);
			int z0 = m.cz(c.origin.z - radius), z1 = m.cz(c.origin.z + radius);
			float r2 = radius * radius;
			for (int x = _max(0, x0); x <= _min(x1, (int)c.lg->max_x()); ++x)
			{
				u32 a, e2;
				for (int z = _max(0, z0); z <= _min(z1, (int)c.lg->max_z()); ++z)
					if (m.cell_nodes(x, z, a, e2))
						for (u32 id = a; id < e2; ++id)
						{
							Fvector ctr = m.centre(id);
							if (_sqr(ctr.x - c.origin.x) + _sqr(ctr.z - c.origin.z) <= r2)
								in_radius++;
						}
			}
		}
		xr_vector<u32> reached;
		u32 reach = flood(m, start, c.origin, radius, 200000, &reached);
		for (u32 id : reached)
			bb.modify(m.centre(id));
		r.i("nodes_in_radius", in_radius).i("reachable", reach).num("reachable_area_m2", reach * m.cell * m.cell, 0);
		if (reach)
			r.raw("reachable_box", "{\"min\":" + fvec(bb.min) + ",\"max\":" + fvec(bb.max) + "}");

		if (grid)
		{
			int n = 2 * iFloor(gradius / gcell) + 1;
			n = _min(n, 61);
			xr_vector<u8> g;
			sample_grid(m, c.origin, yaw, gcell, n, n, g);
			xr_string rows = "[";
			for (int y = 0; y < n; ++y)
			{
				std::string line(n, ' ');
				for (int x = 0; x < n; ++x)
				{
					u8 v = g[size_t(y) * n + x];
					line[x] = !v ? '#' : (v & 0x80) ? '.' : 'o';
				}
				if (y == n / 2)
					line[n / 2] = '@';
				rows += (y ? "," : "") + js(line.c_str());
			}
			jobj gj;
			gj.num("cell", gcell, 2).i("size", n).str("up", yaw == 0 && orient == "north" ? "north (+z)" : "forward (camera)");
			gj.str("legend", "@ origin, . walkable (reachable), o walkable (not reachable within radius), # no AI node");
			r.raw("grid", gj.raw("rows", rows + "]").done());
		}
		r.num("cost_ms", t.GetElapsed_sec() * 1000.f, 3);
		out = r.done();
		return true;
	}

	// ------------------------------------------------------------------ level path search

	// Fallback A* for searches beyond the graph engine's fixed 65500-vertex storage; same costs as the engine's level
	// path manager (cell size per step, heuristic 2 * cell * manhattan cells)
	bool long_astar(const navmap& m, u32 s, u32 d, u32 cap, float max_ms, xr_vector<u32>& path, u32& visited, bool& truncated,
	                const xr_vector<u32>* avoid = nullptr)
	{
		static xr_vector<u32> parent;
		static xr_vector<float> gcost;
		static stamps st;
		u32 n = m.lg.header().vertex_count();
		if (parent.size() != n)
		{
			parent.assign(n, invalid);
			gcost.assign(n, 0);
		}
		st.begin(n);
		int dx, dz;
		m.lg.unpack_xz(m.lg.vertex(d), dx, dz);
		auto est = [&](u32 id)
		{
			int x, z;
			m.lg.unpack_xz(m.lg.vertex(id), x, z);
			return 2 * m.cell * float(_abs(x - dx) + _abs(z - dz));
		};
		typedef std::pair<float, u32> item;
		std::priority_queue<item, xr_vector<item>, std::greater<item>> open;
		st.set(s);
		gcost[s] = 0;
		parent[s] = invalid;
		open.push({ est(s), s });
		CTimer t;
		t.Start();
		visited = 0;
		truncated = false;
		while (!open.empty())
		{
			item it = open.top();
			open.pop();
			u32 cur = it.second;
			float g = gcost[cur];
			if (it.first > g + est(cur) + EPS_L)
				continue; // stale entry
			if (cur == d)
			{
				path.clear();
				for (u32 v = d; v != invalid; v = parent[v])
					path.push_back(v);
				std::reverse(path.begin(), path.end());
				return true;
			}
			if (++visited >= cap || ((visited & 1023) == 0 && t.GetElapsed_sec() * 1000.f > max_ms))
			{
				truncated = true;
				return false;
			}
			const CLevelGraph::CVertex* v = m.lg.vertex(cur);
			for (int k = 0; k < 4; ++k)
			{
				u32 nb = v->link(k);
				if (!m.lg.valid_vertex_id(nb) || !m.lg.is_accessible(nb))
					continue;
				if (avoid && std::binary_search(avoid->begin(), avoid->end(), nb)) // sorted by mover_plan
					continue;
				float ng = g + m.cell;
				if (st.seen(nb) && gcost[nb] <= ng)
					continue;
				st.set(nb);
				gcost[nb] = ng;
				parent[nb] = cur;
				open.push({ ng + est(nb), nb });
			}
		}
		return false;
	}

	// Corner points of a node path (string pulling with the AI map's straight-line check)
	void corners(const navmap& m, const xr_vector<u32>& path, const Fvector& from, xr_vector<u32>& out_idx)
	{
		out_idx.clear();
		u32 n = (u32)path.size();
		u32 a = 0;
		Fvector2 ap;
		ap.set(from.x, from.z);
		Fvector a3 = from;
		auto visible = [&](u32 k)
		{
			return m.lg.check_vertex_in_direction(path[a], ap, path[k]) && m.clear(a3, m.centre(path[k]), clearance);
		};
		while (a + 1 < n)
		{
			// gallop forward, then binary search the last visible node
			u32 ok = a + 1, step = 1, bad = n;
			while (ok + step < n)
			{
				if (visible(ok + step))
				{
					ok += step;
					step *= 2;
				}
				else
				{
					bad = ok + step;
					break;
				}
			}
			u32 lo = ok, hi = _min(bad, n);
			while (hi - lo > 1)
			{
				u32 mid = (lo + hi) / 2;
				if (visible(mid))
					lo = mid;
				else
					hi = mid;
			}
			a = lo;
			out_idx.push_back(a);
			a3 = m.centre(path[a]);
			ap.set(a3.x, a3.z);
		}
	}

	// a path corner (node centre) moved up to 0.25 m away from the AI map edges around it (stays in its own cell)
	Fvector corner_point(const navmap& m, u32 id)
	{
		Fvector c = m.centre(id), push;
		push.set(0, 0, 0);
		for (int dx = -1; dx <= 1; ++dx)
			for (int dz = -1; dz <= 1; ++dz)
				if ((dx || dz) && m.floor_node(c.x + dx * m.cell, c.y, c.z + dz * m.cell, id) == invalid)
				{
					push.x -= dx * (dz ? .7071f : 1.f);
					push.z -= dz * (dx ? .7071f : 1.f);
				}
		float l = _sqrt(push.x * push.x + push.z * push.z);
		if (l < .1f)
			return c;
		c.x += push.x / l * .25f;
		c.z += push.z / l * .25f;
		c.y = m.plane_y(id, c.x, c.z);
		return c;
	}

	struct path_result
	{
		bool found = false, truncated = false;
		LPCSTR reason = nullptr;
		LPCSTR engine = "graph_engine";
		u32 visited = 0;
		xr_vector<u32> nodes;
		xr_vector<Fvector> points; // from, corners..., to
		float length = 0;
		float ms = 0;
	};

	// level path between two snapped nodes; points start at from and end at to_point; avoid = nodes not to pass (mcp A*)
	void level_path(const navmap& m, u32 s, const Fvector& from, u32 d, const Fvector& to_point, u32 cap, float max_ms,
	                path_result& pr, const xr_vector<u32>* avoid = nullptr)
	{
		CTimer t;
		t.Start();
		const components_t& cc = components(m);
		if (cc.of[s] != cc.of[d])
		{
			pr.reason = "no_route"; // different connected areas of the AI map
			pr.engine = "components";
			pr.ms = t.GetElapsed_sec() * 1000.f;
			return;
		}
		GraphEngineSpace::CBaseParameters params(flt_max, u32(-1), _min(cap, 65500u));
		bool avoiding = avoid && !avoid->empty();
		if (!avoiding)
		{
			pr.found = ai().graph_engine().search(m.lg, s, d, &pr.nodes, params);
			pr.visited = ai().graph_engine().m_algorithm->data_storage().get_visited_node_count();
		}
		if (avoiding || (!pr.found && pr.visited >= params.max_visited_node_count && cap > 65500))
		{
			pr.engine = "mcp_astar";
			pr.found = long_astar(m, s, d, cap, max_ms, pr.nodes, pr.visited, pr.truncated, avoid);
		}
		else if (!pr.found && pr.visited >= params.max_visited_node_count)
			pr.truncated = true;
		if (!pr.found)
			pr.reason = pr.truncated ? "search_limit" : "no_route";
		if (pr.found)
		{
			xr_vector<u32> idx;
			corners(m, pr.nodes, from, idx);
			pr.points.push_back(from);
			for (u32 k : idx)
				pr.points.push_back(k + 1 == pr.nodes.size() ? to_point : corner_point(m, pr.nodes[k]));
			if (idx.empty())
				pr.points.push_back(to_point);
			for (size_t k = 1; k < pr.points.size(); ++k)
				pr.length += pr.points[k - 1].distance_to(pr.points[k]);
		}
		pr.ms = t.GetElapsed_sec() * 1000.f;
	}

	xr_string waypoints_json(const xr_vector<Fvector>& pts, float cam_yaw, u32 cap, u32& omitted)
	{
		xr_string a = "[";
		float prev_h = 0;
		omitted = 0;
		for (size_t k = 1; k < pts.size(); ++k)
		{
			if (k > cap)
			{
				omitted = u32(pts.size() - 1 - cap);
				break;
			}
			float h = heading(pts[k - 1], pts[k]);
			jobj e;
			e.vec("position", pts[k]).num("distance", pts[k - 1].distance_to(pts[k]), 1).num("heading", h, 1);
			e.num("bearing", wrap180(h - cam_yaw), 1);
			if (k > 1)
				e.num("turn", wrap180(h - prev_h), 1);
			prev_h = h;
			a += (k > 1 ? "," : "") + e.done();
		}
		return a + "]";
	}

	// ------------------------------------------------------------------ overlay state (drawn in mcp_nav_render)

	struct overlay_t
	{
		bool on = false;
		float radius = 20.f;
		xr_vector<Fvector> path, markers;
		u32 client = 0;
		// cache
		const CLevelGraph* lg = nullptr;
		shared_str level;
		Fvector centre;
		float cached_radius = 0;
		u32 start = invalid;
		xr_vector<u32> nodes; // ascending ids
		xr_vector<u8> reach;
		xr_vector<u8> vis; // 0 unknown, 1 visible from the camera, 2 hidden behind level geometry
		Fvector vis_eye;
		u32 vis_next = 0;
		float build_ms = 0, vis_ms = 0;
		u32 frames = 0;
	};
	overlay_t g_ov;

	xr_string overlay_json()
	{
		jobj o;
		o.b("on", g_ov.on).num("radius", g_ov.radius, 1).i("path_points", g_ov.path.size()).i("markers", g_ov.markers.size());
		u32 hidden = 0;
		for (u8 v : g_ov.vis)
			hidden += v == 2;
		o.i("nodes", g_ov.on ? g_ov.nodes.size() : 0).i("hidden", hidden).i("visibility_pending", g_ov.nodes.size() - _min((u32)g_ov.nodes.size(), g_ov.vis_next));
		o.num("build_ms", g_ov.build_ms, 3).num("visibility_ms", g_ov.vis_ms, 3);
		return o.done();
	}

	// ------------------------------------------------------------------ nav.path

	bool nav_path(LPCSTR params, xr_string& out)
	{
		CTimer t;
		t.Start();
		ctx c;
		if (!ready(params, c, out, "from"))
			return true;
		navmap m(*c.lg);
		u32 cap = (u32)_max<s64>(1000, _min<s64>(int_param(params, "max_nodes", 400000), 4000000));
		float max_ms = _max(5.f, _min(num_param(params, "max_ms", 200.f), 2000.f));
		u32 max_wp = (u32)_max<s64>(1, _min<s64>(int_param(params, "max_waypoints", 64), 500));
		xr_string to_level;
		mcp::get_string_param(params, "to_level", to_level);
		Fvector to;
		int has_to = vec_param(params, "to", to);
		if (has_to < 0)
		{
			out = "to must be \"x,y,z\"";
			return false;
		}
		bool cross = to_level.size() && to_level != g_pGameLevel->name().c_str();
		if (!has_to && !cross)
		{
			out = "to (\"x,y,z\") or to_level required";
			return false;
		}
		jobj r;
		r.b("available", true).str("level", g_pGameLevel->name().c_str());

		// start
		u32 s;
		Fvector from = c.origin;
		if (c.from_actor)
			s = origin_node(m, c);
		else
		{
			snap_t sf = snap(m, c.origin);
			s = sf.id;
			if (!sf.on_mesh)
				r.num("from_snapped", sf.dist, 2);
			from = sf.on_mesh ? c.origin : sf.point;
		}
		r.vec("from", from);

		if (cross)
		{
			CGameGraph* gg = ai().get_game_graph();
			const GameGraph::SLevel* lv = gg ? gg->header().level(to_level.c_str(), true) : nullptr;
			if (!lv)
			{
				out = "unknown level '" + to_level + "'";
				return false;
			}
			u32 sg = c.from_actor && g_actor ? g_actor->ai_location().game_vertex_id() : game_vertex(s);
			if (!gg->valid_vertex_id(sg))
				sg = game_vertex(s);
			xr_vector<u32> gpath;
			bool ok;
			if (has_to)
			{
				u32 dg;
				float dd;
				ok = gg->nearest_vertex(lv->id(), to, dg, dd);
				if (ok)
				{
					GraphEngineSpace::CBaseParameters gp(flt_max, u32(-1), u32(-1));
					ok = ai().graph_engine().search(*gg, sg, dg, &gpath, gp);
				}
			}
			else
			{
				GraphEngineSpace::CGameLevelParams lp(lv->id(), flt_max);
				ok = ai().graph_engine().search(*gg, sg, u32(GameGraph::_GRAPH_ID(-1)), &gpath, lp);
			}
			r.str("to_level", to_level.c_str()).b("found", ok).i("game_vertices", gpath.size());
			if (ok && gpath.size())
			{
				// group consecutive vertices by level: [{level, enter, exit}], with the exit / next entry points
				xr_string legs = "[";
				u32 nl = 0;
				float total = 0;
				for (size_t k = 0; k < gpath.size();)
				{
					u32 lid = gg->vertex(gpath[k])->level_id();
					size_t e = k;
					while (e + 1 < gpath.size() && gg->vertex(gpath[e + 1])->level_id() == lid)
					{
						total += gg->vertex(gpath[e])->game_point().distance_to(gg->vertex(gpath[e + 1])->game_point());
						++e;
					}
					jobj l;
					l.str("level", level_name(lid)).i("enter_game_vertex", gpath[k]).vec("enter", gg->vertex(gpath[k])->level_point());
					l.i("exit_game_vertex", gpath[e]).vec("exit", gg->vertex(gpath[e])->level_point()).i("vertices", e - k + 1);
					legs += (nl++ ? "," : "") + l.done();
					k = e + 1;
				}
				r.raw("legs", legs + "]").num("graph_distance", total, 0);
				// on this level: path from the start to the first exit
				const CGameGraph::CVertex* ex = nullptr;
				u32 next_gv = invalid;
				for (size_t k = 0; k + 1 < gpath.size(); ++k)
					if (gg->vertex(gpath[k + 1])->level_id() != c.lg->level_id())
					{
						ex = gg->vertex(gpath[k]);
						next_gv = gpath[k + 1];
						break;
					}
				if (ex && gg->vertex(gpath[0])->level_id() == c.lg->level_id())
				{
					// the level changer leading there: its destination vertex is the next leg's first vertex, else the
					// changer to that level whose destination is closest to that vertex
					Fvector target = ex->level_point();
					CGameObject* best = nullptr;
					CSE_ALifeLevelChanger* best_se = nullptr;
					bool exact = false;
					float bd = flt_max;
					LPCSTR next_level = level_name(gg->vertex(next_gv)->level_id());
					for (u32 i = 0, n = Level().Objects.o_count(); i < n && ai().get_alife(); ++i)
					{
						CObject* o = Level().Objects.o_get_by_iterator(i);
						if (!o || o->getDestroy() || !smart_cast<CLevelChanger*>(o))
							continue;
						CSE_ALifeLevelChanger* se = smart_cast<CSE_ALifeLevelChanger*>(ai().alife().objects().object(o->ID(), true));
						if (!se)
							continue;
						bool match = se->m_tNextGraphID == next_gv;
						if (!match && (exact || xr_strcmp(se->m_caLevelToChange, next_level)))
							continue;
						float d = se->m_tNextPosition.distance_to(gg->vertex(next_gv)->level_point());
						if ((match && !exact) || d < bd)
						{
							bd = d;
							best = smart_cast<CGameObject*>(o);
							best_se = se;
							exact = match;
						}
					}
					if (best)
					{
						jobj lc;
						lc.i("id", best->ID()).str("name", best->cName().c_str()).vec("position", best->Position());
						lc.num("distance", best->Position().distance_to(c.origin), 1).str("match", exact ? "destination_vertex" : "destination_nearby");
						lc.i("dest_game_vertex", best_se->m_tNextGraphID).vec("dest_position", best_se->m_tNextPosition).num("dest_offset", bd, 1);
						r.raw("level_changer", lc.done());
						target = best->Position();
					}
					snap_t sx = snap(m, target);
					path_result pr;
					level_path(m, s, from, sx.id, sx.point, cap, max_ms, pr);
					jobj lp;
					lp.vec("to", sx.point).b("found", pr.found).num("length", pr.length, 1).i("nodes", pr.nodes.size());
					if (pr.reason)
						lp.str("reason", pr.reason);
					u32 om;
					lp.raw("waypoints", waypoints_json(pr.points, c.yaw, max_wp, om));
					if (om)
						lp.i("omitted", om);
					lp.num("ms", pr.ms, 2);
					r.raw("to_exit", lp.done());
				}
			}
			r.num("cost_ms", t.GetElapsed_sec() * 1000.f, 3);
			out = r.done();
			return true;
		}

		snap_t st = snap(m, to);
		r.vec("to", to);
		if (!st.on_mesh)
			r.raw("to_snapped", jobj().vec("point", st.point).num("distance", st.dist, 2).done());
		r.num("straight", from.distance_to(st.on_mesh ? to : st.point), 1);
		if (s == invalid || st.id == invalid)
		{
			out = r.b("found", false).str("reason", "off_mesh").done();
			return true;
		}
		path_result pr;
		level_path(m, s, from, st.id, st.point, cap, max_ms, pr);
		r.b("found", pr.found).str("engine", pr.engine).i("visited", pr.visited);
		if (pr.reason)
			r.str("reason", pr.reason);
		if (pr.truncated)
			r.b("truncated", true);
		if (pr.found)
		{
			r.num("length", pr.length, 1).i("nodes", pr.nodes.size());
			u32 om;
			r.raw("waypoints", waypoints_json(pr.points, c.yaw, max_wp, om));
			if (om)
				r.i("omitted", om);
		}
		else if (!xr_strcmp(pr.engine, "components"))
		{
			// where it fails: the node of the start's area closest to the target; sizes of both areas
			const components_t& cc = components(m);
			float bd;
			bool sc;
			u32 best = m.nearest(st.point, bd, sc, cc.of.data(), cc.of[s]);
			if (best != invalid)
				r.raw("closest_reachable", jobj().vec("position", m.centre(best)).num("distance_to_target", bd, 1).done());
			r.i("start_area_nodes", cc.size[cc.of[s]]).i("target_area_nodes", cc.size[cc.of[st.id]]);
		}
		r.num("components_ms", g_comp.build_ms, 1);
		if (int_param(params, "overlay", 0) && pr.found)
		{
			g_ov.path = pr.points;
			g_ov.markers.assign(1, st.point);
		}
		r.num("search_ms", pr.ms, 2).num("cost_ms", t.GetElapsed_sec() * 1000.f, 3);
		out = r.done();
		return true;
	}

	// ------------------------------------------------------------------ nav.nearest

	bool nav_nearest(LPCSTR params, xr_string& out)
	{
		CTimer t;
		t.Start();
		ctx c;
		if (!ready(params, c, out))
			return true;
		navmap m(*c.lg);
		snap_t s = snap(m, c.origin);
		jobj r;
		r.b("available", true).vec("point", c.origin).b("on_mesh", s.on_mesh);
		if (s.id == invalid)
		{
			out = r.raw("nearest", "null").done();
			return true;
		}
		r.raw("nearest", jobj().i("id", s.id).vec("position", s.point).num("distance", s.on_mesh ? _abs(s.dy) : s.dist, 2).done());
		float radius = num_param(params, "random_radius", 0.f);
		if (radius > 0)
		{
			radius = _min(radius, 500.f);
			xr_vector<u32> q;
			u32 n = flood(m, s.id, s.point, radius, 400000, &q);
			u32 count = _max<u32>(1, (u32)_max<s64>(1, _min<s64>(int_param(params, "count", 1), 50)));
			xr_string a = "[";
			for (u32 k = 0; k < count && n; ++k)
			{
				u32 id = q[::Random.randI(n)];
				a += (k ? "," : "") + fvec(m.centre(id));
			}
			r.raw("random", a + "]").i("reachable_in_radius", n);
		}
		r.num("cost_ms", t.GetElapsed_sec() * 1000.f, 3);
		out = r.done();
		return true;
	}

	// ------------------------------------------------------------------ nav.grid (top-down image data)

	xr_string base64(const u8* d, size_t n)
	{
		static const char tb[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
		xr_string s;
		s.reserve((n + 2) / 3 * 4);
		for (size_t i = 0; i < n; i += 3)
		{
			u32 v = u32(d[i]) << 16 | (i + 1 < n ? u32(d[i + 1]) << 8 : 0) | (i + 2 < n ? u32(d[i + 2]) : 0);
			s += tb[v >> 18 & 63];
			s += tb[v >> 12 & 63];
			s += i + 1 < n ? tb[v >> 6 & 63] : '=';
			s += i + 2 < n ? tb[v & 63] : '=';
		}
		return s;
	}

	bool nav_grid(LPCSTR params, xr_string& out)
	{
		CTimer t;
		t.Start();
		ctx c;
		if (!ready(params, c, out))
			return true;
		navmap m(*c.lg);
		float radius = _max(5.f, _min(num_param(params, "radius", 40.f), 1000.f));
		float cell = num_param(params, "cell", 0.f);
		int maxpx = (int)_max<s64>(32, _min<s64>(int_param(params, "max_px", 400), 1024));
		if (cell <= 0)
			cell = m.cell;
		cell = _max(cell, 2 * radius / maxpx);
		int n = _min(2 * iFloor(radius / cell) + 1, maxpx);
		xr_string orient;
		float yaw = mcp::get_string_param(params, "orient", orient) && orient == "north" ? 0.f : c.yaw;
		u32 start = origin_node(m, c);
		u32 reach = flood(m, start, c.origin, radius * 1.415f, 600000);
		xr_vector<u8> g;
		sample_grid(m, c.origin, yaw, cell, n, n, g);
		jobj r;
		r.b("available", true).str("level", g_pGameLevel->name().c_str()).vec("origin", c.origin).num("yaw", yaw, 2);
		r.num("camera_yaw", c.yaw, 2).num("cell", cell, 3).i("width", n).i("height", n).i("reachable", reach);
		r.str("encoding", "u8 per cell, rows top (forward) to bottom: 0 none; bit7 reachable; bits0-6 = 1 + clamp((y-origin.y)/0.5+63, 0, 126)");
		r.raw("data", "\"" + base64(g.data(), g.size()) + "\"");
		r.num("cost_ms", t.GetElapsed_sec() * 1000.f, 3);
		out = r.done();
		return true;
	}

	// ------------------------------------------------------------------ nav.overlay

	bool nav_overlay(LPCSTR params, xr_string& out)
	{
		s64 on;
		if (mcp::get_int_param(params, "on", on))
		{
			g_ov.on = on != 0;
			g_ov.lg = nullptr; // rebuild
		}
		float rad = num_param(params, "radius", -1.f);
		if (rad > 0)
		{
			g_ov.radius = _max(2.f, _min(rad, 60.f));
			g_ov.lg = nullptr;
		}
		if (int_param(params, "clear", 0))
		{
			g_ov.path.clear();
			g_ov.markers.clear();
		}
		vec_list_param(params, "path", g_ov.path, 2000);
		vec_list_param(params, "markers", g_ov.markers, 64);
		g_ov.client = mcp::client_id();
		out = jobj().raw("overlay", overlay_json()).done();
		return true;
	}

	// ------------------------------------------------------------------ overlay drawing

	void overlay_build(const Fvector& centre)
	{
		CTimer t;
		t.Start();
		const CLevelGraph& lg = ai().level_graph();
		navmap m(lg);
		if (g_ov.level.size() && g_ov.level != g_pGameLevel->name())
		{
			g_ov.path.clear(); // points of another level
			g_ov.markers.clear();
			g_ov.nodes.clear();
		}
		g_ov.lg = &lg;
		g_ov.level = g_pGameLevel->name();
		g_ov.centre = centre;
		g_ov.cached_radius = g_ov.radius;
		xr_vector<u32> old_nodes;
		xr_vector<u8> old_vis;
		old_nodes.swap(g_ov.nodes);
		old_vis.swap(g_ov.vis);
		g_ov.reach.clear();
		float r = g_ov.radius, r2 = r * r;
		int x0 = _max(0, m.cx(centre.x - r)), x1 = _min(m.cx(centre.x + r), (int)lg.max_x());
		int z0 = _max(0, m.cz(centre.z - r)), z1 = _min(m.cz(centre.z + r), (int)lg.max_z());
		for (int x = x0; x <= x1; ++x)
			for (int z = z0; z <= z1; ++z)
			{
				u32 a, e;
				if (!m.cell_nodes(x, z, a, e))
					continue;
				for (u32 id = a; id < e; ++id)
				{
					Fvector p = m.centre(id);
					if (_sqr(p.x - centre.x) + _sqr(p.z - centre.z) <= r2 && _abs(p.y - centre.y) < 25.f)
						g_ov.nodes.push_back(id);
				}
			}
		// keep the known visibility of nodes that stay in range (no flicker while walking)
		g_ov.vis.assign(g_ov.nodes.size(), 0);
		for (size_t k = 0; k < g_ov.nodes.size(); ++k)
		{
			auto it = std::lower_bound(old_nodes.begin(), old_nodes.end(), g_ov.nodes[k]);
			if (it != old_nodes.end() && *it == g_ov.nodes[k] && size_t(it - old_nodes.begin()) < old_vis.size())
				g_ov.vis[k] = old_vis[it - old_nodes.begin()];
		}
		g_ov.vis_next = 0;
		u32 start = g_actor ? g_actor->ai_location().level_vertex_id() : invalid;
		if (!lg.valid_vertex_id(start))
		{
			float dy;
			start = m.node_at(centre, &dy);
		}
		g_ov.start = start;
		flood(m, start, centre, r + m.cell, 100000);
		g_ov.reach.resize(g_ov.nodes.size());
		for (size_t k = 0; k < g_ov.nodes.size(); ++k)
			g_ov.reach[k] = g_reach.seen(g_ov.nodes[k]);
		g_ov.build_ms = t.GetElapsed_sec() * 1000.f;
	}

	// Frame hook (seqFrame, main thread, no worker running): node list + visibility rays, so rendering only draws
	void overlay_update()
	{
		if (!g_ov.on)
			return;
		if (g_ov.client != mcp::client_id())
		{
			g_ov = overlay_t(); // supervisor gone or replaced: overlay off
			return;
		}
		if ((pApp && pApp->IsLoading()) || !g_pGameLevel || !g_pGameLevel->name().size() || !g_ai_space || !ai().get_level_graph())
			return;
		const CLevelGraph& lg = ai().level_graph();
		Fvector centre = g_actor ? g_actor->Position() : Device.vCameraPosition;
		if (g_ov.lg != &lg || g_ov.level != g_pGameLevel->name() || g_ov.cached_radius != g_ov.radius ||
			centre.distance_to_xz(g_ov.centre) > 1.f || _abs(centre.y - g_ov.centre.y) > 2.f ||
			(g_actor && g_actor->ai_location().level_vertex_id() != g_ov.start && ++g_ov.frames % 15 == 0))
			overlay_build(centre);

		// static-geometry rays from the camera, a budget per frame; restart when the camera moved
		CTimer t;
		t.Start();
		const Fvector& eye = Device.vCameraPosition;
		if (eye.distance_to(g_ov.vis_eye) > .3f)
		{
			g_ov.vis_eye = eye;
			g_ov.vis_next = 0;
		}
		navmap m(lg);
		for (u32 n = 0; n < 400 && g_ov.vis_next < g_ov.nodes.size(); ++n, ++g_ov.vis_next)
		{
			Fvector p = m.centre(g_ov.nodes[g_ov.vis_next]);
			p.y += .25f;
			Fvector d;
			d.sub(p, eye);
			float len = d.magnitude();
			if (len < 1.f)
			{
				g_ov.vis[g_ov.vis_next] = 1;
				continue;
			}
			d.div(len);
			bool hit = !!Level().ObjectSpace.RayTest(eye, d, len - .3f, collide::rqtStatic, nullptr, g_actor);
			g_ov.vis[g_ov.vis_next] = hit ? 2 : 1;
		}
		g_ov.vis_ms = t.GetElapsed_sec() * 1000.f;
	}

	// ------------------------------------------------------------------ move_to: the actor walks a nav path by injected
	// input (camera turn + held forward key), ticked by mcp_input in input frames (so MCP pause / steps apply)

	// a door met on the way (or the nav.use target)
	struct door_rec
	{
		u16 id = 0;
		xr_string name, section, before, after, info, pick;
		LPCSTR action = "none", reason = nullptr;
		Fvector pos, gate, stand; // gate = leaf centre while shut (the doorway); stand = where to open it from
		u32 presses = 0, check_t = 0;
		bool done = false; // opened or given up: not acted on again in this move
		bool was_locked = false;
	};

	struct mover_t
	{
		u32 id = 0;
		bool active = false;
		LPCSTR state = "idle"; // running, reached, stuck, timeout, aborted, no_path, blocked_door; nav.use: used, failed
		LPCSTR reason = nullptr;
		u16 target_id = u16(-1);
		Fvector target, start, last_pos, check_pos;
		float stop = 1.f, travelled = 0, path_length = 0;
		u32 timeout_ms = 60000, elapsed = 0, frames = 0;
		bool sprint = false, fwd_down = false, sprint_tap = false, jump_tap = false;
		xr_vector<Fvector> path;
		u32 wp = 0;
		shared_str level;
		int fwd = 0, sprint_dik = 0, jump_dik = 0, use_dik = 0, tap_dik = 0, back_dik = 0;
		bool back_down = false;
		u32 check_t = 0, fwd_ms = 0, stuck = 0, stuck_total = 0, replans = 0, sprint_t = 0, replan_t = 0;
		Fvector stuck_at;
		xr_vector<door_rec> doors;
		xr_vector<u16> cand;   // doors near the planned path
		xr_vector<u32> avoid;  // AI nodes at doors that did not open: replans go around them
		bool use_only = false; // nav.use: aim at the target and press use, no walking
		bool level_pitch = false;
		bool via = false;      // walk through the opened doorway first (the open leaf may stand beside the path)
		Fvector via_pt;
		int act = -1;          // doors[] index being handled
		int phase = 0;         // 1 approach, 2 aim, 3 watch after the use press
		u32 phase_t = 0, poll_t = 0, aim_try = 0, aim_ok = 0, aim_bad = 0;
		Fvector phase_pos;
		u16 blocked = u16(-1);
		Fvector goal;          // the target snapped to the AI map (the path's end)
		bool goal_on = true;   // the target itself is on the map (else the walk ends at goal)
		bool end_replan = false;
		bool no_skip = false;  // walk to the next corner (no skip-ahead): after a stuck / doorway replan
		u32 idle = 0;          // stuck checks without progress while not holding forward (turning in place)
		u32 stuck_rep = 0;     // stuck replans in a row within 1 m of rep_at
		Fvector rep_at = { flt_max, flt_max, flt_max };
		u32 ray_t = 0;
		u16 ray_id = u16(-1);  // object hit by the look-ahead rays at the last cast
		xr_vector<u16> obst;   // obstacles already avoided
		xr_string blocker;     // last obstacle seen (json)
		xr_vector<xr_string> tried; // recovery steps taken
		double tick_us = 0;
		float tick_max = 0;
	};
	mover_t g_mv;

	const u32 aim_points = 4, max_presses = 2;
	const float use_range = 2.f; // CActor::UpdateCL: the use target is the HUD pick object under 2 m

	void mover_release()
	{
		if (g_mv.fwd_down && mcp_input::is_held(g_mv.fwd))
			mcp_input::hold_key(g_mv.fwd, false);
		if (g_mv.sprint_tap && mcp_input::is_held(g_mv.sprint_dik))
			mcp_input::hold_key(g_mv.sprint_dik, false);
		if (g_mv.jump_tap && mcp_input::is_held(g_mv.tap_dik))
			mcp_input::hold_key(g_mv.tap_dik, false);
		if (g_mv.back_down && mcp_input::is_held(g_mv.back_dik))
			mcp_input::hold_key(g_mv.back_dik, false);
		g_mv.fwd_down = g_mv.sprint_tap = g_mv.jump_tap = g_mv.back_down = false;
	}

	void mover_finish(LPCSTR state, LPCSTR reason = nullptr)
	{
		mover_release();
		g_mv.active = false;
		g_mv.state = state;
		g_mv.reason = reason;
	}

	void path_doors();

	// path from the actor to the target; false when there is none (reason set)
	bool mover_plan(LPCSTR& reason)
	{
		navmap m(ai().level_graph());
		ctx c;
		c.from_actor = true;
		c.origin = g_actor->Position();
		u32 s = origin_node(m, c);
		snap_t st = snap(m, g_mv.target);
		if (s == invalid || st.id == invalid)
		{
			reason = "off_mesh";
			return false;
		}
		if (_abs(st.point.y - g_mv.target.y) >= 2.4f)
		{
			reason = "target_floor_off_mesh"; // no AI node on the target's floor: "reached" (|dy| < 2.5) is impossible
			return false;
		}
		g_mv.goal = st.point;
		g_mv.goal_on = st.on_mesh || st.point.distance_to_xz(g_mv.target) < .35f;
		std::sort(g_mv.avoid.begin(), g_mv.avoid.end());
		g_mv.avoid.erase(std::unique(g_mv.avoid.begin(), g_mv.avoid.end()), g_mv.avoid.end());
		path_result pr;
		level_path(m, s, c.origin, st.id, st.point, 400000, 200.f, pr, &g_mv.avoid);
		if (!pr.found)
		{
			reason = pr.reason;
			return false;
		}
		g_mv.path = pr.points;
		g_mv.path_length = pr.length;
		g_mv.wp = 1;
		g_mv.replan_t = g_mv.elapsed;
		if (g_ov.on)
		{
			g_ov.path = pr.points;
			g_ov.markers.assign(1, g_mv.target);
		}
		path_doors();
		return true;
	}

	void plan_failed(LPCSTR why) { mover_finish(xr_strcmp(why, "target_floor_off_mesh") ? "no_path" : "unreachable", why); }

	void mover_tried(LPCSTR step, u16 id = u16(-1))
	{
		if (g_mv.tried.size() < 24)
			g_mv.tried.push_back(id == u16(-1) ? xr_string(step) : xr_string(step) + ":" + std::to_string(id).c_str());
	}

	CGameObject* online(u16 id)
	{
		CObject* o = Level().Objects.net_Find(id);
		return o && !o->getDestroy() ? smart_cast<CGameObject*>(o) : nullptr;
	}

	bool is_shut(LPCSTR st) { return !xr_strcmp(st, "closed") || !xr_strcmp(st, "closing") || !xr_strcmp(st, "locked"); }

	int door_index(u16 id)
	{
		for (u32 k = 0; k < g_mv.doors.size(); ++k)
			if (g_mv.doors[k].id == id)
				return (int)k;
		return -1;
	}

	// record for o (added on first sight with the state seen then)
	door_rec& door_note(CGameObject* o, LPCSTR state, const xr_string& info)
	{
		int k = door_index(o->ID());
		if (k < 0)
		{
			door_rec r;
			r.id = o->ID();
			r.name = o->cName().c_str();
			r.section = o->cNameSect_str();
			r.pos = o->Position();
			r.before = state;
			g_mv.doors.push_back(r);
			k = (int)g_mv.doors.size() - 1;
		}
		door_rec& r = g_mv.doors[k];
		r.after = state;
		r.info = info;
		r.check_t = g_mv.elapsed;
		return r;
	}

	float camera_pitch() { return rad2deg(asinf(_max(-1.f, _min(1.f, Device.vCameraDirection.y)))); }

	// turn the camera toward p (yaw and pitch, 720 deg/s); true once within 1 degree
	bool look_toward(const Fvector& p, u32 dt)
	{
		const Fvector& eye = Device.vCameraPosition;
		Fvector d;
		d.sub(p, eye);
		float dy = wrap180(heading(eye, p) - camera_yaw());
		float dp = rad2deg(atan2f(d.y, _sqrt(d.x * d.x + d.z * d.z))) - camera_pitch();
		float mx = 720.f * dt / 1000.f;
		float ty = _max(-mx, _min(dy, mx)), tp = _max(-mx, _min(dp, mx));
		CCameraBase* cam = g_actor->cam_Active();
		if (_abs(ty) > .01f)
			cam->Move(ty > 0 ? kRIGHT : kLEFT, deg2rad(_abs(ty)));
		if (_abs(tp) > .01f)
			cam->Move(tp > 0 ? kDOWN : kUP, deg2rad(_abs(tp))); // kDOWN looks up
		return _abs(dy) < 1.f && _abs(dp) < 1.f;
	}

	// where the pick ray should hit o: bbox centre (a living NPC: chest), then points above / below it
	Fvector aim_point(CGameObject* o, u32 k)
	{
		static const float dy[aim_points] = { 0.f, .3f, -.3f, .6f };
		Fvector p;
		CEntityAlive* ea = smart_cast<CEntityAlive*>(o);
		if (ea && ea->g_Alive() && smart_cast<CInventoryOwner*>(o))
			p.set(o->Position()).y += 1.3f;
		else
			o->Center(p);
		p.y += dy[k];
		return p;
	}

	float seg_dist_xz(const Fvector& p, const Fvector& a, const Fvector& b)
	{
		float abx = b.x - a.x, abz = b.z - a.z, apx = p.x - a.x, apz = p.z - a.z;
		float l = abx * abx + abz * abz;
		float t = l > EPS ? _max(0.f, _min(1.f, (apx * abx + apz * abz) / l)) : 0.f;
		return _sqrt(_sqr(apx - abx * t) + _sqr(apz - abz * t));
	}

	// a door one walks through: leaf (2 * hinge-to-centre) >= 0.7 m; cabinet / table doors are ~0.5 m
	bool passage_door(CGameObject* o)
	{
		if (!mcp_world_is_door(o))
			return false;
		Fvector c;
		o->Center(c);
		return c.distance_to_xz(o->Position()) >= .35f;
	}

	bool segs_cross_xz(const Fvector& a, const Fvector& b, const Fvector& c, const Fvector& d)
	{
		auto side = [](const Fvector& p, const Fvector& q, const Fvector& r) { return (q.x - p.x) * (r.z - p.z) - (q.z - p.z) * (r.x - p.x); };
		return side(a, b, c) * side(a, b, d) < 0.f && side(c, d, a) * side(c, d, b) < 0.f;
	}

	// the rest of the path (from the actor) crosses the door's current leaf (hinge to tip, 0.1 m longer each way)
	bool path_crosses(CGameObject* o, const Fvector& pos)
	{
		Fvector h = o->Position(), c, dir;
		o->Center(c);
		dir.set(c.x - h.x, 0, c.z - h.z);
		float l = dir.magnitude();
		if (l < EPS_L)
			return false;
		dir.div(l);
		Fvector a = Fvector(h).mad(dir, -.1f), b = Fvector(h).mad(dir, 2.f * l + .1f);
		Fvector from = pos;
		for (u32 k = g_mv.wp; k < g_mv.path.size(); from = g_mv.path[k++])
			if (segs_cross_xz(from, g_mv.path[k], a, b))
				return true;
		return false;
	}

	// passage doors whose centre is within 1.2 m of the planned path
	void path_doors()
	{
		g_mv.cand.clear();
		CObjectList& list = Level().Objects;
		for (u32 n = 0, cnt = list.o_count(); n < cnt; n++)
		{
			CObject* ob = list.o_get_by_iterator(n);
			if (!ob || ob->getDestroy() || ob->H_Parent())
				continue;
			CGameObject* o = smart_cast<CGameObject*>(ob);
			if (!passage_door(o))
				continue;
			Fvector c;
			o->Center(c);
			for (size_t k = 1; k < g_mv.path.size(); ++k)
			{
				const Fvector &a = g_mv.path[k - 1], &b = g_mv.path[k];
				if (seg_dist_xz(c, a, b) < 1.2f && c.y > _min(a.y, b.y) - 1.f && c.y < _max(a.y, b.y) + 3.f)
				{
					g_mv.cand.push_back(o->ID());
					break;
				}
			}
		}
	}

	void door_phase(int k, int phase)
	{
		door_rec& r = g_mv.doors[k];
		CGameObject* o = online(r.id);
		if (g_mv.act != k && o)
		{
			// stand on the doorway's normal, on the actor's side, outside the leaf's swing (hinge = object position,
			// leaf length = 2 * hinge-to-centre) but within use range
			Fvector a = g_actor->Position(), h = o->Position();
			o->Center(r.gate);
			float sx = r.gate.x - h.x, sz = r.gate.z - h.z, l = _sqrt(sx * sx + sz * sz);
			r.stand = a;
			if (l > .05f)
			{
				float nx = -sz / l, nz = sx / l;
				if (nx * (a.x - r.gate.x) + nz * (a.z - r.gate.z) < 0.f)
					nx = -nx, nz = -nz;
				float d = _min(1.8f, _max(1.2f, _sqrt(_max(0.f, _sqr(2.f * l + .5f) - l * l))));
				r.stand.set(r.gate.x + nx * d, a.y, r.gate.z + nz * d);
			}
		}
		g_mv.act = k;
		g_mv.phase = phase;
		g_mv.phase_t = g_mv.poll_t = g_mv.elapsed;
		g_mv.phase_pos = g_actor->Position();
		g_mv.aim_try = g_mv.aim_ok = g_mv.aim_bad = 0;
	}

	// back to walking (stuck detection restarts)
	void door_end()
	{
		if (g_mv.back_down)
		{
			mcp_input::hold_key(g_mv.back_dik, false);
			g_mv.back_down = false;
		}
		g_mv.act = -1;
		g_mv.phase = 0;
		g_mv.poll_t = g_mv.elapsed;
		g_mv.level_pitch = true;
		g_mv.check_t = g_mv.elapsed;
		g_mv.check_pos = g_actor->Position();
		g_mv.fwd_ms = 0;
	}

	// the door did not open: walk around it (its AI nodes avoided) if another route exists, else blocked_door
	void door_fail(door_rec& r, LPCSTR reason)
	{
		r.reason = reason;
		r.done = true;
		if (g_mv.use_only)
			return mover_finish("failed", reason);
		navmap m(ai().level_graph());
		Fvector c = r.pos;
		if (CGameObject* o = online(r.id))
			o->Center(c);
		int x0 = m.cx(c.x), z0 = m.cz(c.z);
		for (int x = x0 - 2; x <= x0 + 2; ++x)
			for (int z = z0 - 2; z <= z0 + 2; ++z)
			{
				u32 f, l;
				if (m.cell_nodes(x, z, f, l))
					for (u32 id = f; id < l; ++id)
					{
						Fvector p = m.centre(id);
						if (p.distance_to_xz(c) < 1.f + m.cell * .5f && _abs(p.y - c.y) < 2.5f)
							g_mv.avoid.push_back(id);
					}
			}
		g_mv.replans++;
		LPCSTR why;
		if (mover_plan(why))
		{
			r.action = "avoided";
			return door_end();
		}
		g_mv.blocked = r.id;
		mover_finish("blocked_door", reason);
	}

	// a door on the path within reach ahead: shut -> approach it to open; open ones are only recorded (no use: it would
	// close them)
	void door_scan(const Fvector& pos, const Fvector& aim)
	{
		for (u16 id : g_mv.cand)
		{
			CGameObject* o = online(id);
			if (!o)
				continue;
			Fvector c;
			o->Center(c);
			float d = pos.distance_to_xz(c);
			if (d > 2.4f || c.y < pos.y - 1.f || c.y > pos.y + 3.f)
				continue;
			int k = door_index(id);
			if (k >= 0 && (g_mv.doors[k].done || g_mv.elapsed - g_mv.doors[k].check_t < 250))
				continue;
			if (d > .7f && (c.x - pos.x) * (aim.x - pos.x) + (c.z - pos.z) * (aim.z - pos.z) <= 0.f)
				continue; // behind
			xr_string info;
			bool usable;
			LPCSTR st = mcp_door_state(o, &info, &usable);
			bool shut = is_shut(st);
			if (shut && !path_crosses(o, pos))
				continue; // a shut door beside the path
			door_rec& r = door_note(o, st, info);
			if (!shut)
				continue;
			k = door_index(r.id);
			if (!xr_strcmp(st, "locked") && !usable)
				return door_fail(r, "locked");
			return door_phase(k, 1);
		}
	}

	// stuck: a shut door within 2 m -> aim and use it (1); an open one -> no use tap, it would close it (-1); none (0)
	int door_stuck(const Fvector& pos)
	{
		CGameObject* best = nullptr;
		float bd = 2.f;
		LPCSTR st = nullptr;
		xr_string info;
		bool usable = false;
		CObjectList& list = Level().Objects;
		for (u32 n = 0, cnt = list.o_count(); n < cnt; n++)
		{
			CObject* ob = list.o_get_by_iterator(n);
			if (!ob || ob->getDestroy() || ob->H_Parent() || ob->Position().distance_to(pos) > 4.f)
				continue;
			CGameObject* o = smart_cast<CGameObject*>(ob);
			if (!passage_door(o))
				continue;
			Fvector c;
			o->Center(c);
			float d = c.distance_to_xz(pos);
			if (d >= bd || c.y < pos.y - 1.f || c.y > pos.y + 3.f)
				continue;
			xr_string i;
			bool u;
			LPCSTR s = mcp_door_state(o, &i, &u);
			bd = d;
			best = o;
			st = s;
			info = i;
			usable = u;
		}
		if (!best)
			return 0;
		door_rec& r = door_note(best, st, info);
		if (r.done || r.presses >= max_presses)
			return -1;
		if (!is_shut(st) && !(usable && !xr_strcmp(st, "scripted")))
			return xr_strcmp(st, "no_logic") ? -1 : 0;
		if (!xr_strcmp(st, "locked") && !usable)
		{
			door_fail(r, "locked");
			return 1;
		}
		door_phase(door_index(best->ID()), 1);
		return 1;
	}

	// approach (phase 1, walking: ends near the door or when blocked), aim until the HUD pick is the door within use
	// range (2), press use and watch the result (3)
	void door_tick(u32 dt, const Fvector& pos, Fvector* aim = nullptr)
	{
		door_rec& r = g_mv.doors[g_mv.act];
		CGameObject* o = online(r.id);
		u32 t = g_mv.elapsed - g_mv.phase_t;
		if (!o)
		{
			if (g_mv.use_only && g_mv.phase == 3)
				return mover_finish("used", "gone"); // picked up / consumed
			return door_fail(r, "gone");
		}
		if (g_mv.phase == 1)
		{
			bool stalled = false;
			if (g_mv.elapsed - g_mv.poll_t >= 400)
			{
				stalled = t >= 800 && pos.distance_to(g_mv.phase_pos) < .15f;
				g_mv.phase_pos = pos;
				g_mv.poll_t = g_mv.elapsed;
			}
			if (pos.distance_to_xz(r.stand) <= .3f || stalled || t > 4000)
				door_phase(g_mv.act, 2);
			else if (aim)
				*aim = r.stand;
			if (g_mv.sprint_dik && !g_mv.sprint_tap && (g_actor->MovingState() & mcSprint) && pos.distance_to_xz(r.stand) < 3.f)
			{
				// a sprint overshoots the stand point into the leaf's swing: the opening leaf then sweeps the actor
				// behind it (Sidorovich's door)
				mcp_input::hold_key(g_mv.sprint_dik, true);
				g_mv.sprint_tap = true;
				g_mv.sprint_t = g_mv.elapsed;
			}
			return;
		}
		// still inside the leaf's swing (a sprint overshot the stand point): back off before the press, or the opening
		// leaf sweeps the actor behind it
		Fvector h = o->Position();
		bool back = g_mv.phase == 2 && !g_mv.use_only && g_mv.back_dik && t < 2000 &&
		            pos.distance_to_xz(h) < 2.f * r.gate.distance_to_xz(h) + .25f;
		if (back != g_mv.back_down)
		{
			mcp_input::hold_key(g_mv.back_dik, back);
			g_mv.back_down = back;
		}
		const collide::rq_result& rq = HUD().GetRQ();
		bool closer = g_mv.phase == 2 && !g_mv.use_only && !back && rq.O == o && rq.range >= use_range;
		if (g_mv.fwd_down != closer)
		{
			mcp_input::hold_key(g_mv.fwd, closer); // step toward a door just out of use range
			g_mv.fwd_down = closer;
		}
		if (g_mv.phase == 2)
		{
			if (g_mv.aim_try >= aim_points || t > 5000)
				return door_fail(r, "pick_missed");
			if (!look_toward(aim_point(o, g_mv.aim_try), dt))
			{
				g_mv.aim_ok = 0;
				return;
			}
			if (closer || back)
				return;
			if (rq.O == o && rq.range < use_range)
			{
				if (++g_mv.aim_ok < 2)
					return; // the pick must hold for two frames (the actor reads it in UpdateCL)
				if (!g_mv.use_dik)
					return door_fail(r, "use_not_bound");
				g_mv.tap_dik = g_mv.use_dik;
				mcp_input::hold_key(g_mv.tap_dik, true);
				g_mv.jump_tap = true;
				r.presses++;
				r.action = "use";
				r.pick.clear();
				g_mv.phase = 3;
				g_mv.phase_t = g_mv.poll_t = g_mv.elapsed;
				return;
			}
			g_mv.aim_ok = 0;
			if (rq.O == o)
				r.pick = "out_of_range";
			else if (rq.O)
				r.pick = xr_string("blocked by ") + rq.O->cName().c_str();
			else
				r.pick = rq.range > 0.f ? "blocked by level geometry" : "nothing hit";
			if (rq.O == o && g_mv.use_only)
				return door_fail(r, "out_of_range");
			if (++g_mv.aim_bad >= 4)
			{
				g_mv.aim_try++;
				g_mv.aim_bad = 0;
			}
			return;
		}
		// phase 3: poll every 100 ms
		if (g_mv.elapsed - g_mv.poll_t < 100)
			return;
		g_mv.poll_t = g_mv.elapsed;
		bool door = mcp_world_is_door(o);
		if (g_mv.use_only && !door)
		{
			if (o->H_Parent() == g_actor)
				return mover_finish("used", "taken");
			if (t >= 1500)
				return mover_finish("used", "no_effect");
			return;
		}
		xr_string info;
		bool usable;
		LPCSTR st = mcp_door_state(o, &info, &usable);
		door_note(o, st, info);
		if (g_mv.use_only)
		{
			bool settled = xr_strcmp(st, "opening") && xr_strcmp(st, "closing");
			if ((settled && (r.before != st || t >= 1200)) || t >= 6000)
				return mover_finish("used", r.before != st ? "door_toggled" : "no_effect");
			return;
		}
		if (!xr_strcmp(st, "open") || (!xr_strcmp(st, "opening") && t >= 6000))
		{
			r.action = "opened";
			r.done = true;
			g_mv.via = true;
			g_mv.via_pt = r.gate;
			return door_end();
		}
		if (t < 1200 || !xr_strcmp(st, "opening"))
			return;
		if (!is_shut(st))
		{
			r.done = true; // a door without ph_door logic: nothing to watch, walk on
			return door_end();
		}
		r.was_locked |= !xr_strcmp(st, "locked") || r.before == "locked";
		if (r.presses < max_presses && usable)
			return door_phase(g_mv.act, 2); // one more try (a missed press, or the logic needed two steps)
		door_fail(r, r.was_locked ? "locked" : "no_effect"); // a use on a locked door can toggle locked <-> closed
	}

	xr_string door_json(const door_rec& r)
	{
		jobj e;
		e.i("id", r.id).str("name", r.name.c_str()).str("section", r.section.c_str()).vec("position", r.pos);
		if (r.before.size())
			e.str("before", r.before.c_str()).str("after", r.after.c_str());
		e.str("action", r.action).i("presses", r.presses);
		if (r.reason)
			e.str("reason", r.reason);
		if (r.pick.size())
			e.str("pick", r.pick.c_str());
		if (r.info.size())
			e.raw("door", r.info);
		return e.done();
	}

	xr_string blocker_json(const collide::rq_result& r);

	// a thing the AI map does not know about that the actor should walk around (not a creature: it moves; not a door:
	// the door logic opens it; not loose items or the move's own target)
	bool obstacle(CObject* o)
	{
		if (!o || o->getDestroy() || o->H_Parent() || o->ID() == g_mv.target_id || smart_cast<CEntityAlive*>(o))
			return false;
		CGameObject* g = smart_cast<CGameObject*>(o);
		return g && !mcp_world_is_door(g) && !smart_cast<CInventoryItem*>(o);
	}

	// nearest hit of six short horizontal rays toward aim (knee and chest height; body centre and both shoulders):
	// objects_only -> only obstacle() objects count
	bool ahead_hit(const Fvector& pos, const Fvector& aim, float range, bool objects_only, collide::rq_result& hit)
	{
		Fvector dir;
		dir.set(aim.x - pos.x, 0, aim.z - pos.z);
		float len = dir.magnitude();
		if (len < .3f)
			return false;
		dir.div(len);
		range = _min(range, len);
		bool any = false;
		hit.range = flt_max;
		const float hs[2] = { .45f, 1.2f }, ss[3] = { -.3f, 0.f, .3f };
		for (float h : hs)
			for (float s : ss)
			{
				Fvector p;
				p.set(pos.x + dir.z * s, pos.y + h, pos.z - dir.x * s);
				collide::rq_result r;
				if (Level().ObjectSpace.RayPick(p, dir, range, collide::rqtBoth, r, g_actor) && r.range < hit.range &&
				    (!objects_only || obstacle(r.O)))
				{
					hit = r;
					any = true;
				}
			}
		return any;
	}

	// the same object ahead on two casts 100 ms apart: its AI nodes (box grown by 0.4 m, not the actor's own spot)
	// join g_mv.avoid and the path is replanned; no other route -> they are dropped again (the stuck steps follow)
	void obstacle_scan(const Fvector& pos, const Fvector& aim)
	{
		if (g_mv.elapsed - g_mv.ray_t < 100)
			return;
		g_mv.ray_t = g_mv.elapsed;
		collide::rq_result r;
		u16 id = ahead_hit(pos, aim, 1.8f, true, r) ? r.O->ID() : u16(-1);
		bool confirmed = id != u16(-1) && id == g_mv.ray_id;
		g_mv.ray_id = id;
		if (!confirmed || std::find(g_mv.obst.begin(), g_mv.obst.end(), id) != g_mv.obst.end())
			return;
		g_mv.obst.push_back(id);
		g_mv.blocker = blocker_json(r);
		CObject* o = r.O;
		Fvector c;
		o->Center(c);
		float rad = o->Radius();
		if (rad > 6.f)
			return mover_tried("obstacle_too_big", id);
		Fbox b;
		if (o->Visual())
			b.set(o->Visual()->getVisData().box);
		else
			b.set(-rad, -rad, -rad, rad, rad, rad);
		b.grow(.4f);
		Fmatrix inv;
		inv.invert(o->XFORM());
		navmap m(ai().level_graph());
		xr_vector<u32> keep = g_mv.avoid;
		int n = iCeil((rad + .5f) / m.cell);
		for (int x = m.cx(c.x) - n; x <= m.cx(c.x) + n; ++x)
			for (int z = m.cz(c.z) - n; z <= m.cz(c.z) + n; ++z)
			{
				u32 f, l;
				if (!m.cell_nodes(x, z, f, l))
					continue;
				for (u32 nd = f; nd < l; ++nd)
				{
					Fvector p = m.centre(nd), lp;
					if (p.distance_to_xz(pos) < .8f || p.distance_to_xz(g_mv.goal) < .5f)
						continue;
					inv.transform_tiny(lp, p);
					if (lp.x >= b.min.x && lp.x <= b.max.x && lp.z >= b.min.z && lp.z <= b.max.z && lp.y >= b.min.y - 1.5f && lp.y <= b.max.y)
						g_mv.avoid.push_back(nd);
				}
			}
		if (g_mv.avoid.size() == keep.size())
			return mover_tried("obstacle_off_map", id);
		g_mv.replans++;
		LPCSTR why;
		if (mover_plan(why))
			return mover_tried("avoid", id);
		g_mv.avoid.swap(keep);
		mover_tried("avoid_no_route", id);
	}

	void mover_step(u32 dt);

	void mover_tick(u32 dt)
	{
		if (!g_mv.active)
			return;
		CTimer t;
		t.Start();
		mover_step(dt);
		float us = t.GetElapsed_sec() * 1e6f;
		g_mv.tick_us += us;
		g_mv.tick_max = _max(g_mv.tick_max, us);
	}

	void mover_step(u32 dt)
	{
		// taps last one input frame
		if (g_mv.sprint_tap)
		{
			mcp_input::hold_key(g_mv.sprint_dik, false);
			g_mv.sprint_tap = false;
		}
		if (g_mv.jump_tap)
		{
			mcp_input::hold_key(g_mv.tap_dik, false);
			g_mv.jump_tap = false;
		}
		g_mv.elapsed += dt;
		g_mv.frames++;
		if ((pApp && pApp->IsLoading()) || !g_pGameLevel || g_pGameLevel->name() != g_mv.level || !g_ai_space || !ai().get_level_graph())
			return mover_finish("aborted", "level_change");
		if (!g_actor || !g_actor->g_Alive())
			return mover_finish("aborted", "actor_dead");
		CMainMenu* mm = g_pGamePersistent ? MainMenu() : nullptr;
		CUIGameCustom* gui = CurrentGameUI();
		if ((mm && mm->IsActive()) || (gui && gui->TopInputReceiver()) || (Console && Console->bVisible))
		{
			if (g_mv.use_only && g_mv.phase == 3 && gui && gui->TopInputReceiver())
				return mover_finish("used", "dialog_opened");
			return mover_finish("aborted", "dialog");
		}
		if (g_mv.fwd_down && !mcp_input::is_held(g_mv.fwd))
			return mover_finish("aborted", "keys_released");
		if (g_mv.use_only)
		{
			if (g_mv.elapsed > g_mv.timeout_ms)
				return mover_finish("timeout");
			return door_tick(dt, g_actor->Position());
		}
		if (g_mv.target_id != u16(-1))
		{
			CObject* o = Level().Objects.net_Find(g_mv.target_id);
			if (!o || o->getDestroy())
				return mover_finish("aborted", "target_gone");
			g_mv.target = o->Position();
			LPCSTR why;
			if (g_mv.target.distance_to(g_mv.path.back()) > 2.f && g_mv.elapsed - g_mv.replan_t > 1000 && !mover_plan(why))
				return plan_failed(why);
		}

		Fvector pos = g_actor->Position();
		g_mv.travelled += pos.distance_to(g_mv.last_pos);
		g_mv.last_pos = pos;
		if (pos.distance_to_xz(g_mv.target) <= g_mv.stop && _abs(pos.y - g_mv.target.y) < 2.5f)
			return mover_finish("reached");
		if (g_mv.elapsed > g_mv.timeout_ms)
			return mover_finish("timeout");

		// next corner: reached ones are skipped, and so is a corner whose successor is already in straight walkable line
		const CLevelGraph& lg = ai().level_graph();
		// (on the corner's floor: a corner straight above or below is not reached)
		u32 last = (u32)g_mv.path.size() - 1;
		while (g_mv.wp < last && pos.distance_to_xz(g_mv.path[g_mv.wp]) < .5f && _abs(pos.y - g_mv.path[g_mv.wp].y) < 2.f)
		{
			g_mv.wp++;
			g_mv.no_skip = false;
		}
		u32 node = g_actor->ai_location().level_vertex_id();
		// (the map's straight line can still be blocked: door frames, where the stuck replan came from)
		if (g_mv.wp < last && !g_mv.no_skip && lg.valid_vertex_id(node) && lg.inside(node, pos))
		{
			const Fvector& nx = g_mv.path[g_mv.wp + 1];
			navmap m(lg);
			u32 nn = m.node_at(nx);
			if (nn != invalid && lg.check_vertex_in_direction(node, Fvector2().set(pos.x, pos.z), nn) && m.clear(pos, nx, clearance))
				g_mv.wp++;
		}
		// copy: a door replan replaces the path; an off-map target: walk to its snapped point (not into the wall)
		Fvector aim = g_mv.wp < last ? g_mv.path[g_mv.wp] : g_mv.goal_on ? g_mv.target : g_mv.path[last];
		if (g_mv.wp >= last && pos.distance_to_xz(aim) < .35f && (!g_mv.goal_on || _abs(pos.y - g_mv.target.y) >= 2.5f))
		{
			// at the path end, target not reached: never turn on the spot under / over it
			if (_abs(pos.y - g_mv.target.y) < 2.5f)
				return mover_finish("unreachable", "target_off_mesh");
			LPCSTR why;
			if (g_mv.end_replan)
				return mover_finish("unreachable", "wrong_floor");
			g_mv.end_replan = true;
			g_mv.replans++;
			mover_tried("replan_wrong_floor");
			if (!mover_plan(why))
				return plan_failed(why);
			return;
		}
		if (g_mv.via)
		{
			// poll_t = when the door opened
			if (pos.distance_to_xz(g_mv.via_pt) < .4f || g_mv.elapsed - g_mv.poll_t > 3000 || g_mv.phase)
			{
				g_mv.via = false;
				LPCSTR why; // the path planned while the door was shut may graze its frame: re-pull it from the doorway
				if (!g_mv.phase && mover_plan(why))
				{
					g_mv.no_skip = true;
					return;
				}
			}
			else
				aim = g_mv.via_pt;
		}
		if (!g_mv.phase)
			door_scan(pos, aim);
		if (!g_mv.active)
			return;
		if (g_mv.phase)
		{
			door_tick(dt, pos, &aim);
			if (g_mv.phase != 1)
				return;
		}
		float d = wrap180(heading(pos, aim) - camera_yaw());
		float max_turn = 720.f * dt / 1000.f;
		float turn = _max(-max_turn, _min(d, max_turn));
		if (_abs(turn) > .01f)
			g_actor->cam_Active()->Move(turn > 0 ? kRIGHT : kLEFT, deg2rad(_abs(turn)));
		if (g_mv.level_pitch)
		{
			// look ahead again after aiming at a door
			float p = camera_pitch(), step = _min(_abs(p), max_turn * .5f);
			if (step > .01f)
				g_actor->cam_Active()->Move(p > 0 ? kUP : kDOWN, deg2rad(step));
			g_mv.level_pitch = _abs(p) > 1.f;
		}
		bool want = _abs(d) < 50.f;
		if (want != g_mv.fwd_down)
		{
			mcp_input::hold_key(g_mv.fwd, want);
			g_mv.fwd_down = want;
		}
		if (g_mv.fwd_down)
			g_mv.fwd_ms += dt;
		if (g_mv.sprint && !g_mv.phase && g_mv.fwd_down && _abs(d) < 20.f && !(g_actor->MovingState() & mcSprint) && g_mv.elapsed - g_mv.sprint_t > 1500)
		{
			mcp_input::hold_key(g_mv.sprint_dik, true);
			g_mv.sprint_tap = true;
			g_mv.sprint_t = g_mv.elapsed;
		}
		if (!g_mv.phase && g_mv.fwd_down)
		{
			obstacle_scan(pos, aim);
			if (!g_mv.active)
				return;
		}

		// stuck: under 0.3 m of progress per second of holding forward -> open a door within 2 m (else tap use), jump,
		// replan, give up; 3 s without progress while turning in place (forward never held long) -> replan, give up
		if (!g_mv.phase && g_mv.elapsed - g_mv.check_t >= 1000)
		{
			bool held_long = g_mv.fwd_ms > 600, moved = pos.distance_to(g_mv.check_pos) >= .3f;
			g_mv.idle = moved || held_long ? 0 : g_mv.idle + 1;
			if (!moved && (held_long || g_mv.idle >= 3))
			{
				if (!held_long)
				{
					g_mv.idle = 0;
					g_mv.stuck = _max(g_mv.stuck, 2u);
				}
				g_mv.stuck++;
				g_mv.stuck_total++;
				g_mv.stuck_at = pos;
				if (g_mv.wp < last && pos.distance_to_xz(g_mv.path[g_mv.wp]) < 1.f)
				{
					g_mv.wp++; // a corner the capsule cannot get closer to
					mover_tried("skip_corner");
				}
				LPCSTR why;
				if (g_mv.stuck == 1)
				{
					int ds = door_stuck(pos);
					if (ds > 0)
					{
						mover_tried("door");
						return; // door phase started (or the door failed: avoided / blocked_door)
					}
					if (ds < 0)
						g_mv.stuck = 2; // an open door near: no use tap
				}
				if (g_mv.stuck <= 2 && (g_mv.stuck == 1 ? g_mv.use_dik : g_mv.jump_dik))
				{
					g_mv.tap_dik = g_mv.stuck == 1 ? g_mv.use_dik : g_mv.jump_dik;
					mcp_input::hold_key(g_mv.tap_dik, true);
					g_mv.jump_tap = true;
					mover_tried(g_mv.stuck == 1 ? "use" : "jump");
				}
				else if (g_mv.stuck <= 3 && (g_mv.rep_at.distance_to(pos) > 1.f || g_mv.stuck_rep < 3))
				{
					// replans that keep ending at the same spot count toward giving up
					g_mv.stuck_rep = g_mv.rep_at.distance_to(pos) > 1.f ? 1 : g_mv.stuck_rep + 1;
					g_mv.rep_at = pos;
					g_mv.replans++;
					mover_tried(held_long ? "replan" : "replan_turning");
					if (!mover_plan(why))
						return plan_failed(why);
					g_mv.no_skip = true;
				}
				else
				{
					collide::rq_result r; // what is in front now
					g_mv.blocker = ahead_hit(pos, aim, 1.5f, false, r) ? blocker_json(r) : jobj().str("what", "none_ahead").done();
					return mover_finish("stuck");
				}
			}
			else if (moved)
				g_mv.stuck = 0;
			g_mv.check_pos = pos;
			g_mv.check_t = g_mv.elapsed;
			g_mv.fwd_ms = 0;
		}
	}

	xr_string mover_json()
	{
		jobj r;
		r.i("id", g_mv.id).str("state", g_mv.state).b("done", !g_mv.active);
		if (g_mv.reason)
			r.str("reason", g_mv.reason);
		if (!g_mv.id)
			return r.done();
		if (g_mv.use_only)
		{
			r.str("mode", "use").i("elapsed_ms", g_mv.elapsed).i("frames", g_mv.frames);
			return r.raw("use", door_json(g_mv.doors[0])).done();
		}
		r.vec("target", g_mv.target);
		if (g_mv.target_id != u16(-1))
			r.i("target_id", g_mv.target_id);
		if (g_mv.doors.size())
		{
			xr_string a = "[";
			for (const door_rec& d : g_mv.doors)
				a += (a.size() > 1 ? "," : "") + door_json(d);
			r.raw("doors", a + "]");
		}
		if (g_mv.blocked != u16(-1))
			r.i("blocked_door", g_mv.blocked);
		r.i("elapsed_ms", g_mv.elapsed).i("frames", g_mv.frames).num("path_length", g_mv.path_length, 1);
		r.i("waypoint", g_mv.wp).i("waypoints", g_mv.path.size()).num("travelled", g_mv.travelled, 1);
		if (g_actor && g_pGameLevel && g_pGameLevel->name() == g_mv.level)
		{
			Fvector p = g_actor->Position();
			r.vec("position", p).num("distance_left", p.distance_to_xz(g_mv.target), 2);
		}
		r.i("stuck_checks", g_mv.stuck_total).i("replans", g_mv.replans);
		if (g_mv.stuck_total)
			r.vec("stuck_at", g_mv.stuck_at);
		if (!g_mv.path.empty())
			r.vec("goal", g_mv.goal).b("goal_on_mesh", g_mv.goal_on);
		if (g_mv.blocker.size())
			r.raw("blocker", g_mv.blocker);
		if (g_mv.tried.size())
		{
			xr_string a = "[";
			for (const xr_string& s : g_mv.tried)
				a += (a.size() > 1 ? "," : "") + js(s.c_str());
			r.raw("recovery", a + "]");
		}
		if (g_mv.frames)
			r.raw("tick_us", jobj().num("avg", g_mv.tick_us / g_mv.frames, 1).num("max", g_mv.tick_max, 0).done());
		return r.done();
	}

	void shoot_replace(); // a running nav.shoot ends "replaced": both turn the camera

	bool nav_move_to(LPCSTR params, xr_string& out)
	{
		ctx c;
		if (!ready(params, c, out, "to"))
			return true;
		if (!g_actor || !g_actor->g_Alive())
		{
			out = jobj().b("available", false).str("reason", "no_actor").done();
			return true;
		}
		if (g_mv.active)
			mover_finish("aborted", "replaced");
		shoot_replace();
		s64 id = -1;
		mcp::get_int_param(params, "id", id);
		mover_t m;
		m.id = g_mv.id + 1;
		m.level = g_pGameLevel->name();
		m.target = c.origin;
		if (id >= 0)
		{
			CObject* o = id < 0xffff ? Level().Objects.net_Find((u16)id) : nullptr;
			if (!o || o->getDestroy())
			{
				out = "object " + xr_string(std::to_string(id).c_str()) + " is not online";
				return false;
			}
			m.target_id = (u16)id;
			m.target = o->Position();
		}
		else if (c.from_actor)
		{
			out = "to (\"x,y,z\") or id required";
			return false;
		}
		m.stop = _max(.3f, num_param(params, "stop_distance", id >= 0 ? 1.5f : 1.f));
		m.timeout_ms = (u32)_max<s64>(1000, _min<s64>(int_param(params, "timeout_ms", 120000), 3600000));
		m.sprint = int_param(params, "sprint", 0) != 0;
		m.fwd = get_action_dik(kFWD);
		m.sprint_dik = get_action_dik(kSPRINT_TOGGLE);
		m.back_dik = get_action_dik(kBACK);
		m.jump_dik = get_action_dik(kJUMP);
		m.use_dik = get_action_dik(kUSE);
		if (!m.fwd)
		{
			out = "the forward action is not bound to a key";
			return false;
		}
		m.start = m.last_pos = m.check_pos = g_actor->Position();
		g_mv = m;
		g_mv.active = true;
		g_mv.state = "running";
		LPCSTR why = nullptr;
		if (!mover_plan(why))
			plan_failed(why);
		out = mover_json();
		return true;
	}

	// Aimed use (like a player): look at the object's pick point until the HUD pick is that object within use range,
	// press use, report what happened (shares the mover's door aim / watch phases)
	bool nav_use(LPCSTR params, xr_string& out)
	{
		ctx c;
		if (!ready(params, c, out, "to"))
			return true;
		if (!g_actor || !g_actor->g_Alive())
		{
			out = jobj().b("available", false).str("reason", "no_actor").done();
			return true;
		}
		s64 id = -1;
		CGameObject* o = mcp::get_int_param(params, "id", id) && id >= 0 && id < 0xffff ? online((u16)id) : nullptr;
		if (!o)
		{
			out = "id of an online object required";
			return false;
		}
		if (g_mv.active)
			mover_finish("aborted", "replaced");
		shoot_replace();
		mover_t m;
		m.id = g_mv.id + 1;
		m.level = g_pGameLevel->name();
		m.use_only = true;
		m.target_id = o->ID();
		m.target = o->Position();
		m.timeout_ms = 10000;
		m.fwd = get_action_dik(kFWD);
		m.use_dik = get_action_dik(kUSE);
		g_mv = m;
		g_mv.active = true;
		g_mv.state = "running";
		door_rec r;
		r.id = o->ID();
		r.name = o->cName().c_str();
		r.section = o->cNameSect_str();
		r.pos = o->Position();
		if (mcp_world_is_door(o))
			r.before = r.after = mcp_door_state(o, &r.info, nullptr);
		g_mv.doors.push_back(r);
		door_phase(0, 2);
		float dist = Device.vCameraPosition.distance_to(aim_point(o, 0));
		if (dist > use_range + .5f)
		{
			g_mv.doors[0].pick = "out_of_range";
			mover_finish("failed", "out_of_range");
		}
		out = mover_json();
		return true;
	}

	bool nav_move_status(LPCSTR params, xr_string& out)
	{
		if (int_param(params, "stop", 0) && g_mv.active)
			mover_finish("aborted", "stopped");
		out = mover_json();
		return true;
	}

	// ------------------------------------------------------------------ shoot: keep the camera on a bone of a creature (re-aimed
	// every input frame, so recoil kick is corrected) and fire the equipped weapon through the normal fire / reload actions,
	// only while the weapon's own fire ray (CActor::GetPick: the ray bullets leave along before dispersion) hits the target first

	struct shot_hits // filled by the CEntity::Hit hook (any thread) under g_hits_lock
	{
		u32 hits = 0, other = 0;
		float power = 0;
		xr_vector<std::pair<xr_string, u32>> bones; // target bone -> hits
		xr_vector<u16> others;                      // other objects the actor hit meanwhile (first 8)
	};

	struct shooter_t
	{
		u32 id = 0;
		bool active = false;
		LPCSTR state = "idle"; // running, done, target_dead, target_gone, los_blocked, out_of_range, out_of_ammo, jammed, no_weapon, timeout, aborted
		LPCSTR reason = nullptr;
		u16 target = u16(-1), wid = u16(-1);
		xr_string name, section, part, bone, weapon, blocker;
		const void* level = nullptr; // g_pGameLevel identity: a save load (same level name) ends the shoot too
		bool fire = true, until_dead = false, ads = false, zoomed = false;
		u32 want = 1, burst = 3, burst_n = 0, burst_from = 0, timeout_ms = 30000, elapsed = 0, frames = 0, conn = 0, releases = 0;
		int fire_dik = 0, reload_dik = 0, zoom_dik = 0;
		bool fire_down = false, tap = false, reload_tap = false, zoom_down = false;
		u32 shots = 0, reloads = 0, jams = 0, on_frames = 0;
		int mag = 0, mag_before = -1, total_before = -1, mag_after = -1, total_after = -1;
		u32 wstate = 0, press_t = 0, release_t = 0, zoom_t = 0, nowpn_t = 0, blocked_t = 0, range_t = 0, los_t = 0;
		u32 settle_t = 0, unjam_t = 0, unjam_tries = 0, reload_t = 0, reload_tries = 0, dead_t = 0;
		bool nowpn = false, blocked = false, beyond = false, settling = false, misfire = false, dead = false;
		float health_before = 0, health = 0, dist = 0, err = 0, bias_y = 0, bias_p = 0;
		Fvector aim, pos;
		u16 killer = u16(-1);
	};
	shooter_t g_sh;
	shot_hits g_hits;
	xrCriticalSection g_hits_lock;

	void shoot_on_hit(CEntity* victim, const SHit* h)
	{
		if (!g_sh.active || !g_actor || h->whoID != g_actor->ID() || victim == g_actor)
			return;
		xr_string bone;
		IKinematics* k = victim->Visual() ? victim->Visual()->dcast_PKinematics() : nullptr;
		if (k && h->boneID < k->LL_BoneCount())
			bone = k->LL_BoneName_dbg(h->boneID);
		xrCriticalSectionGuard guard(g_hits_lock);
		if (victim->ID() != g_sh.target)
		{
			g_hits.other++;
			if (g_hits.others.size() < 8 && std::find(g_hits.others.begin(), g_hits.others.end(), victim->ID()) == g_hits.others.end())
				g_hits.others.push_back(victim->ID());
			return;
		}
		g_hits.hits++;
		g_hits.power += h->power;
		auto it = std::find_if(g_hits.bones.begin(), g_hits.bones.end(), [&bone](const std::pair<xr_string, u32>& b) { return b.first == bone; });
		if (it != g_hits.bones.end())
			it->second++;
		else
			g_hits.bones.emplace_back(bone, 1);
	}

	u16 part_bone(CGameObject* o, LPCSTR part)
	{
		IKinematics* k = o->Visual() ? o->Visual()->dcast_PKinematics() : nullptr;
		if (!k)
			return BI_NONE;
		static LPCSTR head[] = { "bip01_head", "head", nullptr }, chest[] = { "bip01_spine2", "bip01_spine1", "bip01_spine", "spine", nullptr };
		LPCSTR* names = !xr_strcmp(part, "head") ? head : !xr_strcmp(part, "chest") ? chest : nullptr;
		if (!names)
			return k->LL_BoneID(part);
		for (; *names; ++names)
		{
			u16 b = k->LL_BoneID(*names);
			if (b != BI_NONE)
				return b;
		}
		return BI_NONE;
	}

	// world centre of the bone's collision shape (what the pick ray tests), else the bone origin
	void bone_point(CGameObject* o, u16 b, Fvector& p)
	{
		IKinematics* k = o->Visual()->dcast_PKinematics();
		const SBoneShape& s = k->LL_GetData(b).shape;
		Fvector l;
		if (s.type == SBoneShape::stBox)
			l = s.box.m_translate;
		else if (s.type == SBoneShape::stSphere)
			l = s.sphere.P;
		else if (s.type == SBoneShape::stCylinder)
			l = s.cylinder.m_center;
		else
			l.set(0, 0, 0);
		Fmatrix m;
		m.mul_43(o->XFORM(), k->LL_GetTransform(b));
		m.transform_tiny(p, l);
	}

	// aim point of the target this frame; bone name in g_sh.bone ("center" without a matching bone)
	Fvector shoot_aim(CGameObject* o)
	{
		Fvector p;
		u16 b = part_bone(o, g_sh.part.c_str());
		IKinematics* k = o->Visual() ? o->Visual()->dcast_PKinematics() : nullptr;
		if (!k || b == BI_NONE || b >= k->LL_BoneCount())
		{
			g_sh.bone = "center";
			o->Center(p);
			return p;
		}
		g_sh.bone = k->LL_BoneName_dbg(b);
		bone_point(o, b, p);
		return p;
	}

	// the actor's firearm in hands, else null and why
	CWeapon* shoot_weapon(LPCSTR& why)
	{
		PIItem it = g_actor->inventory().ActiveItem();
		CWeapon* w = smart_cast<CWeapon*>(it);
		why = nullptr;
		if (!it)
			why = "nothing in hands";
		else if (!w)
			why = "not a firearm in hands";
		else if (smart_cast<CWeaponKnife*>(w))
			why = "knife in hands";
		else if (smart_cast<CWeaponBinoculars*>(w))
			why = "binoculars in hands";
		else if (smart_cast<CWeaponMagazinedWGrenade*>(w) && smart_cast<CWeaponMagazinedWGrenade*>(w)->GetGrenadeLauncherMode())
			why = "grenade launcher mode (switch back to the rifle)";
		return why ? nullptr : w;
	}

	float pitch_to(const Fvector& from, const Fvector& to)
	{
		Fvector d;
		d.sub(to, from);
		return rad2deg(atan2f(d.y, _sqrt(d.x * d.x + d.z * d.z)));
	}

	void key_up(int dik, bool& down)
	{
		if (down && mcp_input::is_held(dik))
			mcp_input::hold_key(dik, false);
		down = false;
	}

	void key_tap(int dik)
	{
		mcp_input::hold_key(dik, true);
		mcp_input::hold_key(dik, false);
	}

	// main menu, console or a game dialog (inventory, talk, PDA...) takes the input
	bool ui_blocking()
	{
		CMainMenu* mm = g_pGamePersistent ? MainMenu() : nullptr;
		CUIGameCustom* gui = CurrentGameUI();
		return (mm && mm->IsActive()) || (gui && gui->TopInputReceiver()) || (Console && Console->bVisible);
	}

	// final target / weapon numbers (the objects may be gone)
	void shoot_sample()
	{
		if ((pApp && pApp->IsLoading()) || !g_pGameLevel || g_pGameLevel != g_sh.level || !g_actor)
			return;
		if (CEntityAlive* ea = smart_cast<CEntityAlive*>(online(g_sh.target)))
		{
			g_sh.health = _max(0.f, ea->GetfHealth());
			g_sh.pos = ea->Position();
			g_sh.killer = ea->g_Alive() ? u16(-1) : ea->killer_id();
		}
		LPCSTR why;
		if (CWeapon* w = shoot_weapon(why))
		{
			g_sh.mag_after = w->GetAmmoElapsed();
			g_sh.total_after = w->GetSuitableAmmoTotal();
		}
	}

	void shoot_finish(LPCSTR state, LPCSTR reason = nullptr)
	{
		key_up(g_sh.fire_dik, g_sh.fire_down);
		g_sh.tap = false;
		if (g_sh.reload_tap)
			key_up(g_sh.reload_dik, g_sh.reload_tap);
		key_up(g_sh.zoom_dik, g_sh.zoom_down);
		if (g_sh.zoomed && psActorFlags.test(AF_AIM_TOGGLE) && g_actor && g_pGameLevel == g_sh.level && !(pApp && pApp->IsLoading()) && !ui_blocking())
		{
			LPCSTR why;
			CWeapon* w = shoot_weapon(why);
			if (w && w->IsZoomed())
				key_tap(g_sh.zoom_dik); // toggle aim back off
		}
		shoot_sample();
		g_sh.active = false;
		g_sh.state = state;
		g_sh.reason = reason;
	}

	void shoot_replace()
	{
		if (g_sh.active)
			shoot_finish("aborted", "replaced");
	}

	xr_string blocker_json(const collide::rq_result& r)
	{
		jobj b;
		b.num("distance", r.range, 2);
		if (r.O)
		{
			CEntityAlive* ea = smart_cast<CEntityAlive*>(r.O);
			b.str("what", ea && ea->g_Alive() ? "creature" : "object").i("id", r.O->ID()).str("name", r.O->cName().c_str());
			b.str("section", r.O->cNameSect_str());
		}
		else
		{
			b.str("what", "level_geometry");
			if (r.element >= 0 && r.element < Level().ObjectSpace.GetStaticModel()->get_tris_count())
			{
				u16 m = (u16)Level().ObjectSpace.GetStaticTris()[r.element].material;
				if (m < GMLib.CountMaterial())
					b.str("material", GMLib.GetMaterialByIdx(m)->m_Name.c_str());
			}
		}
		return b.done();
	}

	void shoot_tick(u32 dt)
	{
		if (!g_sh.active)
			return;
		// taps last one input frame
		if (g_sh.tap)
		{
			key_up(g_sh.fire_dik, g_sh.fire_down);
			g_sh.tap = false;
		}
		if (g_sh.reload_tap)
			key_up(g_sh.reload_dik, g_sh.reload_tap);
		g_sh.elapsed += dt;
		g_sh.frames++;
		if ((pApp && pApp->IsLoading()) || !g_pGameLevel || g_pGameLevel != g_sh.level)
			return shoot_finish("aborted", "level_change");
		if (!g_actor || !g_actor->g_Alive())
			return shoot_finish("aborted", "actor_dead");
		if (mcp::client_id() != g_sh.conn || mcp_input::release_count() != g_sh.releases ||
			(g_sh.fire_down && !mcp_input::is_held(g_sh.fire_dik)) || (g_sh.zoom_down && !mcp_input::is_held(g_sh.zoom_dik)))
			return shoot_finish("aborted", "keys_released");
		if (ui_blocking())
			return shoot_finish("aborted", "dialog");
		CEntityAlive* o = smart_cast<CEntityAlive*>(online(g_sh.target));
		if (!o)
			return shoot_finish("target_gone");
		g_sh.health = _max(0.f, o->GetfHealth());
		if (!o->g_Alive())
		{
			key_up(g_sh.fire_dik, g_sh.fire_down);
			if (!g_sh.dead)
				g_sh.dead_t = g_sh.elapsed;
			g_sh.dead = true;
			if (o->killer_id() != u16(-1) || g_sh.elapsed - g_sh.dead_t >= 1000) // the killer is set by the death event
				shoot_finish("target_dead");
			return;
		}
		if (g_sh.settling && g_sh.elapsed - g_sh.settle_t >= 500)
			return shoot_finish("done"); // the last rounds had time to land
		if (g_sh.elapsed > g_sh.timeout_ms)
			return g_sh.fire ? shoot_finish("timeout") : shoot_finish("done", "max_seconds");

		// turn the camera so that the fire ray points at the aim point; bias = filtered angle between the fire ray and
		// the camera ray (non-zero with g_firepos / g_aimpos: bullets leave from the barrel)
		Fvector aim = g_sh.aim = shoot_aim(o);
		g_sh.pos = o->Position();
		const Fvector& eye = Device.vCameraPosition;
		float cy = wrap180(heading(eye, aim) - camera_yaw()), cp = pitch_to(eye, aim) - camera_pitch();
		SPickParam& pp = g_actor->GetPick();
		bool ray = !pp.barrel_blocked && pp.defs.dir.square_magnitude() > EPS;
		LPCSTR why;
		CWeapon* w = shoot_weapon(why);
		u32 st = w ? w->GetState() : 0;
		bool steady = w && !w->IsPending() && (st == CHUDState::eIdle || st == CWeapon::eFire || st == CWeapon::eFire2);
		if (ray)
		{
			float fy = wrap180(heading(pp.defs.start, aim) - rad2deg(atan2f(pp.defs.dir.x, pp.defs.dir.z)));
			float fp = pitch_to(pp.defs.start, aim) - rad2deg(asinf(_max(-1.f, _min(1.f, pp.defs.dir.y))));
			if (steady) // the barrel swings during draw / reload animations
			{
				g_sh.bias_y += .25f * (_max(-10.f, _min(wrap180(fy - cy), 10.f)) - g_sh.bias_y);
				g_sh.bias_p += .25f * (_max(-10.f, _min(fp - cp, 10.f)) - g_sh.bias_p);
			}
			g_sh.err = _sqrt(fy * fy + fp * fp);
		}
		else
			g_sh.err = _sqrt(cy * cy + cp * cp);
		float mx = 1080.f * dt / 1000.f;
		float ty = _max(-mx, _min(cy + g_sh.bias_y, mx)), tp = _max(-mx, _min(cp + g_sh.bias_p, mx));
		CCameraBase* cam = g_actor->cam_Active();
		if (_abs(ty) > .005f)
			cam->Move(ty > 0 ? kRIGHT : kLEFT, deg2rad(_abs(ty)));
		if (_abs(tp) > .005f)
			cam->Move(tp > 0 ? kDOWN : kUP, deg2rad(_abs(tp))); // kDOWN looks up

		// on target = the weapon's fire ray (last frame) hits this creature first, within the weapon's fire distance
		Fvector start = ray ? pp.defs.start : eye;
		g_sh.dist = start.distance_to(aim);
		bool on = ray && pp.result.O == o && (!w || pp.result.range <= w->GetFireDistance());
		g_sh.on_frames = on ? g_sh.on_frames + 1 : 0;
		if (on)
		{
			g_sh.blocked = false;
			g_sh.blocker.clear();
		}
		else if (g_sh.elapsed - g_sh.los_t >= 100)
		{
			// line of fire to the aim point: same ray rules as the pick (objects stop it, see-through geometry not)
			g_sh.los_t = g_sh.elapsed;
			SPickParam lp;
			lp.defs.start = start;
			lp.defs.dir.sub(aim, start);
			float len = lp.defs.dir.magnitude();
			if (len > EPS_L)
			{
				lp.defs.dir.div(len);
				lp.defs.range = len + .5f;
				HUD().DoPick(lp);
				if (lp.result.O == o || (lp.result.O && lp.result.O->H_Parent() == o) || lp.result.range >= len - .05f)
				{
					g_sh.blocked = false;
					g_sh.blocker.clear();
				}
				else
				{
					if (!g_sh.blocked)
						g_sh.blocked_t = g_sh.elapsed;
					g_sh.blocked = true;
					g_sh.blocker = blocker_json(lp.result);
				}
			}
		}
		if (pp.barrel_blocked)
		{
			if (!g_sh.blocked)
				g_sh.blocked_t = g_sh.elapsed;
			g_sh.blocked = true;
			g_sh.blocker = jobj().str("what", "barrel_blocked").done(); // the weapon is pushed into geometry
		}
		bool beyond = w && g_sh.dist > w->GetFireDistance();
		if (beyond && !g_sh.beyond)
			g_sh.range_t = g_sh.elapsed;
		g_sh.beyond = beyond;
		if (!g_sh.settling && g_sh.blocked && g_sh.elapsed - g_sh.blocked_t >= 1500)
			return shoot_finish("los_blocked");
		if (!g_sh.settling && beyond && g_sh.elapsed - g_sh.range_t >= 1500)
			return shoot_finish("out_of_range");

		if (!g_sh.fire)
			return;
		if (!w)
		{
			key_up(g_sh.fire_dik, g_sh.fire_down);
			if (!g_sh.nowpn)
				g_sh.nowpn_t = g_sh.elapsed;
			g_sh.nowpn = true;
			if (xr_strcmp(why, "nothing in hands") || g_sh.elapsed - g_sh.nowpn_t >= 2000) // 2 s for a slot switch
				return shoot_finish("no_weapon", why);
			return;
		}
		g_sh.nowpn = false;

		// shots = rounds that left the magazine outside reloads (a cleared jam ejects one)
		int mag = w->GetAmmoElapsed();
		if (w->ID() != g_sh.wid)
		{
			g_sh.wid = w->ID();
			g_sh.weapon = w->cNameSect_str();
			g_sh.wstate = st;
		}
		else
		{
			if (mag < g_sh.mag && st != CWeapon::eReload && g_sh.wstate != CWeapon::eReload)
				g_sh.shots += g_sh.mag - mag;
			if (st == CWeapon::eReload && g_sh.wstate != CWeapon::eReload && !g_sh.misfire)
				g_sh.reloads++;
		}
		g_sh.mag = mag;
		g_sh.wstate = st;

		if (g_sh.settling)
			return;
		if (!g_sh.until_dead && g_sh.shots >= g_sh.want)
		{
			key_up(g_sh.fire_dik, g_sh.fire_down);
			g_sh.settling = true;
			g_sh.settle_t = g_sh.elapsed;
			return;
		}
		if (w->IsMisfire())
		{
			key_up(g_sh.fire_dik, g_sh.fire_down);
			if (!g_sh.misfire)
			{
				g_sh.jams++;
				g_sh.unjam_tries = 0;
				g_sh.unjam_t = g_sh.elapsed;
			}
			g_sh.misfire = true;
			// the engine clears a jam on reload (eReload with the jammed round); a mod may bind it elsewhere
			if (st != CWeapon::eReload && !w->IsPending() && (!g_sh.unjam_tries || g_sh.elapsed - g_sh.unjam_t >= 3000))
			{
				if (!g_sh.reload_dik || g_sh.unjam_tries >= 2)
					return shoot_finish("jammed", g_sh.reload_dik ? "reload did not clear the jam" : "reload is not bound to a key");
				mcp_input::hold_key(g_sh.reload_dik, true);
				g_sh.reload_tap = true;
				g_sh.unjam_tries++;
				g_sh.unjam_t = g_sh.elapsed;
			}
			return;
		}
		g_sh.misfire = false;
		if (!steady)
		{
			key_up(g_sh.fire_dik, g_sh.fire_down); // drawing, reloading, switching mode: wait
			return;
		}
		if (mag == 0)
		{
			key_up(g_sh.fire_dik, g_sh.fire_down);
			if (w->GetSuitableAmmoTotal() <= 0)
				return shoot_finish("out_of_ammo");
			if (!g_sh.reload_tries || g_sh.elapsed - g_sh.reload_t >= 3000)
			{
				if (!g_sh.reload_dik || g_sh.reload_tries >= 2)
					return shoot_finish("out_of_ammo", g_sh.reload_dik ? "reload did not start" : "reload is not bound to a key");
				mcp_input::hold_key(g_sh.reload_dik, true);
				g_sh.reload_tap = true;
				g_sh.reload_tries++;
				g_sh.reload_t = g_sh.elapsed;
			}
			return;
		}
		g_sh.reload_tries = 0;
		if (g_sh.ads && g_sh.zoom_dik && !w->IsZoomed() && st == CHUDState::eIdle && g_sh.elapsed - g_sh.zoom_t >= 500)
		{
			key_up(g_sh.zoom_dik, g_sh.zoom_down);
			mcp_input::hold_key(g_sh.zoom_dik, true);
			g_sh.zoom_down = g_sh.zoomed = true;
			g_sh.zoom_t = g_sh.elapsed;
			if (psActorFlags.test(AF_AIM_TOGGLE))
				key_up(g_sh.zoom_dik, g_sh.zoom_down);
		}

		bool ok = on && g_sh.on_frames >= 2 && !beyond;
		if (w->GetCurrentFireMode() < 0) // automatic: short bursts while on target
		{
			if (g_sh.fire_down)
			{
				bool stalled = g_sh.shots == g_sh.burst_from && g_sh.elapsed - g_sh.press_t >= 600; // e.g. a press that only left safe mode
				if (!ok || g_sh.shots - g_sh.burst_from >= g_sh.burst_n || stalled)
				{
					key_up(g_sh.fire_dik, g_sh.fire_down);
					g_sh.release_t = g_sh.elapsed;
				}
			}
			else if (ok && g_sh.elapsed - g_sh.release_t >= 150)
			{
				mcp_input::hold_key(g_sh.fire_dik, true);
				g_sh.fire_down = true;
				g_sh.press_t = g_sh.elapsed;
				g_sh.burst_from = g_sh.shots;
				g_sh.burst_n = g_sh.until_dead ? g_sh.burst : _min(g_sh.burst, g_sh.want - g_sh.shots);
			}
		}
		else if (ok && st == CHUDState::eIdle && g_sh.elapsed - g_sh.release_t >= 100) // single / fixed burst: one press each
		{
			mcp_input::hold_key(g_sh.fire_dik, true);
			g_sh.fire_down = g_sh.tap = true;
			g_sh.release_t = g_sh.elapsed;
		}
	}

	xr_string shoot_json()
	{
		jobj r;
		r.i("id", g_sh.id).str("state", g_sh.state).b("done", !g_sh.active);
		if (g_sh.reason)
			r.str("reason", g_sh.reason);
		if (!g_sh.id)
			return r.done();
		r.str("mode", g_sh.fire ? "shoot" : "track");
		jobj t;
		t.i("id", g_sh.target).str("name", g_sh.name.c_str()).str("section", g_sh.section.c_str()).str("part", g_sh.part.c_str());
		t.str("bone", g_sh.bone.c_str()).vec("position", g_sh.pos).num("distance", g_sh.dist, 2);
		t.num("health_before", g_sh.health_before).num("health", g_sh.health);
		if (g_sh.killer != u16(-1))
			t.i("killer_id", g_sh.killer).b("killed_by_actor", g_actor && g_sh.killer == g_actor->ID());
		r.raw("target", t.done());
		{
			xrCriticalSectionGuard guard(g_hits_lock);
			r.i("shots", g_sh.shots).i("hits", g_hits.hits).num("hit_power", g_hits.power);
			xr_string bones = "{";
			for (auto& b : g_hits.bones)
				bones += (bones.size() > 1 ? "," : "") + js(b.first.c_str()) + ":" + std::to_string(b.second).c_str();
			r.raw("hit_bones", bones + "}");
			if (g_hits.other)
			{
				xr_string ids = "[";
				for (u16 v : g_hits.others)
					ids += (ids.size() > 1 ? "," : "") + xr_string(std::to_string(v).c_str());
				r.i("other_hits", g_hits.other).raw("other_ids", ids + "]");
			}
		}
		if (g_sh.weapon.size())
		{
			jobj a;
			a.i("mag_before", g_sh.mag_before).i("total_before", g_sh.total_before);
			a.i("mag", g_sh.active ? g_sh.mag : g_sh.mag_after).i("total", g_sh.total_after);
			r.str("weapon", g_sh.weapon.c_str()).raw("ammo", a.done()).i("reloads", g_sh.reloads).i("jams", g_sh.jams);
		}
		r.num("aim_error_deg", g_sh.err, 2);
		if (g_sh.blocker.size())
			r.raw("blocker", g_sh.blocker);
		return r.i("elapsed_ms", g_sh.elapsed).i("frames", g_sh.frames).done();
	}

	bool nav_shoot(LPCSTR params, xr_string& out)
	{
		ctx c;
		if (!ready(params, c, out, "to"))
			return true;
		if (!g_actor || !g_actor->g_Alive())
		{
			out = jobj().b("available", false).str("reason", "no_actor").done();
			return true;
		}
		s64 id = -1;
		CEntityAlive* o = mcp::get_int_param(params, "id", id) && id >= 0 && id < 0xffff ? smart_cast<CEntityAlive*>(online((u16)id)) : nullptr;
		if (!o || !o->Visual())
		{
			out = "id of an online creature required";
			return false;
		}
		if (!o->g_Alive())
		{
			out = "the target is already dead";
			return false;
		}
		xr_string part = "chest";
		mcp::get_string_param(params, "part", part);
		if (part.empty())
			part = "chest";
		if (part != "chest" && part != "head" && part_bone(o, part.c_str()) == BI_NONE)
		{
			out = "no bone \"" + part + "\" on " + o->cNameVisual().c_str() + " (part: chest, head or a bone name)";
			return false;
		}
		shooter_t s;
		s.fire = int_param(params, "fire", 1) != 0;
		s.until_dead = int_param(params, "until_dead", 0) != 0;
		s.ads = int_param(params, "ads", 0) != 0;
		s.want = (u32)_max<s64>(1, _min<s64>(int_param(params, "shots", 1), 10000));
		s.burst = (u32)_max<s64>(1, _min<s64>(int_param(params, "burst", 3), 100));
		s.timeout_ms = (u32)_max<s64>(500, _min<s64>(int_param(params, "timeout_ms", 30000), 3600000));
		s.fire_dik = get_action_dik(kWPN_FIRE);
		s.reload_dik = get_action_dik(kWPN_RELOAD);
		s.zoom_dik = get_action_dik(kWPN_ZOOM);
		if (s.fire && !s.fire_dik)
		{
			out = "the fire action is not bound to a key";
			return false;
		}
		if (g_mv.active)
			mover_finish("aborted", "replaced");
		shoot_replace();
		s.id = g_sh.id + 1;
		s.level = g_pGameLevel;
		s.target = o->ID();
		s.name = o->cName().c_str();
		s.section = o->cNameSect_str();
		s.part = part;
		s.health_before = s.health = o->GetfHealth();
		s.conn = mcp::client_id();
		s.releases = mcp_input::release_count();
		s.pos = o->Position();
		LPCSTR why;
		CWeapon* w = shoot_weapon(why);
		if (w)
		{
			s.wid = w->ID();
			s.weapon = w->cNameSect_str();
			s.mag = s.mag_before = s.mag_after = w->GetAmmoElapsed();
			s.total_before = s.total_after = w->GetSuitableAmmoTotal();
			s.wstate = w->GetState();
		}
		g_sh = s;
		{
			xrCriticalSectionGuard guard(g_hits_lock);
			g_hits = shot_hits();
		}
		g_sh.active = true;
		g_sh.state = "running";
		g_sh.aim = shoot_aim(o);
		g_sh.dist = Device.vCameraPosition.distance_to(g_sh.aim);
		bool switching = g_actor->inventory().GetNextActiveSlot() != g_actor->inventory().GetActiveSlot();
		if (g_sh.fire && !w && !(switching && !xr_strcmp(why, "nothing in hands")))
			shoot_finish("no_weapon", why);
		else if (g_sh.fire && w && g_sh.dist > w->GetFireDistance())
			shoot_finish("out_of_range");
		out = shoot_json();
		return true;
	}

	bool nav_shoot_status(LPCSTR params, xr_string& out)
	{
		if (int_param(params, "stop", 0) && g_sh.active)
			shoot_finish("aborted", "stopped");
		out = shoot_json();
		return true;
	}

	void driver_tick(u32 dt)
	{
		mover_tick(dt);
		shoot_tick(dt);
	}

	struct frame_hook : public pureFrame
	{
		virtual void _BCL OnFrame() { overlay_update(); }
	} frame;

	// Batched line lists per colour (DRender flushes by itself near the u16 index limit)
	struct line_batch
	{
		xr_vector<Fvector> v;
		xr_vector<u16> idx;
		u32 color;
		explicit line_batch(u32 c) : color(c) {}
		void flush()
		{
			if (!idx.empty())
				DRender->add_lines(v.data(), (u32)v.size(), idx.data(), (u32)idx.size() / 2, color, false);
			v.clear();
			idx.clear();
		}
		void need(u32 nv)
		{
			if (v.size() + nv > 16000)
				flush();
		}
		void line(const Fvector& a, const Fvector& b)
		{
			need(2);
			u16 base = (u16)v.size();
			v.push_back(a);
			v.push_back(b);
			idx.push_back(base);
			idx.push_back(base + 1);
		}
	};
}

// CLevel::OnRender hook: draw the overlay state built by the frame hook (no-op unless nav.overlay turned it on)
void mcp_nav_render()
{
	if (!g_ov.on || !g_ov.lg || !DRender || !g_ai_space || ai().get_level_graph() != g_ov.lg)
		return;
	const CLevelGraph& lg = *g_ov.lg;
	navmap m(lg);
	float h = m.cell * .35f;
	line_batch ok(color_rgba(40, 255, 60, 255)), cut(color_rgba(255, 110, 30, 255));
	for (size_t k = 0; k < g_ov.nodes.size(); ++k)
	{
		if (g_ov.vis[k] == 2)
			continue;
		u32 id = g_ov.nodes[k];
		const CLevelGraph::CVertex* v = lg.vertex(id);
		Fvector c = m.centre(id);
		float dx[4] = { -h, h, h, -h }, dz[4] = { -h, -h, h, h };
		line_batch& b = g_ov.reach[k] ? ok : cut;
		b.need(4);
		u16 base = (u16)b.v.size();
		for (int i = 0; i < 4; ++i)
		{
			b.v.push_back(Fvector().set(c.x + dx[i], lg.vertex_plane_y(v, c.x + dx[i], c.z + dz[i]) + .06f, c.z + dz[i]));
			b.idx.push_back(base + i);
			b.idx.push_back(base + (i + 1) % 4);
		}
	}
	ok.flush();
	cut.flush();
	line_batch path(color_rgba(255, 230, 0, 255)), post(color_rgba(0, 220, 255, 255)), mark(color_rgba(255, 0, 255, 255));
	Fvector up0, up1, top;
	up0.set(0, .3f, 0);
	up1.set(0, .36f, 0);
	top.set(0, 1.5f, 0);
	for (size_t k = 0; k < g_ov.path.size(); ++k)
	{
		const Fvector& p = g_ov.path[k];
		if (k)
		{
			path.line(Fvector().add(g_ov.path[k - 1], up0), Fvector().add(p, up0));
			path.line(Fvector().add(g_ov.path[k - 1], up1), Fvector().add(p, up1));
		}
		post.line(p, Fvector().add(p, top));
	}
	for (const Fvector& p : g_ov.markers)
	{
		mark.line(p, Fvector().add(p, Fvector().set(0, 3.f, 0)));
		mark.line(Fvector().add(p, Fvector().set(-.5f, .05f, 0)), Fvector().add(p, Fvector().set(.5f, .05f, 0)));
		mark.line(Fvector().add(p, Fvector().set(0, .05f, -.5f)), Fvector().add(p, Fvector().set(0, .05f, .5f)));
	}
	path.flush();
	post.flush();
	mark.flush();
}

void mcp_register_nav()
{
	if (!Core.ParamsData.test(ECoreParams::mcp_port))
		return;
	mcp::register_method("nav.info", nav_info);
	mcp::register_method("nav.at", nav_at);
	mcp::register_method("nav.around", nav_around);
	mcp::register_method("nav.path", nav_path);
	mcp::register_method("nav.nearest", nav_nearest);
	mcp::register_method("nav.grid", nav_grid);
	mcp::register_method("nav.overlay", nav_overlay);
	mcp::register_method("nav.move_to", nav_move_to);
	mcp::register_method("nav.move_status", nav_move_status);
	mcp::register_method("nav.use", nav_use);
	mcp::register_method("nav.shoot", nav_shoot);
	mcp::register_method("nav.shoot_status", nav_shoot_status);
	mcp_input::set_driver(driver_tick);
	g_mcp_on_hit = shoot_on_hit;
	Device.seqFrame.Add(&frame, REG_PRIORITY_LOW);
}

