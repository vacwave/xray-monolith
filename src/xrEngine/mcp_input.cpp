#include "stdafx.h"
#include "mcp_input.h"
#include "mcp_server.h"
#include "mcp_time.h"
#include "xr_input.h"
#include "IInputReceiver.h"

#include <nlohmann/json.hpp>

using json = nlohmann::json;

// Step grammar (input.run {"steps":[...]}): an array runs its items in sequence, an array nested in it runs its items
// in parallel (and an array inside that again in sequence, ...). Items:
//   {"action":"forward"|"key":"ESCAPE", "ms":N, "state":"down"|"up"}  no ms/state = tap (1 frame), ms = hold N ms
//   {"text":"abc"}  {"wheel":N}  {"wait":N}  {"look":{"yaw":deg,"pitch":deg},"ms":N}  {"look_at":[x,y,z],"ms":N}
//   {"look_dir":[x,y,z],"ms":N}  {"mouse":{"dx":px,"dy":px},"ms":N}
//   {"cursor":[x,y],"ms":N}  UI cursor to UI-space x,y (1024x768), gliding over ms, done a frame later at the earliest
//   {"cursor":"release"}  give the UI cursor back to the Windows mouse (no-op in silent runs)
namespace mcp_input
{
namespace
{
	enum
	{
		mouse_1 = 0xED + 100, // MOUSE_1..MOUSE_8 as in xr_level_controller.h
		mouse_8 = 0xED + 107,
		max_ops = 10000,
		max_depth = 16,
		max_ms = 600000,
		keep_done = 32, // finished sequences kept for input.status
		wheel_notch = 120, // DirectInput wheel delta per notch
	};

	enum op_kind { o_seq, o_par, o_key, o_wheel, o_wait, o_look, o_look_at, o_mouse, o_cursor };
	enum key_mode { km_tap, km_down, km_up };

	struct op
	{
		op_kind kind = o_wait;
		int dik = 0;
		key_mode mode = km_tap;
		u32 ms = 0;
		float yaw = 0, pitch = 0; // look total, radians (+ = right / up); look_at fills it at start
		Fvector target{}; // look_at position or direction
		bool target_dir = false;
		int dx = 0, dy = 0, wheel = 0;
		xr_vector<op> children;

		bool started = false, done = false;
		u32 t0 = 0, frame0 = 0, idx = 0;
		float done_yaw = 0, done_pitch = 0;
		int done_dx = 0, done_dy = 0;
		bool cursor_release = false, cursor_failed = false; // cursor: target.x/y = destination, from = start
		Fvector2 from{};
	};

	struct sequence
	{
		u32 id = 0;
		op root;
		bool done = false, cancelled = false;
		u32 t0 = 0, frame0 = 0, t1 = 0, frame1 = 0;
		xr_string warning;
	};

	struct held_key
	{
		int dik;
		u32 frame; // press frame: mouse Hold starts on the next frame, like CInput::MouseUpdate
	};

	game_hooks hooks{};
	bool enabled = false;
	u32 next_id = 1;
	xr_list<sequence> seqs;
	xr_vector<held_key> held;
	u32 last_conn = 0;
	u32 clock = 0, last_wall = 0; // input time: wall ms, or simulation ms while MCP time control is in use

	// accumulated for this frame and delivered once
	int frame_dx = 0, frame_dy = 0, frame_wheel = 0;
	float frame_yaw = 0, frame_pitch = 0;
	bool frame_cursor_failed = false;

	std::string dump(const json& j) { return j.dump(-1, ' ', false, json::error_handler_t::replace); }

	bool is_mouse(int dik) { return dik >= mouse_1 && dik <= mouse_8; }

	xr_string key_name(int dik)
	{
		LPCSTR n = hooks.key_name ? hooks.key_name(dik) : nullptr;
		if (n)
			return n;
		string32 buf;
		xr_sprintf(buf, "dik_%d", dik);
		return buf;
	}

	void set_state(int dik, BOOL down)
	{
		if (is_mouse(dik))
			pInput->mcpMouseState[dik - mouse_1] = down;
		else if (dik >= 0 && dik < CInput::COUNT_KB_BUTTONS)
			pInput->mcpKBState[dik] = down;
	}

	void press(int dik)
	{
		for (auto& h : held)
			if (h.dik == dik)
				return;
		held.push_back({ dik, Device.dwFrame });
		set_state(dik, TRUE);
		if (is_mouse(dik))
			pInput->CurrentIR()->IR_OnMousePress(dik - mouse_1);
		else
			pInput->CurrentIR()->IR_OnKeyboardPress(dik);
	}

	void release(int dik)
	{
		auto it = std::find_if(held.begin(), held.end(), [dik](const held_key& h) { return h.dik == dik; });
		if (it == held.end())
			return;
		held.erase(it);
		set_state(dik, FALSE);
		if (is_mouse(dik))
			pInput->CurrentIR()->IR_OnMouseRelease(dik - mouse_1);
		else
			pInput->CurrentIR()->IR_OnKeyboardRelease(dik);
	}

	// Distribute total over ms by elapsed time; returns true when complete
	template <typename T>
	bool progress(op& o, u32 now, T total, T& done, T& out)
	{
		float p = o.ms ? std::min(1.f, float(now - o.t0) / float(o.ms)) : 1.f;
		T target = p >= 1.f ? total : T(total * p);
		out += target - done;
		done = target;
		return p >= 1.f;
	}

	void start(op& o)
	{
		switch (o.kind)
		{
		case o_key:
			if (o.mode == km_up)
				release(o.dik);
			else
				press(o.dik);
			break;
		case o_wheel:
			frame_wheel += o.wheel * wheel_notch;
			break;
		case o_look_at:
		{
			Fvector d = o.target;
			if (!o.target_dir)
				d.sub(Device.vCameraPosition);
			if (d.magnitude() < EPS_L)
				break;
			d.normalize();
			const Fvector& c = Device.vCameraDirection;
			o.yaw = -angle_normalize_signed(d.getH() - c.getH()); // getH grows to the left
			o.pitch = d.getP() - c.getP();
			break;
		}
		case o_cursor:
			o.done_yaw = -1.f; // last moved progress
			if (o.cursor_release)
			{
				if (hooks.cursor_release)
					hooks.cursor_release();
			}
			else if (!hooks.cursor_get || !hooks.cursor_get(o.from.x, o.from.y))
				o.cursor_failed = true;
			break;
		default:
			break;
		}
	}

	bool tick(op& o, u32 now)
	{
		if (o.done)
			return true;
		if (!o.started)
		{
			o.started = true;
			o.t0 = now;
			o.frame0 = Device.dwFrame;
			start(o);
		}

		bool done = true;
		switch (o.kind)
		{
		case o_seq:
			while (o.idx < o.children.size() && tick(o.children[o.idx], now))
				o.idx++;
			done = o.idx >= o.children.size();
			break;
		case o_par:
			for (auto& c : o.children)
				if (!tick(c, now))
					done = false;
			break;
		case o_key:
			if (o.mode == km_tap)
			{
				done = Device.dwFrame > o.frame0 && now - o.t0 >= o.ms;
				if (done)
					release(o.dik);
			}
			break;
		case o_wait:
			done = now - o.t0 >= o.ms;
			break;
		case o_look:
		case o_look_at:
			done = progress(o, now, o.yaw, o.done_yaw, frame_yaw);
			progress(o, now, o.pitch, o.done_pitch, frame_pitch);
			break;
		case o_mouse:
			done = progress(o, now, o.dx, o.done_dx, frame_dx);
			progress(o, now, o.dy, o.done_dy, frame_dy);
			break;
		case o_cursor:
		{
			float p = o.ms ? std::min(1.f, float(now - o.t0) / float(o.ms)) : 1.f;
			if (!o.cursor_release && !o.cursor_failed && p > o.done_yaw)
			{
				o.done_yaw = p;
				if (!hooks.cursor_move || !hooks.cursor_move(o.from.x + (o.target.x - o.from.x) * p, o.from.y + (o.target.y - o.from.y) * p))
					o.cursor_failed = true;
			}
			frame_cursor_failed |= o.cursor_failed;
			done = Device.dwFrame > o.frame0 && p >= 1.f;
			break;
		}
		default:
			break;
		}
		o.done = done;
		return done;
	}

	json held_names()
	{
		json arr = json::array();
		for (auto& h : held)
			arr.push_back(key_name(h.dik).c_str());
		return arr;
	}

	json seq_status(const sequence& s)
	{
		u32 now = clock, frame = Device.dwFrame;
		json r = {
			{ "id", s.id },
			{ "done", s.done },
			{ "started", s.frame0 != 0 },
			{ "elapsed_ms", s.frame0 ? (s.done ? s.t1 : now) - s.t0 : 0 },
			{ "frames", s.frame0 ? (s.done ? s.frame1 : frame) - s.frame0 : 0 },
		};
		if (s.cancelled)
			r["cancelled"] = true;
		if (s.warning.size())
			r["warning"] = s.warning.c_str();
		return r;
	}

	json release_all()
	{
		json released = held_names();
		while (!held.empty())
			release(held.back().dik);
		u32 cancelled = 0;
		for (auto& s : seqs)
			if (!s.done)
			{
				s.done = s.cancelled = true;
				s.t1 = Device.dwTimeContinual;
				s.frame1 = Device.dwFrame;
				cancelled++;
			}
		if (hooks.cursor_release)
			hooks.cursor_release();
		return { { "released", released }, { "cancelled", cancelled } };
	}

	// --- parsing ---

	struct parser
	{
		xr_string err;
		u32 count = 0;

		bool fail(const xr_string& path, const xr_string& msg)
		{
			err = path + ": " + msg;
			return false;
		}

		bool get_ms(const json& j, const xr_string& path, LPCSTR key, u32& out)
		{
			auto it = j.find(key);
			if (it == j.end())
				return true;
			if (!it->is_number() || it->get<double>() < 0 || it->get<double>() > max_ms)
				return fail(path, xr_string("\"") + key + "\" must be a number of ms in 0.." + std::to_string(max_ms).c_str());
			out = u32(it->get<double>() + 0.5);
			return true;
		}

		bool get_num(const json& j, LPCSTR key, float& out)
		{
			auto it = j.find(key);
			if (it == j.end())
				return true;
			if (!it->is_number())
				return false;
			out = it->get<float>();
			return true;
		}

		bool get_vec(const json& j, Fvector& out)
		{
			if (!j.is_array() || j.size() != 3)
				return false;
			for (u32 i = 0; i < 3; i++)
			{
				if (!j[i].is_number())
					return false;
				out[i] = j[i].get<float>();
			}
			return true;
		}

		bool text(const std::string& s, const xr_string& path, op& o)
		{
			o.kind = o_seq;
			int wn = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), nullptr, 0);
			xr_vector<wchar_t> w(wn);
			if (wn)
				MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), w.data(), wn);
			const HKL hkl = GetKeyboardLayout(0);
			for (wchar_t c : w)
			{
				SHORT vks = c == L'\n' ? VK_RETURN : VkKeyScanExW(c, hkl);
				UINT scan = vks == -1 ? 0 : MapVirtualKeyExW(LOBYTE(vks), MAPVK_VK_TO_VSC, hkl);
				if (!scan || (HIBYTE(vks) & ~1))
					return fail(path, xr_string("character U+") + std::to_string((u32)c).c_str()
						+ " cannot be typed with the current keyboard layout (no shift-only key)");
				bool shift = HIBYTE(vks) & 1;
				if (shift)
					o.children.push_back(make_key(DIK_LSHIFT, km_down));
				o.children.push_back(make_key((int)scan, km_tap));
				if (shift)
					o.children.push_back(make_key(DIK_LSHIFT, km_up));
				count += 3;
			}
			return true;
		}

		static op make_key(int dik, key_mode mode)
		{
			op o;
			o.kind = o_key;
			o.dik = dik;
			o.mode = mode;
			return o;
		}

		bool item(const json& j, const xr_string& path, bool parallel, u32 depth, op& o)
		{
			if (++count > max_ops)
				return fail(path, "too many steps");
			if (j.is_array())
			{
				if (depth >= max_depth)
					return fail(path, "nested too deep");
				o.kind = parallel ? o_par : o_seq;
				o.children.resize(j.size());
				for (u32 i = 0; i < j.size(); i++)
					if (!item(j[i], path + "[" + std::to_string(i).c_str() + "]", !parallel, depth + 1, o.children[i]))
						return false;
				return true;
			}
			if (!j.is_object())
				return fail(path, "a step must be an object or an array");

			static const char* verbs[] = { "action", "key", "text", "wheel", "wait", "look", "look_at", "look_dir", "mouse", "cursor" };
			const char* verb = nullptr;
			for (const char* v : verbs)
				if (j.contains(v))
				{
					if (verb)
						return fail(path, xr_string("one verb per step (") + verb + ", " + v + ")");
					verb = v;
				}
			if (!verb)
				return fail(path, "expected one of action, key, text, wheel, wait, look, look_at, look_dir, mouse, cursor");
			for (auto& kv : j.items())
				if (kv.key() != verb && kv.key() != "ms" && kv.key() != "state")
					return fail(path, "unknown field \"" + xr_string(kv.key().c_str()) + "\"");

			const json& v = j[verb];
			xr_string vp = path + "." + verb;
			if (!get_ms(j, path, "ms", o.ms))
				return false;

			if (!strcmp(verb, "action") || !strcmp(verb, "key"))
			{
				if (!v.is_string())
					return fail(vp, "expected a name");
				o.kind = o_key;
				xr_string e; // names are ASCII
				if (!hooks.resolve || !hooks.resolve(v.get_ref<const std::string&>().c_str(), verb[0] == 'a', o.dik, e))
					return fail(vp, hooks.resolve ? e : "game is not ready");
				auto st = j.find("state");
				if (st != j.end())
				{
					if (*st == "down")
						o.mode = km_down;
					else if (*st == "up")
						o.mode = km_up;
					else
						return fail(path, "\"state\" must be \"down\" or \"up\"");
					if (j.contains("ms"))
						return fail(path, "\"ms\" and \"state\" exclude each other");
				}
				return true;
			}
			if (j.contains("state"))
				return fail(path, "\"state\" only applies to action/key");
			if (!strcmp(verb, "text"))
			{
				if (!v.is_string())
					return fail(vp, "expected a string");
				return text(v.get_ref<const std::string&>(), vp, o);
			}
			if (!strcmp(verb, "wheel"))
			{
				if (!v.is_number_integer())
					return fail(vp, "expected notches (integer, + = up)");
				o.kind = o_wheel;
				o.wheel = v.get<int>();
				return true;
			}
			if (!strcmp(verb, "wait"))
			{
				o.kind = o_wait;
				return get_ms(j, path, "wait", o.ms);
			}
			if (!strcmp(verb, "look"))
			{
				o.kind = o_look;
				float yaw = 0, pitch = 0;
				if (!v.is_object() || !get_num(v, "yaw", yaw) || !get_num(v, "pitch", pitch))
					return fail(vp, "expected {\"yaw\":deg,\"pitch\":deg} (+ = right / up)");
				o.yaw = deg2rad(yaw);
				o.pitch = deg2rad(pitch);
				return true;
			}
			if (!strcmp(verb, "look_at") || !strcmp(verb, "look_dir"))
			{
				o.kind = o_look_at;
				o.target_dir = verb[5] == 'd';
				if (!get_vec(v, o.target))
					return fail(vp, "expected [x,y,z]");
				return true;
			}
			if (!strcmp(verb, "cursor"))
			{
				o.kind = o_cursor;
				if (v == "release")
				{
					o.cursor_release = true;
					return true;
				}
				if (!v.is_array() || v.size() != 2 || !v[0].is_number() || !v[1].is_number())
					return fail(vp, "expected [x,y] in UI space (0..1024, 0..768) or \"release\"");
				o.target.set(v[0].get<float>(), v[1].get<float>(), 0.f);
				return true;
			}
			// mouse
			o.kind = o_mouse;
			float dx = 0, dy = 0;
			if (!v.is_object() || !get_num(v, "dx", dx) || !get_num(v, "dy", dy))
				return fail(vp, "expected {\"dx\":px,\"dy\":px} (+ = right / down)");
			o.dx = iFloor(dx + 0.5f);
			o.dy = iFloor(dy + 0.5f);
			return true;
		}
	};

	bool input_run(LPCSTR params, xr_string& out)
	{
		json p = json::parse(params, nullptr, false);
		auto steps = p.is_object() ? p.find("steps") : p.end();
		if (steps == p.end() || !steps->is_array())
		{
			out = "expected params {\"steps\":[...]}";
			return false;
		}
		parser ps;
		sequence s;
		if (!ps.item(*steps, "steps", false, 0, s.root))
		{
			out = ps.err;
			return false;
		}
		s.id = next_id++;
		seqs.push_back(std::move(s));
		out = dump(json{ { "id", seqs.back().id } }).c_str();
		return true;
	}

	bool input_status(LPCSTR params, xr_string& out)
	{
		s64 id;
		if (mcp::get_int_param(params, "id", id))
		{
			for (auto& s : seqs)
				if (s.id == id)
				{
					json r = seq_status(s);
					r["held"] = held_names();
					out = dump(r).c_str();
					return true;
				}
			out = "unknown or expired sequence id";
			return false;
		}
		json running = json::array();
		for (auto& s : seqs)
			if (!s.done)
				running.push_back(seq_status(s));
		out = dump(json{ { "running", running }, { "held", held_names() } }).c_str();
		return true;
	}

	bool input_release_all(LPCSTR, xr_string& out)
	{
		out = dump(release_all()).c_str();
		return true;
	}
}

void init(const game_hooks& h)
{
	if (!Core.ParamsData.test(ECoreParams::mcp_port))
		return;
	hooks = h;
	enabled = true;
	mcp::register_method("input.run", input_run);
	mcp::register_method("input.status", input_status);
	mcp::register_method("input.release_all", input_release_all);
}

void on_frame()
{
	if (!enabled)
		return;

	// A new or lost client never inherits held keys
	u32 conn = mcp::client_id();
	if (conn != last_conn)
	{
		last_conn = conn;
		bool running = std::any_of(seqs.begin(), seqs.end(), [](const sequence& s) { return !s.done; });
		if (!held.empty() || running)
		{
			Msg("* [MCP] input: client changed, releasing %u key(s)", (u32)held.size());
			release_all();
		}
	}
	u32 wall = Device.dwTimeContinual, delta;
	bool run = mcp_time::input_delta(last_wall ? wall - last_wall : 0, delta);
	last_wall = wall;
	if (run)
		clock += delta;
	if (Device.dwPrecacheFrame || !run)
		return;

	u32 now = clock;
	frame_dx = frame_dy = frame_wheel = 0;
	frame_yaw = frame_pitch = 0;
	for (auto& s : seqs)
	{
		if (s.done)
			continue;
		if (!s.frame0)
		{
			s.t0 = now;
			s.frame0 = Device.dwFrame;
		}
		if (tick(s.root, now))
		{
			s.done = true;
			s.t1 = now;
			s.frame1 = Device.dwFrame;
		}
		if ((frame_yaw || frame_pitch) && (!hooks.look || !hooks.look(frame_yaw, frame_pitch)))
			s.warning = "look skipped: no actor";
		if (frame_cursor_failed)
			s.warning = "cursor skipped: no UI";
		frame_yaw = frame_pitch = 0;
		frame_cursor_failed = false;
	}

	IInputReceiver* ir = pInput->CurrentIR();
	if (frame_dx || frame_dy)
		ir->IR_OnMouseMove(frame_dx, frame_dy);
	if (frame_wheel)
		pInput->CurrentIR()->IR_OnMouseWheel(frame_wheel);

	xr_vector<held_key> hold = held;
	for (auto& h : hold)
	{
		if (is_mouse(h.dik))
		{
			if (h.frame < Device.dwFrame)
				pInput->CurrentIR()->IR_OnMouseHold(h.dik - mouse_1);
		}
		else if (h.dik < CInput::COUNT_KB_BUTTONS)
			pInput->CurrentIR()->IR_OnKeyboardHold(h.dik);
	}

	u32 finished = 0;
	for (auto& s : seqs)
		finished += s.done;
	while (finished > keep_done && seqs.front().done)
	{
		seqs.pop_front();
		finished--;
	}
}
}
