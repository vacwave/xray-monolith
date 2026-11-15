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
#include <queue>

// MCP nav.*: AI-map (level graph) and game-graph queries for agents (read-only), plus a debug overlay of nodes / a path
// drawn into the 3D view (same line path as Lua debug gizmos: DRender->add_lines, flushed by CLevel::OnRender).
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
		// 50 m, then a full scan
		u32 nearest(const Fvector& p, float& dist, bool& scanned, const u32* comp_of = nullptr, u32 comp = 0) const
		{
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
							float d = centre(id).distance_to(p);
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
					float d = centre(id).distance_to_sqr(p);
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
	};

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
		s.id = m.nearest(p, s.dist, sc);
		if (s.id != invalid)
			s.point = m.centre(s.id);
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
	bool long_astar(const navmap& m, u32 s, u32 d, u32 cap, float max_ms, xr_vector<u32>& path, u32& visited, bool& truncated)
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
		auto visible = [&](u32 k) { return m.lg.check_vertex_in_direction(path[a], ap, path[k]); };
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
			Fvector c = m.centre(path[a]);
			ap.set(c.x, c.z);
		}
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

	// level path between two snapped nodes; points start at from and end at to_point
	void level_path(const navmap& m, u32 s, const Fvector& from, u32 d, const Fvector& to_point, u32 cap, float max_ms,
	                path_result& pr)
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
		pr.found = ai().graph_engine().search(m.lg, s, d, &pr.nodes, params);
		pr.visited = ai().graph_engine().m_algorithm->data_storage().get_visited_node_count();
		if (!pr.found && pr.visited >= params.max_visited_node_count && cap > 65500)
		{
			pr.engine = "mcp_astar";
			pr.found = long_astar(m, s, d, cap, max_ms, pr.nodes, pr.visited, pr.truncated);
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
				pr.points.push_back(k + 1 == pr.nodes.size() ? to_point : m.centre(pr.nodes[k]));
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

	struct mover_t
	{
		u32 id = 0;
		bool active = false;
		LPCSTR state = "idle"; // running, reached, stuck, timeout, aborted, no_path
		LPCSTR reason = nullptr;
		u16 target_id = u16(-1);
		Fvector target, start, last_pos, check_pos;
		float stop = 1.f, travelled = 0, path_length = 0;
		u32 timeout_ms = 60000, elapsed = 0, frames = 0;
		bool sprint = false, fwd_down = false, sprint_tap = false, jump_tap = false;
		xr_vector<Fvector> path;
		u32 wp = 0;
		shared_str level;
		int fwd = 0, sprint_dik = 0, jump_dik = 0, use_dik = 0, tap_dik = 0;
		u32 check_t = 0, fwd_ms = 0, stuck = 0, stuck_total = 0, replans = 0, sprint_t = 0, replan_t = 0;
		Fvector stuck_at;
	};
	mover_t g_mv;

	void mover_release()
	{
		if (g_mv.fwd_down && mcp_input::is_held(g_mv.fwd))
			mcp_input::hold_key(g_mv.fwd, false);
		if (g_mv.sprint_tap && mcp_input::is_held(g_mv.sprint_dik))
			mcp_input::hold_key(g_mv.sprint_dik, false);
		if (g_mv.jump_tap && mcp_input::is_held(g_mv.tap_dik))
			mcp_input::hold_key(g_mv.tap_dik, false);
		g_mv.fwd_down = g_mv.sprint_tap = g_mv.jump_tap = false;
	}

	void mover_finish(LPCSTR state, LPCSTR reason = nullptr)
	{
		mover_release();
		g_mv.active = false;
		g_mv.state = state;
		g_mv.reason = reason;
	}

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
		path_result pr;
		level_path(m, s, c.origin, st.id, st.point, 400000, 200.f, pr);
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
		return true;
	}

	void mover_tick(u32 dt)
	{
		if (!g_mv.active)
			return;
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
			return mover_finish("aborted", "dialog");
		if (g_mv.fwd_down && !mcp_input::is_held(g_mv.fwd))
			return mover_finish("aborted", "keys_released");
		if (g_mv.target_id != u16(-1))
		{
			CObject* o = Level().Objects.net_Find(g_mv.target_id);
			if (!o || o->getDestroy())
				return mover_finish("aborted", "target_gone");
			g_mv.target = o->Position();
			LPCSTR why;
			if (g_mv.target.distance_to(g_mv.path.back()) > 2.f && g_mv.elapsed - g_mv.replan_t > 1000 && !mover_plan(why))
				return mover_finish("no_path", why);
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
		u32 last = (u32)g_mv.path.size() - 1;
		while (g_mv.wp < last && pos.distance_to_xz(g_mv.path[g_mv.wp]) < .7f)
			g_mv.wp++;
		u32 node = g_actor->ai_location().level_vertex_id();
		if (g_mv.wp < last && lg.valid_vertex_id(node) && lg.inside(node, pos))
		{
			const Fvector& nx = g_mv.path[g_mv.wp + 1];
			u32 nn = navmap(lg).node_at(nx);
			if (nn != invalid && lg.check_vertex_in_direction(node, Fvector2().set(pos.x, pos.z), nn))
				g_mv.wp++;
		}
		const Fvector& aim = g_mv.wp >= last ? g_mv.target : g_mv.path[g_mv.wp];
		float d = wrap180(heading(pos, aim) - camera_yaw());
		float max_turn = 720.f * dt / 1000.f;
		float turn = _max(-max_turn, _min(d, max_turn));
		if (_abs(turn) > .01f)
			g_actor->cam_Active()->Move(turn > 0 ? kRIGHT : kLEFT, deg2rad(_abs(turn)));
		bool want = _abs(d) < 50.f;
		if (want != g_mv.fwd_down)
		{
			mcp_input::hold_key(g_mv.fwd, want);
			g_mv.fwd_down = want;
		}
		if (g_mv.fwd_down)
			g_mv.fwd_ms += dt;
		if (g_mv.sprint && g_mv.fwd_down && _abs(d) < 20.f && !(g_actor->MovingState() & mcSprint) && g_mv.elapsed - g_mv.sprint_t > 1500)
		{
			mcp_input::hold_key(g_mv.sprint_dik, true);
			g_mv.sprint_tap = true;
			g_mv.sprint_t = g_mv.elapsed;
		}

		// stuck: under 0.3 m of progress per second of holding forward -> use (opens doors), jump, replan, give up
		if (g_mv.elapsed - g_mv.check_t >= 1000)
		{
			bool held_long = g_mv.fwd_ms > 600;
			if (held_long && pos.distance_to(g_mv.check_pos) < .3f)
			{
				g_mv.stuck++;
				g_mv.stuck_total++;
				g_mv.stuck_at = pos;
				LPCSTR why;
				if (g_mv.stuck <= 2 && (g_mv.stuck == 1 ? g_mv.use_dik : g_mv.jump_dik))
				{
					g_mv.tap_dik = g_mv.stuck == 1 ? g_mv.use_dik : g_mv.jump_dik;
					mcp_input::hold_key(g_mv.tap_dik, true);
					g_mv.jump_tap = true;
				}
				else if (g_mv.stuck <= 3)
				{
					g_mv.replans++;
					if (!mover_plan(why))
						return mover_finish("no_path", why);
				}
				else
					return mover_finish("stuck");
			}
			else if (pos.distance_to(g_mv.check_pos) >= .3f)
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
		r.vec("target", g_mv.target);
		if (g_mv.target_id != u16(-1))
			r.i("target_id", g_mv.target_id);
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
		return r.done();
	}

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
			mover_finish("no_path", why);
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
	mcp_input::set_driver(mover_tick);
	Device.seqFrame.Add(&frame, REG_PRIORITY_LOW);
}

