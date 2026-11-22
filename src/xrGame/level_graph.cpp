////////////////////////////////////////////////////////////////////////////
//	Module 		: level_graph.cpp
//	Created 	: 02.10.2001
//  Modified 	: 11.11.2003
//	Author		: Oles Shihkovtsov, Dmitriy Iassenev
//	Description : Level graph
////////////////////////////////////////////////////////////////////////////

#include "stdafx.h"
#include "level_graph.h"
#include "profiler.h"

LPCSTR LEVEL_GRAPH_NAME = "level.ai";

#ifdef AI_COMPILER
CLevelGraph::CLevelGraph		(LPCSTR filename)
#else
CLevelGraph::CLevelGraph()
#endif
{
	m_nodes_allocated = false;
#ifndef AI_COMPILER
#ifdef DEBUG
	sh_debug->create				("debug\\ai_nodes","$null");
#endif
	string_path file_name;
	FS.update_path(file_name, "$level$", LEVEL_GRAPH_NAME);
#else
	string256					file_name;
	strconcat					(sizeof(file_name), file_name, filename, LEVEL_GRAPH_NAME);
#endif
	m_reader = FS.r_open(file_name);
	R_ASSERT2(m_reader, "Cannot open level.ai");

	// m_header & data
	m_header = (CHeader*)m_reader->pointer();
	R_ASSERT2(header().version() == XRAI_CURRENT_VERSION || header().version() == XRAI_LARGE_VERSION,
		"Unsupported level.ai version");
	m_reader->advance(sizeof(CHeader));
	const u64 node_bytes = u64(header().vertex_count()) *
		(header().version() == XRAI_LARGE_VERSION ? sizeof(CVertex) : sizeof(NodeCompressed));
	R_ASSERT2(node_bytes <= m_reader->elapsed(), "Truncated level.ai node array");
	if (header().version() == XRAI_LARGE_VERSION)
	{
		R_ASSERT2(header().vertex_count() < NodeCompressed13::LINK_MASK, "Too many level.ai nodes");
		m_nodes = (CVertex*)m_reader->pointer();
	}
	else
	{
		const NodeCompressed* old_nodes = (const NodeCompressed*)m_reader->pointer();
		m_nodes = xr_alloc<CVertex>(header().vertex_count());
		ZeroMemory(m_nodes, sizeof(CVertex) * header().vertex_count());
		m_nodes_allocated = true;
		for (u32 i = 0; i < header().vertex_count(); ++i)
		{
			for (u8 direction = 0; direction < 4; ++direction)
			{
				const u32 old_link = old_nodes[i].link(direction);
				m_nodes[i].set_link(direction,
					old_link == 0x007fffff ? NodeCompressed13::LINK_MASK : old_link);
			}
			m_nodes[i].high = old_nodes[i].high;
			m_nodes[i].low = old_nodes[i].low;
			m_nodes[i].NodeCompressed13::plane = old_nodes[i].plane;
			m_nodes[i].p.xz(old_nodes[i].p.xz());
			m_nodes[i].p.y(u16(old_nodes[i].p.y()));
		}
	}
	m_row_length = iFloor((header().box().max.z - header().box().min.z) / header().cell_size() + EPS_L + 1.5f);
	m_column_length = iFloor((header().box().max.x - header().box().min.x) / header().cell_size() + EPS_L + 1.5f);
	m_access_mask.assign(header().vertex_count(), true);
	unpack_xz(vertex_position(header().box().max), m_max_x, m_max_z);

#ifdef DEBUG
#	ifndef AI_COMPILER
		m_current_level_id		= -1;
		m_current_actual		= false;
		m_current_center		= Fvector().set(flt_max,flt_max,flt_max);
		m_current_radius		= Fvector().set(flt_max,flt_max,flt_max);
#	endif
#endif
}

CLevelGraph::~CLevelGraph()
{
	if (m_nodes_allocated)
		xr_free(m_nodes);
	FS.r_close(m_reader);
}

// first node in [b, e) with packed xz >= key (u64 so row arithmetic cannot wrap)
static const CLevelGraph::CVertex* lower_xz(const CLevelGraph::CVertex* b, const CLevelGraph::CVertex* e, u64 key)
{
	return std::lower_bound(b, e, key, [](const CLevelGraph::CVertex& v, u64 k) { return v.position().xz() < k; });
}

// cell index a grid coordinate rounds to, clamped to [0, last] (NaN gives 0)
static u32 clamped_cell(float f, u32 last)
{
	if (!(f > 0.f))
		return 0;
	if (f >= float(last))
		return last;
	return u32(iFloor(f));
}

u32 CLevelGraph::vertex(const Fvector& position) const
{
	float min_dist = flt_max;
	u32 selected;
	set_invalid_vertex(selected);
	const float st = header().cell_size() * .5f;
	const CVertex* B = m_nodes;
	const CVertex* E = m_nodes + header().vertex_count();
	const u64 rl = m_row_length;
	const u32 z0 = clamped_cell((position.z - header().box().min.z) / header().cell_size() + .5f, m_row_length - 1);
	const u32 x0 = clamped_cell((position.x - header().box().min.x) / header().cell_size() + .5f, m_column_length - 1);

	// lower bound: xz distance to the cell square; a vertex beyond min_dist cannot win. Returns false if skipped
	auto visit = [&](const CVertex* I, float dx, float& z) -> bool
	{
		float x;
		unpack_xz(*I, x, z);
		float dz = _abs(position.z - z) - st;
		if (dz < 0.f) dz = 0.f;
		if (dx * dx + dz * dz > min_dist * 1.01f + .01f)
			return false;

		const u32 i = u32(I - B);
		float dist = distance(i, position);
		// lowest index wins a tie, as in an ascending full scan
		if (dist < min_dist || (dist == min_dist && valid_vertex_id(selected) && i < selected))
		{
			min_dist = dist;
			selected = i;
		}
		return true;
	};

	// scans one x row outward from z0; returns true when this and all rows farther in this direction are beyond the bound
	auto scan_row = [&](const CVertex* rb, const CVertex* re, bool up) -> bool
	{
		float x, z;
		unpack_xz(*rb, x, z);
		float dx = _abs(position.x - x) - st;
		if (dx < 0.f) dx = 0.f;
		if (dx * dx > min_dist * 1.01f + .01f)
			return up ? x >= position.x : x <= position.x;

		const CVertex* mid = lower_xz(rb, re, u64(rb->position().xz() / m_row_length) * rl + z0);
		for (const CVertex* I = mid; I != re; ++I)
			if (!visit(I, dx, z) && z >= position.z)
				break;
		for (const CVertex* I = mid; I != rb;)
			if (!visit(--I, dx, z) && z <= position.z)
				break;
		return false;
	};

	// nodes are sorted by packed xz = x * row_length + z: walk rows outward from x0 in both directions
	const CVertex* S = lower_xz(B, E, u64(x0) * rl);
	for (const CVertex* rb = S; rb != E;)
	{
		const CVertex* re = lower_xz(rb, E, u64(rb->position().xz() / m_row_length + 1) * rl);
		if (scan_row(rb, re, true))
			break;
		rb = re;
	}
	for (const CVertex* re = S; re != B;)
	{
		const CVertex* rb = lower_xz(B, re, u64((re - 1)->position().xz() / m_row_length) * rl);
		if (scan_row(rb, re, false))
			break;
		re = rb;
	}

	VERIFY(valid_vertex_id(selected));
	return (selected);
}

u32 CLevelGraph::vertex(u32 current_node_id, const Fvector& position) const
{
	START_PROFILE("Level_Graph::find vertex")
#ifndef AI_COMPILER
		Device.Statistic->AI_Node.Begin();
#endif

		u32 id;

		if (valid_vertex_position(position))
		{
			// so, our position is inside the level graph bounding box
			if (valid_vertex_id(current_node_id) && inside(vertex(current_node_id), position))
			{
				// so, our node corresponds to the position
#ifndef AI_COMPILER
				Device.Statistic->AI_Node.End();
#endif
				return (current_node_id);
			}

			// so, our node doesn't correspond to the position
			// try to search it with O(logN) time algorithm
			u32 _vertex_id = vertex_id(position);
			if (valid_vertex_id(_vertex_id))
			{
				// so, there is a node which corresponds with x and z to the position
				bool ok = true;
				if (valid_vertex_id(current_node_id))
				{
					{
						CVertex const& vertex = *this->vertex(current_node_id);
						for (u32 i = 0; i < 4; ++i)
						{
							if (vertex.link(i) == _vertex_id)
							{
#ifndef AI_COMPILER
								Device.Statistic->AI_Node.End();
#endif // AI_COMPILER
								return (_vertex_id);
							}
						}
					}
					{
						CVertex const& vertex = *this->vertex(_vertex_id);
						for (u32 i = 0; i < 4; ++i)
						{
							if (vertex.link(i) == current_node_id)
							{
#ifndef AI_COMPILER
								Device.Statistic->AI_Node.End();
#endif // AI_COMPILER
								return (_vertex_id);
							}
						}
					}

					float y0 = vertex_plane_y(current_node_id, position.x, position.z);
					float y1 = vertex_plane_y(_vertex_id, position.x, position.z);
					bool over0 = position.y > y0;
					bool over1 = position.y > y1;
					float y_dist0 = position.y - y0;
					float y_dist1 = position.y - y1;
					if (over0)
					{
						if (over1)
						{
							if (y_dist1 - y_dist0 > 1.f)
								ok = false;
							else
								ok = true;
						}
						else
						{
							if (y_dist0 - y_dist1 > 1.f)
								ok = false;
							else
								ok = true;
						}
					}
					else
					{
						ok = true;
					}
				}
				if (ok)
				{
#ifndef AI_COMPILER
					Device.Statistic->AI_Node.End();
#endif
					return (_vertex_id);
				}
			}
		}

		if (!valid_vertex_id(current_node_id))
		{
			// so, we do not have a correct current node
			// performing very slow full search
			id = vertex(position);
			VERIFY(valid_vertex_id(id));
#ifndef AI_COMPILER
			Device.Statistic->AI_Node.End();
#endif
			return (id);
		}

		u32 new_vertex_id = guess_vertex_id(current_node_id, position);
		if (new_vertex_id != current_node_id)
			return (new_vertex_id);

		// so, our position is outside the level graph bounding box
		// or
		// there is no node for the current position
		// try to search the nearest one iteratively
		SContour _contour;
		Fvector point;
		u32 best_vertex_id = current_node_id;
		contour(_contour, current_node_id);
		nearest(point, position, _contour);
		float best_distance_sqr = position.distance_to_sqr(point);
		const_iterator i, e;
		begin(current_node_id, i, e);
		for (; i != e; ++i)
		{
			u32 level_vertex_id = value(current_node_id, i);
			if (!valid_vertex_id(level_vertex_id))
				continue;

			contour(_contour, level_vertex_id);
			nearest(point, position, _contour);
			float distance_sqr = position.distance_to_sqr(point);
			if (best_distance_sqr > distance_sqr)
			{
				best_distance_sqr = distance_sqr;
				best_vertex_id = level_vertex_id;
			}
		}

#ifndef AI_COMPILER
		Device.Statistic->AI_Node.End();
#endif
		return (best_vertex_id);

	STOP_PROFILE
}

u32 CLevelGraph::vertex_id(const Fvector& position) const
{
	VERIFY2(
		valid_vertex_position(position),
		make_string(
			"invalid position for CLevelGraph::vertex_id specified: [%f][%f][%f]",
			VPUSH(position)
		)
	);

	if (!valid_vertex_position(position))
	{
		Msg("{@} --- Invalid position for CLevelGraph::vertex_id specified: [%f][%f][%f]", VPUSH(position));
		return (u32(-1));
	}

	CPosition _vertex_position = vertex_position(position);
	CVertex* B = m_nodes;
	CVertex* E = m_nodes + header().vertex_count();
	CVertex* I = std::lower_bound(B, E, _vertex_position.xz());
	if ((I == E) || ((*I).position().xz() != _vertex_position.xz()))
		return (u32(-1));

	u32 best_vertex_id = u32(I - B);
	float y = vertex_plane_y(best_vertex_id, position.x, position.z);
	for (++I; I != E; ++I)
	{
		if ((*I).position().xz() != _vertex_position.xz())
			break;

		u32 new_vertex_id = u32(I - B);
		float _y = vertex_plane_y(new_vertex_id, position.x, position.z);
		if (y <= position.y)
		{
			// so, current node is under the specified position
			if (_y <= position.y)
			{
				// so, new node is under the specified position
				if (position.y - _y < position.y - y)
				{
					// so, new node is closer to the specified position
					y = _y;
					best_vertex_id = new_vertex_id;
				}
			}
		}
		else
			// so, current node is over the specified position
			if (_y <= position.y)
			{
				// so, new node is under the specified position
				y = _y;
				best_vertex_id = new_vertex_id;
			}
			else
				// so, new node is over the specified position
				if (_y - position.y < y - position.y)
				{
					// so, new node is closer to the specified position
					y = _y;
					best_vertex_id = new_vertex_id;
				}
	}

	return (best_vertex_id);
}

static const int max_guess_vertex_count = 4;

u32 CLevelGraph::guess_vertex_id(u32 const& current_vertex_id, Fvector const& position) const
{
	VERIFY(valid_vertex_id(current_vertex_id));

	CPosition vertex_position;
	if (valid_vertex_position(position))
		vertex_position = this->vertex_position(position);
	else
		vertex_position = vertex(current_vertex_id)->position();

	u32 x, z;
	unpack_xz(vertex_position, x, z);

	SContour vertex_contour;
	contour(vertex_contour, current_vertex_id);
	Fvector best_point;
	float result_distance = nearest(best_point, position, vertex_contour);
	u32 result_vertex_id = current_vertex_id;

	CVertex const* B = m_nodes;
	CVertex const* E = m_nodes + header().vertex_count();
	u32 start_x = (u32)_max(0, int(x) - max_guess_vertex_count);
	u32 stop_x = _min(max_x(), x + (u32)max_guess_vertex_count);
	u32 start_z = (u32)_max(0, int(z) - max_guess_vertex_count);
	u32 stop_z = _min(max_z(), z + (u32)max_guess_vertex_count);
	for (u32 i = start_x; i <= stop_x; ++i)
	{
		for (u32 j = start_z; j <= stop_z; ++j)
		{
			u32 test_xz = i * m_row_length + j;
			CVertex const* I = std::lower_bound(B, E, test_xz);
			if (I == E)
				continue;

			if ((*I).position().xz() != test_xz)
				continue;

			u32 best_vertex_id = u32(I - B);
			contour(vertex_contour, best_vertex_id);
			float best_distance = nearest(best_point, position, vertex_contour);
			for (++I; I != E; ++I)
			{
				if ((*I).position().xz() != test_xz)
					break;

				u32 vertex_id = u32(I - B);
				Fvector point;
				contour(vertex_contour, vertex_id);
				float distance = nearest(point, position, vertex_contour);
				if (distance >= best_distance)
					continue;

				best_point = point;
				best_distance = distance;
				best_vertex_id = vertex_id;
			}

			if (_abs(best_point.y - position.y) >= 3.f)
				continue;

			if (result_distance <= best_distance)
				continue;

			result_distance = best_distance;
			result_vertex_id = best_vertex_id;
		}
	}

	return (result_vertex_id);
}
