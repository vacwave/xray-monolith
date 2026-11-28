#include "pch_script.h"
#include "../xrEngine/mcp_perf.h"
#include "../xrEngine/mcp_server.h"
#include "ai_space.h"
#include "script_engine.h"
#include <mutex>
#include <algorithm>
#include <unordered_map>
#include <map>
#include <set>

// Lua profiler for an MCP perf window ("lua_profile" mode, see notes/perf.md):
// - exact: outermost luabind entries (pcall_hook) timed per called function
// - time: LuaJIT timer sampler (jit.profile backport, 1 ms), gated to ticks inside a luabind entry; stacks give
//   self/inclusive per function and the axr_main make_callback listener; scaled to the exact Lua time
// - calls: time + exact call counts per function (call hook; JIT off for the window)
// - alloc: jit.allocprof byte-sampled allocation per function (JIT off for the window), no time sampling
// Lua runs on one thread at a time (phase guarantee), the mutex only guards against surprises.

// LuaJIT internals (lj_profile.c / lj_allocprof.c of the vendored fork)
extern "C" {
int lj_profile_active(void);
extern int lj_allocprof_active;
extern unsigned long long lj_allocprof_seen, lj_allocprof_dropped;
void lj_allocprof_start(lua_State* L, int depth);
void lj_allocprof_stop(lua_State* L);
int lj_allocprof_slots(int stacks);
const void* lj_allocprof_slot(int stacks, int k);
}

namespace
{
	struct alloc_entry // AllocProfEntry
	{
		char key[4096];
		unsigned long long bytes;
	};

	enum { th_main, th_worker, th_count };
	const char* th_names[th_count] = { "main", "worker" };
	enum { vm_n, vm_i, vm_c, vm_g, vm_j, vm_count };
	const char* vm_names[vm_count] = { "jit", "interp", "c", "gc", "compiler" };
	const int max_depth = 48;
	const u32 n_worst = 5;
	const u32 worst_rows = 5;

	struct fn_stat
	{
		xr_string src, name;
		int line, name_rank;
		u32 self[th_count], incl[th_count], gc[th_count], listen[th_count], mark; // samples; listen = as a make_callback listener (inclusive)
		u64 calls[th_count];
		s64 entry_ticks[th_count], entry_max;
		u32 entry_calls[th_count];
		u64 alloc_bytes;
	};

	struct worst_frame
	{
		float ms;
		u32 index;
		double lua_ms;
		xr_vector<std::pair<u32, double>> entries, funcs;
	};

	std::mutex mtx;
	xr_vector<fn_stat> fns;
	std::unordered_map<u64, u32> by_key;
	std::unordered_map<const void*, u32> by_ptr; // luabind entry function -> fn (cleared with the state)

	bool collecting = false; // window open
	lua_State* prof_L = nullptr; // state the sampler / hooks are armed on (null = not armed)
	bool m_time, m_calls, m_alloc;
	xr_string mode_name;
	int top_n = 30;
	bool jit_off = false; // we switched the JIT off on prof_L
	volatile long gate = 0; // threads inside an outermost luabind entry
	u32 mark_id = 0;

	u32 samples[th_count], vm_samples[th_count][vm_count], truncated, sample_cbs;
	s64 lua_ticks[th_count], armed_ticks[th_count], overhead[th_count];
	u64 entries[th_count];
	u32 state_closes, rearm_failures;
	xr_string rearm_error;
	unsigned long long alloc_seen, alloc_dropped;
	u32 frame_no;

	struct frame_entry { u32 fn; s64 ticks; };
	xr_vector<frame_entry> fr_entries;
	xr_vector<std::pair<u32, u32>> fr_samples; // leaf fn, weight
	xr_vector<worst_frame> worst;

	thread_local u32 tl_fn = 0;
	thread_local s64 tl_overhead = 0;

	u64 key_hash(const char* s, int line)
	{
		u64 h = 1469598103934665603ull;
		for (; *s; s++)
		{
			h ^= (u8)*s;
			h *= 1099511628211ull;
		}
		h ^= (u64)(u32)line;
		return h * 1099511628211ull;
	}

	int name_rank(const char* namewhat)
	{
		if (!namewhat || !*namewhat)
			return 0;
		if (!strcmp(namewhat, "global") || !strcmp(namewhat, "method"))
			return 3;
		if (!strcmp(namewhat, "field"))
			return 2;
		return 1; // local, upvalue
	}

	// Caller holds mtx
	u32 intern(const char* src, int line, const char* name = nullptr, const char* namewhat = nullptr)
	{
		u64 h = key_hash(src, line);
		auto it = by_key.find(h);
		u32 i;
		if (it != by_key.end())
			i = it->second;
		else
		{
			i = u32(fns.size());
			fns.emplace_back();
			fn_stat& f = fns.back();
			f.src = src;
			f.line = line;
			f.name_rank = 0;
			f.mark = 0;
			f.entry_max = 0;
			f.alloc_bytes = 0;
			for (int t = 0; t < th_count; t++)
			{
				f.self[t] = f.incl[t] = f.gc[t] = f.listen[t] = f.entry_calls[t] = 0;
				f.calls[t] = 0;
				f.entry_ticks[t] = 0;
			}
			by_key[h] = i;
		}
		if (name && *name)
		{
			int r = name_rank(namewhat);
			fn_stat& f = fns[i];
			if (r > f.name_rank || f.name.empty())
			{
				f.name = name;
				f.name_rank = r;
			}
		}
		return i;
	}

	u32 unknown_fn()
	{
		return intern("?", -1);
	}

	// Identity of the function a luabind entry calls (fn = its stack index, 0 = resume of thread L)
	u32 entry_fn(lua_State* L, int fn)
	{
		lua_Debug ar;
		if (fn > 0)
		{
			const void* p = lua_topointer(L, fn);
			{
				std::lock_guard<std::mutex> g(mtx);
				auto it = by_ptr.find(p);
				if (it != by_ptr.end())
					return it->second;
			}
			// callable tables/userdata (luabind class constructors): getinfo(">S") would not pop a non-function here
			bool is_fn = lua_isfunction(L, fn), ok = false;
			if (is_fn)
			{
				lua_pushvalue(L, fn);
				ok = !!lua_getinfo(L, ">S", &ar);
			}
			std::lock_guard<std::mutex> g(mtx);
			u32 i = ok ? intern(ar.short_src, ar.linedefined) : is_fn ? unknown_fn() : intern("[callable object]", -1);
			by_ptr[p] = i;
			return i;
		}
		// Resume: the coroutine's outermost frame, or its body function on the first resume
		lua_Debug last;
		bool any = false;
		for (int lvl = 0; lua_getstack(L, lvl, &ar); lvl++)
		{
			last = ar;
			any = true;
		}
		bool ok = false;
		if (any)
			ok = !!lua_getinfo(L, "S", &last);
		else if (lua_type(L, 1) == LUA_TFUNCTION)
		{
			lua_pushvalue(L, 1);
			ok = !!lua_getinfo(L, ">S", &last);
		}
		std::lock_guard<std::mutex> g(mtx);
		return ok ? intern(last.short_src, last.linedefined) : unknown_fn();
	}

	int vm_index(int vmstate)
	{
		switch (vmstate)
		{
		case 'N': return vm_n;
		case 'I': return vm_i;
		case 'C': return vm_c;
		case 'G': return vm_g;
		default: return vm_j;
		}
	}

	// make_callback's src:linedefined (0 = unknown: matched by call-site name)
	u64 mc_key = 0;

	// jit.profile callback (sample_cf below): walks the profiled thread's stack, read-only
	void on_sample(lua_State* L, int n, int vmstate)
	{
		if (!mcp_perf::in_frame())
			return;
		s64 t0 = mcp_perf::ticks_now();
		int th = mcp_perf::lua_on_main() ? th_main : th_worker;
		u32 w = n > 0 ? u32(n) : 1;
		u32 stack[max_depth];
		int depth = 0;
		bool trunc = false, dispatched = false;
		u32 listener = 0;
		lua_Debug ar;
		std::lock_guard<std::mutex> g(mtx);
		for (int lvl = 0; lua_getstack(L, lvl, &ar); lvl++)
		{
			if (depth == max_depth)
			{
				trunc = true;
				break;
			}
			if (!lua_getinfo(L, "Sn", &ar) || ar.what[0] == 'C')
				continue;
			u32 f = intern(ar.short_src, ar.linedefined, ar.name, ar.namewhat);
			// innermost axr_main make_callback frame: the frame above it is the listener
			if (!dispatched && (mc_key ? key_hash(ar.short_src, ar.linedefined) == mc_key
				: ar.name && !strcmp(ar.name, "make_callback") && strstr(ar.short_src, "axr_main")))
			{
				dispatched = true;
				listener = depth ? stack[depth - 1] : f;
			}
			stack[depth++] = f;
		}
		samples[th] += w;
		vm_samples[th][vm_index(vmstate)] += w;
		sample_cbs++;
		if (trunc)
			truncated += w;
		if (depth)
		{
			fn_stat& leaf = fns[stack[0]];
			leaf.self[th] += w;
			if (vmstate == 'G')
				leaf.gc[th] += w;
			if (++mark_id == 0)
				mark_id = 1;
			for (int i = 0; i < depth; i++)
			{
				fn_stat& f = fns[stack[i]];
				if (f.mark != mark_id)
				{
					f.mark = mark_id;
					f.incl[th] += w;
				}
			}
			fr_samples.emplace_back(stack[0], w);
		}
		if (dispatched)
			fns[listener].listen[th] += w;
		s64 d = mcp_perf::ticks_now() - t0;
		overhead[th] += d;
		tl_overhead += d;
	}

	// Registered through Lua jit.profile.start: lib_jit calls it on its own thread like a script callback and then aborts any
	// trace being recorded (the profile hook skips lj_trace_ins for the sampled instruction; a raw C callback would not)
	int sample_cf(lua_State* L2)
	{
		lua_State* L = lua_tothread(L2, 1);
		const char* vm = lua_tostring(L2, 3);
		if (L)
			on_sample(L, int(lua_tointeger(L2, 2)), vm ? vm[0] : 'J');
		return 0;
	}
	// Call hook (calls mode)
	void on_call(lua_State* L, lua_Debug* ar)
	{
		if (!mcp_perf::in_frame() || !lua_getinfo(L, "S", ar) || ar->what[0] == 'C')
			return;
		int th = mcp_perf::lua_on_main() ? th_main : th_worker;
		std::lock_guard<std::mutex> g(mtx);
		fns[intern(ar->short_src, ar->linedefined)].calls[th]++;
	}

	bool jit_status(lua_State* L)
	{
		lua_getglobal(L, "jit");
		if (!lua_istable(L, -1))
		{
			lua_pop(L, 1);
			return false;
		}
		lua_getfield(L, -1, "status");
		bool on = lua_isfunction(L, -1) && lua_pcall(L, 0, 1, 0) == 0 && lua_toboolean(L, -1);
		lua_pop(L, 2);
		return on;
	}

	void harvest_alloc()
	{
		int slots = lj_allocprof_slots(0);
		std::lock_guard<std::mutex> g(mtx);
		for (int k = 0; k < slots; k++)
		{
			const alloc_entry* e = (const alloc_entry*)lj_allocprof_slot(0, k);
			if (!e)
				continue;
			// key: "name@src:line" or "src:line"
			xr_string key = e->key, name;
			size_t at = key.find('@');
			if (at != xr_string::npos)
			{
				name = key.substr(0, at);
				key = key.substr(at + 1);
			}
			size_t colon = key.rfind(':');
			int line = colon != xr_string::npos ? atoi(key.c_str() + colon + 1) : -1;
			if (colon != xr_string::npos)
				key.resize(colon);
			// call-site name of unknown kind: rank it like a local so the module scan may replace it
			fns[intern(key.c_str(), line, name.c_str(), "local")].alloc_bytes += e->bytes;
		}
		alloc_seen += lj_allocprof_seen;
		alloc_dropped += lj_allocprof_dropped;
	}

	// Runs a chunk with an optional C function argument; false + err on error
	bool run_chunk(lua_State* L, const char* code, lua_CFunction arg, xr_string* err)
	{
		int top = lua_gettop(L);
		bool ok = !luaL_loadstring(L, code);
		if (ok)
		{
			if (arg)
				lua_pushcfunction(L, arg);
			ok = !lua_pcall(L, arg ? 1 : 0, 0, 0);
		}
		if (!ok && err)
			*err = lua_isstring(L, -1) ? lua_tostring(L, -1) : "Lua error";
		lua_settop(L, top);
		return ok;
	}

	// src:linedefined of axr_main.make_callback (rawget, no lazy script load)
	u64 make_callback_key(lua_State* L)
	{
		u64 k = 0;
		int top = lua_gettop(L);
		lua_pushstring(L, "axr_main");
		lua_rawget(L, LUA_GLOBALSINDEX);
		if (lua_istable(L, -1))
		{
			lua_pushstring(L, "make_callback");
			lua_rawget(L, -2);
			lua_Debug ar;
			if (lua_isfunction(L, -1) && lua_getinfo(L, ">S", &ar))
				k = key_hash(ar.short_src, ar.linedefined);
		}
		lua_settop(L, top);
		return k;
	}

	bool arm(lua_State* L, xr_string& err)
	{
		if (lj_profile_active() || lj_allocprof_active)
		{
			err = "jit.profile / jit.allocprof is already running (a script profiler such as perf_luaprof?)";
			return false;
		}
		if (m_calls && lua_gethook(L))
		{
			err = "a Lua debug hook is already set (debugger?)";
			return false;
		}
		jit_off = false;
		if ((m_calls || m_alloc) && jit_status(L))
		{
			// Hooks / the allocation trap do not run inside compiled traces
			luaJIT_setmode(L, 0, LUAJIT_MODE_ENGINE | LUAJIT_MODE_FLUSH);
			luaJIT_setmode(L, 0, LUAJIT_MODE_ENGINE | LUAJIT_MODE_OFF);
			jit_off = true;
		}
		if (m_calls)
			lua_sethook(L, on_call, LUA_MASKCALL, 0);
		if (m_alloc)
			lj_allocprof_start(L, 1);
		else
		{
			mc_key = make_callback_key(L);
			luaJIT_profile_gate(&gate);
			if (!run_chunk(L, "local p = jit.profile or require('jit.profile') p.start('i1', ...)", sample_cf, &err))
			{
				luaJIT_profile_gate(nullptr);
				if (m_calls)
					lua_sethook(L, nullptr, 0, 0);
				if (jit_off)
					luaJIT_setmode(L, 0, LUAJIT_MODE_ENGINE | LUAJIT_MODE_ON);
				return false;
			}
		}
		prof_L = L;
		return true;
	}

	void disarm()
	{
		lua_State* L = prof_L;
		if (!L)
			return;
		if (m_alloc)
		{
			harvest_alloc();
			lj_allocprof_stop(L);
		}
		else
		{
			run_chunk(L, "local p = jit.profile or require('jit.profile') p.stop()", nullptr, nullptr);
			luaJIT_profile_gate(nullptr);
		}
		if (m_calls)
			lua_sethook(L, nullptr, 0, 0);
		if (jit_off)
			luaJIT_setmode(L, 0, LUAJIT_MODE_ENGINE | LUAJIT_MODE_ON);
		jit_off = false;
		prof_L = nullptr;
	}

	lua_State* game_lua()
	{
		return g_ai_space ? ai().script_engine().lua() : nullptr;
	}

	void reset()
	{
		fns.clear();
		by_key.clear();
		by_ptr.clear();
		fr_entries.clear();
		fr_samples.clear();
		worst.clear();
		mark_id = 0;
		for (int t = 0; t < th_count; t++)
		{
			samples[t] = 0;
			for (int v = 0; v < vm_count; v++)
				vm_samples[t][v] = 0;
			lua_ticks[t] = armed_ticks[t] = overhead[t] = 0;
			entries[t] = 0;
		}
		truncated = sample_cbs = state_closes = rearm_failures = frame_no = 0;
		rearm_error.clear();
		alloc_seen = alloc_dropped = 0;
		gate = 0;
	}

	// mcp_perf::lua_profiler

	bool prof_start(LPCSTR mode, int top, xr_string& err)
	{
		m_time = !strcmp(mode, "time");
		m_calls = !strcmp(mode, "calls");
		m_alloc = !strcmp(mode, "alloc");
		if (!m_time && !m_calls && !m_alloc)
		{
			err = "lua_profile must be \"time\", \"calls\" or \"alloc\"";
			return false;
		}
		lua_State* L = game_lua();
		if (!L)
		{
			err = "no Lua state";
			return false;
		}
		reset();
		mode_name = mode;
		top_n = top;
		if (!arm(L, err))
			return false;
		collecting = true;
		return true;
	}

	void prof_frame(float frame_ms)
	{
		// The state was closed (level change / load): arm again on the new one
		if (collecting && !prof_L && state_closes)
		{
			lua_State* L = game_lua();
			if (L && !arm(L, rearm_error))
				rearm_failures++;
		}
		std::lock_guard<std::mutex> g(mtx);
		frame_no++;
		if (worst.size() < n_worst || frame_ms > worst.back().ms)
		{
			worst_frame wf;
			wf.ms = frame_ms;
			wf.index = frame_no;
			std::unordered_map<u32, s64> e;
			s64 lt = 0;
			for (const frame_entry& fe : fr_entries)
			{
				e[fe.fn] += fe.ticks;
				lt += fe.ticks;
			}
			wf.lua_ms = mcp_perf::ticks_ms(double(lt));
			for (auto& kv : e)
				wf.entries.emplace_back(kv.first, mcp_perf::ticks_ms(double(kv.second)));
			std::unordered_map<u32, u32> s;
			u32 st = 0;
			for (auto& fs : fr_samples)
			{
				s[fs.first] += fs.second;
				st += fs.second;
			}
			for (auto& kv : s)
				wf.funcs.emplace_back(kv.first, st ? wf.lua_ms * kv.second / st : 0.0);
			auto by_ms = [](const std::pair<u32, double>& a, const std::pair<u32, double>& b) { return a.second > b.second; };
			std::sort(wf.entries.begin(), wf.entries.end(), by_ms);
			std::sort(wf.funcs.begin(), wf.funcs.end(), by_ms);
			if (wf.entries.size() > worst_rows)
				wf.entries.resize(worst_rows);
			if (wf.funcs.size() > worst_rows)
				wf.funcs.resize(worst_rows);
			worst.push_back(std::move(wf));
			std::sort(worst.begin(), worst.end(), [](const worst_frame& a, const worst_frame& b) { return a.ms > b.ms; });
			if (worst.size() > n_worst)
				worst.pop_back();
		}
		fr_entries.clear();
		fr_samples.clear();
	}

	void prof_stop()
	{
		collecting = false;
		disarm();
	}

	// -- report

	void add(xr_string& s, const char* fmt, ...)
	{
		char buf[512];
		va_list a;
		va_start(a, fmt);
		int n = vsnprintf(buf, sizeof(buf), fmt, a);
		va_end(a);
		if (n > 0)
			s.append(buf, std::min(n, int(sizeof(buf) - 1)));
	}

	const char* base_name(const xr_string& src)
	{
		const char* s = src.c_str();
		const char* b = std::max(strrchr(s, '\\'), strrchr(s, '/'));
		return b ? b + 1 : s;
	}

	xr_string fn_label(u32 i)
	{
		const fn_stat& f = fns[i];
		if (f.line < 0)
			return base_name(f.src);
		return make_string("%s:%d", base_name(f.src), f.line).c_str();
	}

	// Names of module-level functions: _G[module] (rawget, no lazy script load) scanned once per module
	std::map<xr_string, std::unordered_map<int, xr_string>> scanned;

	// listener src:linedefined -> "callback|callback" from axr_main's intercepts upvalue (name -> {func|table = true})
	std::unordered_map<u64, xr_string> listener_cbs;

	void scan_intercepts(lua_State* L)
	{
		listener_cbs.clear();
		if (!L)
			return;
		int top = lua_gettop(L);
		lua_pushstring(L, "axr_main");
		lua_rawget(L, LUA_GLOBALSINDEX);
		if (lua_istable(L, -1))
		{
			lua_pushstring(L, "make_callback");
			lua_rawget(L, -2);
			int mc = lua_gettop(L);
			for (int n = 1; lua_isfunction(L, mc); n++)
			{
				const char* un = lua_getupvalue(L, mc, n);
				if (!un)
					break;
				if (strcmp(un, "intercepts") || !lua_istable(L, -1))
				{
					lua_pop(L, 1);
					continue;
				}
				std::map<u64, std::set<xr_string>> names;
				lua_pushnil(L);
				while (lua_next(L, -2)) // name -> set
				{
					if (lua_type(L, -2) == LUA_TSTRING && lua_istable(L, -1))
					{
						xr_string cb = lua_tostring(L, -2);
						lua_pushnil(L);
						while (lua_next(L, -2)) // func_or_table -> true
						{
							lua_pushvalue(L, -2);
							if (lua_istable(L, -1)) // table listener: its method named after the callback
							{
								lua_pushstring(L, cb.c_str());
								lua_rawget(L, -2);
								lua_remove(L, -2);
							}
							lua_Debug ar;
							if (lua_isfunction(L, -1) && lua_getinfo(L, ">S", &ar))
								names[key_hash(ar.short_src, ar.linedefined)].insert(cb);
							else
								lua_pop(L, 1);
							lua_pop(L, 1);
						}
					}
					lua_pop(L, 1);
				}
				for (auto& kv : names)
				{
					xr_string j;
					for (auto& s : kv.second)
						j += (j.empty() ? "" : "|") + s;
					listener_cbs[kv.first] = j;
				}
				break;
			}
		}
		lua_settop(L, top);
	}
	void scan_table(lua_State* L, const char* prefix, const char* want_src, std::unordered_map<int, xr_string>& out)
	{
		lua_pushnil(L);
		while (lua_next(L, -2))
		{
			if (lua_type(L, -2) == LUA_TSTRING && lua_type(L, -1) == LUA_TFUNCTION)
			{
				lua_Debug ar;
				lua_pushvalue(L, -1);
				if (lua_getinfo(L, ">S", &ar) && !strcmp(ar.short_src, want_src) && !out.count(ar.linedefined))
					out[ar.linedefined] = make_string("%s%s", prefix, lua_tostring(L, -2)).c_str();
			}
			lua_pop(L, 1);
		}
	}

	void resolve_name(u32 i)
	{
		fn_stat& f = fns[i];
		if (f.name_rank >= 2 || f.line < 0)
			return;
		lua_State* L = game_lua();
		if (!L)
			return;
		auto it = scanned.find(f.src);
		if (it == scanned.end())
		{
			it = scanned.emplace(f.src, std::unordered_map<int, xr_string>()).first;
			xr_string mod = base_name(f.src);
			size_t dot = mod.rfind('.');
			if (dot != xr_string::npos)
				mod.resize(dot);
			int top = lua_gettop(L);
			lua_pushstring(L, mod.c_str());
			lua_rawget(L, LUA_GLOBALSINDEX);
			if (lua_istable(L, -1))
				scan_table(L, (mod + ".").c_str(), f.src.c_str(), it->second);
			lua_settop(L, top);
			// globals defined by this chunk (e.g. lua.exec code)
			lua_pushvalue(L, LUA_GLOBALSINDEX);
			scan_table(L, "", f.src.c_str(), it->second);
			lua_settop(L, top);
		}
		auto n = it->second.find(f.line);
		if (n != it->second.end())
		{
			f.name = n->second;
			f.name_rank = 3;
		}
	}

	void add_fn(xr_string& s, u32 i)
	{
		resolve_name(i);
		const fn_stat& f = fns[i];
		add(s, "\"fn\":%s", mcp::to_json_string(fn_label(i).c_str()).c_str());
		// rank 1 = the caller's local/upvalue name (e.g. make_callback's func_or_userdata), not the function's own
		if (!f.name.empty())
			add(s, ",\"name\":%s", mcp::to_json_string(f.name_rank == 1 ? (f.name + " (local)").c_str() : f.name.c_str()).c_str());
	}

	inline double rnd(double v, double sc = 1000.0) { return std::round(v * sc) / sc; }

	template <typename F>
	xr_vector<u32> top_by(F key)
	{
		xr_vector<u32> v;
		for (u32 i = 0; i < fns.size(); i++)
			if (key(i) > 0)
				v.push_back(i);
		std::sort(v.begin(), v.end(), [&](u32 a, u32 b) { return key(a) > key(b); });
		if (v.size() > size_t(top_n))
			v.resize(top_n);
		return v;
	}

	void report_thread(xr_string& s, int th, double nf, double avg)
	{
		double lua_ms = mcp_perf::ticks_ms(double(lua_ticks[th])) / nf;
		double armed_ms = mcp_perf::ticks_ms(double(armed_ticks[th]));
		double per_sample = samples[th] ? armed_ms / samples[th] : 0.0; // ms of exact Lua time per sample
		auto pct = [&](double ms) { return lua_ms > 0 ? rnd(ms * 100.0 / lua_ms, 10.0) : 0.0; };
		auto fpct = [&](double ms) { return avg > 0 ? rnd(ms * 100.0 / avg, 10.0) : 0.0; };
		auto sms = [&](double smp) { return smp * per_sample / nf; };

		add(s, "\"%s\":{\"lua_ms\":%.3f,\"frame_pct\":%.1f,\"entries_per_frame\":%.1f,\"samples\":%u,\"sampler_ms\":%.4f",
			th_names[th], lua_ms, fpct(lua_ms), entries[th] / nf, samples[th], mcp_perf::ticks_ms(double(overhead[th])) / nf);
		if (samples[th])
		{
			s += ",\"vmstate_pct\":{";
			for (int v = 0; v < vm_count; v++)
				add(s, "%s\"%s\":%.1f", v ? "," : "", vm_names[v], vm_samples[th][v] * 100.0 / samples[th]);
			s += "}";
		}

		// sampled functions
		if (!m_alloc)
		{
			s += ",\"functions\":[";
			auto v = top_by([&](u32 i) { return double(fns[i].self[th]); });
			for (size_t k = 0; k < v.size(); k++)
			{
				const fn_stat& f = fns[v[k]];
				s += k ? ",{" : "{";
				add_fn(s, v[k]);
				double self = sms(f.self[th]), incl = sms(f.incl[th]);
				add(s, ",\"self_ms\":%.3f,\"self_pct\":%.1f,\"frame_pct\":%.1f,\"total_ms\":%.3f,\"total_pct\":%.1f,\"samples\":%u",
					self, pct(self), fpct(self), incl, pct(incl), f.self[th]);
				if (f.gc[th])
					add(s, ",\"gc_pct\":%.1f", f.gc[th] * 100.0 / f.self[th]);
				if (m_calls)
					add(s, ",\"calls_per_frame\":%.2f", f.calls[th] / nf);
				s += "}";
			}
			s += "]";

			// call-tree view: inclusive time (wrappers, dispatchers, entry functions)
			s += ",\"functions_by_total\":[";
			auto tv = top_by([&](u32 i) { return double(fns[i].incl[th]); });
			if (tv.size() > 15)
				tv.resize(15);
			for (size_t k = 0; k < tv.size(); k++)
			{
				const fn_stat& f = fns[tv[k]];
				s += k ? ",{" : "{";
				add_fn(s, tv[k]);
				double self = sms(f.self[th]), incl = sms(f.incl[th]);
				add(s, ",\"total_ms\":%.3f,\"total_pct\":%.1f,\"self_ms\":%.3f}", incl, pct(incl), self);
			}
			s += "]";

			// files (self time)
			std::unordered_map<xr_string, u32> files;
			for (const fn_stat& f : fns)
				if (f.self[th])
					files[base_name(f.src)] += f.self[th];
			std::vector<std::pair<xr_string, u32>> fv(files.begin(), files.end());
			std::sort(fv.begin(), fv.end(), [](const std::pair<xr_string, u32>& a, const std::pair<xr_string, u32>& b) { return a.second > b.second; });
			if (fv.size() > size_t(top_n))
				fv.resize(top_n);
			s += ",\"files\":[";
			for (size_t k = 0; k < fv.size(); k++)
			{
				double ms = sms(fv[k].second);
				add(s, "%s{\"file\":%s,\"self_ms\":%.3f,\"self_pct\":%.1f}", k ? "," : "", mcp::to_json_string(fv[k].first.c_str()).c_str(), ms, pct(ms));
			}
			s += "]";

			// make_callback listeners grouped by the callback names they are registered for
			std::map<xr_string, std::vector<std::pair<u32, u32>>> groups;
			for (u32 i = 0; i < fns.size(); i++)
				if (fns[i].listen[th])
				{
					auto it = listener_cbs.find(key_hash(fns[i].src.c_str(), fns[i].line));
					groups[it != listener_cbs.end() ? it->second : xr_string("(dispatch iterator or unregistered)")].emplace_back(i, fns[i].listen[th]);
				}
			std::vector<std::pair<u32, xr_string>> gv;
			for (auto& kv : groups)
			{
				u32 sum = 0;
				for (auto& l : kv.second)
					sum += l.second;
				gv.emplace_back(sum, kv.first);
			}
			std::sort(gv.begin(), gv.end(), [](const std::pair<u32, xr_string>& a, const std::pair<u32, xr_string>& b) { return a.first > b.first; });
			if (gv.size() > size_t(top_n))
				gv.resize(top_n);
			s += ",\"callbacks\":[";
			for (size_t k = 0; k < gv.size(); k++)
			{
				double ms = sms(gv[k].first);
				add(s, "%s{\"callback\":%s,\"ms\":%.3f,\"pct\":%.1f,\"listeners\":[", k ? "," : "", mcp::to_json_string(gv[k].second.c_str()).c_str(), ms, pct(ms));
				auto& lv = groups[gv[k].second];
				std::sort(lv.begin(), lv.end(), [](const std::pair<u32, u32>& a, const std::pair<u32, u32>& b) { return a.second > b.second; });
				if (lv.size() > 10)
					lv.resize(10);
				for (size_t j = 0; j < lv.size(); j++)
				{
					s += j ? ",{" : "{";
					add_fn(s, lv[j].first);
					double lms = sms(lv[j].second);
					add(s, ",\"ms\":%.3f,\"pct\":%.1f}", lms, pct(lms));
				}
				s += "]}";
			}
			s += "]";
		}

		// exact entry points
		s += ",\"entries\":[";
		auto ev = top_by([&](u32 i) { return double(fns[i].entry_ticks[th]); });
		for (size_t k = 0; k < ev.size(); k++)
		{
			const fn_stat& f = fns[ev[k]];
			double ms = mcp_perf::ticks_ms(double(f.entry_ticks[th])) / nf;
			s += k ? ",{" : "{";
			add_fn(s, ev[k]);
			add(s, ",\"ms\":%.3f,\"pct\":%.1f,\"calls_per_frame\":%.2f,\"max_ms\":%.3f}", ms, pct(ms), f.entry_calls[th] / nf,
				mcp_perf::ticks_ms(double(f.entry_max)));
		}
		s += "]}";
	}

	void prof_report(double avg, u32 frames, xr_string& s)
	{
		std::lock_guard<std::mutex> g(mtx);
		double nf = frames ? double(frames) : 1.0;
		scanned.clear();
		scan_intercepts(game_lua());
		s = "{";
		add(s, "\"mode\":\"%s\",\"frames\":%u,\"top\":%d", mode_name.c_str(), frames, top_n);
		add(s, ",\"method\":%s", mcp::to_json_string(m_alloc
			? "entries: exact (luabind pcall/resume); allocation: jit.allocprof byte-sampled (mean 8 KB), JIT off for the window"
			: "entries: exact (luabind pcall/resume); functions/files/callbacks: sampled (LuaJIT timer ~1-2 ms, only ticks inside a luabind entry), scaled to the exact Lua time").c_str());
		s += ",\"threads\":{";
		report_thread(s, th_main, nf, avg);
		if (lua_ticks[th_worker] || samples[th_worker])
		{
			s += ",";
			report_thread(s, th_worker, nf, avg);
		}
		s += "}";

		if (m_alloc)
		{
			double total = 0;
			for (const fn_stat& f : fns)
				total += double(f.alloc_bytes);
			auto v = top_by([&](u32 i) { return double(fns[i].alloc_bytes); });
			add(s, ",\"alloc\":{\"kb_per_frame\":%.2f,\"sampled_kb_per_frame\":%.2f,\"dropped_kb\":%.1f,\"functions\":[",
				alloc_seen / 1024.0 / nf, total / 1024.0 / nf, alloc_dropped / 1024.0);
			for (size_t k = 0; k < v.size(); k++)
			{
				const fn_stat& f = fns[v[k]];
				s += k ? ",{" : "{";
				add_fn(s, v[k]);
				add(s, ",\"kb_per_frame\":%.2f,\"pct\":%.1f}", f.alloc_bytes / 1024.0 / nf, total > 0 ? f.alloc_bytes * 100.0 / total : 0.0);
			}
			std::unordered_map<xr_string, double> files;
			for (const fn_stat& f : fns)
				if (f.alloc_bytes)
					files[base_name(f.src)] += double(f.alloc_bytes);
			std::vector<std::pair<xr_string, double>> fv(files.begin(), files.end());
			std::sort(fv.begin(), fv.end(), [](const std::pair<xr_string, double>& a, const std::pair<xr_string, double>& b) { return a.second > b.second; });
			if (fv.size() > size_t(top_n))
				fv.resize(top_n);
			s += "],\"files\":[";
			for (size_t k = 0; k < fv.size(); k++)
				add(s, "%s{\"file\":%s,\"kb_per_frame\":%.2f,\"pct\":%.1f}", k ? "," : "", mcp::to_json_string(fv[k].first.c_str()).c_str(),
					fv[k].second / 1024.0 / nf, total > 0 ? fv[k].second * 100.0 / total : 0.0);
			s += "]}";
		}

		s += ",\"worst_frames\":[";
		for (size_t k = 0; k < worst.size(); k++)
		{
			const worst_frame& w = worst[k];
			add(s, "%s{\"frame\":%u,\"frame_ms\":%.3f,\"lua_ms\":%.3f,\"entries\":[", k ? "," : "", w.index, w.ms, w.lua_ms);
			for (size_t j = 0; j < w.entries.size(); j++)
			{
				s += j ? ",{" : "{";
				add_fn(s, w.entries[j].first);
				add(s, ",\"ms\":%.3f}", w.entries[j].second);
			}
			s += "],\"functions\":[";
			for (size_t j = 0; j < w.funcs.size(); j++)
			{
				s += j ? ",{" : "{";
				add_fn(s, w.funcs[j].first);
				add(s, ",\"ms\":%.3f}", w.funcs[j].second);
			}
			s += "]}";
		}
		s += "]";

		double oh = mcp_perf::ticks_ms(double(overhead[th_main] + overhead[th_worker])) / nf;
		double lua_all = mcp_perf::ticks_ms(double(lua_ticks[th_main] + lua_ticks[th_worker])) / nf;
		add(s, ",\"overhead\":{\"sampler_ms_per_frame\":%.4f,\"sampler_pct_of_lua\":%.1f,\"sample_callbacks\":%u,\"truncated_stacks\":%u}",
			oh, lua_all > 0 ? oh * 100.0 / lua_all : 0.0, sample_cbs, truncated);
		add(s, ",\"lua_state_closes\":%u", state_closes);
		if (rearm_failures)
			add(s, ",\"rearm_error\":%s", mcp::to_json_string(rearm_error.c_str()).c_str());
		s += "}";
		scanned.clear();
	}
}

// luabind pcall/resume hook (replaces mcp_perf::lua_hook as pcall_hook while a perf window runs)
void mcp_lua_profiler_hook(lua_State* L, int fn, bool enter)
{
	s64 t = 0;
	if (!mcp_perf::lua_hook(enter, t) || !collecting)
		return;
	if (enter)
	{
		InterlockedIncrement(&gate);
		tl_overhead = 0;
		tl_fn = entry_fn(L, fn);
		return;
	}
	InterlockedDecrement(&gate);
	if (!mcp_perf::in_frame()) // loading frames are not window frames
		return;
	s64 d = std::max(t - tl_overhead, s64(0)); // without the sampler's own time
	int th = mcp_perf::lua_on_main() ? th_main : th_worker;
	std::lock_guard<std::mutex> g(mtx);
	if (tl_fn >= fns.size())
		return;
	lua_ticks[th] += d;
	if (prof_L)
		armed_ticks[th] += d;
	entries[th]++;
	fn_stat& f = fns[tl_fn];
	f.entry_ticks[th] += d;
	f.entry_calls[th]++;
	f.entry_max = std::max(f.entry_max, d);
	fr_entries.push_back({ tl_fn, d });
}

// CScriptStorage: L is about to be closed (level change / load / exit)
void mcp_lua_profiler_close(lua_State* L)
{
	if (!collecting && prof_L != L)
		return;
	if (prof_L == L)
		disarm();
	std::lock_guard<std::mutex> g(mtx);
	by_ptr.clear();
	state_closes++;
}

void mcp_register_lua_profiler()
{
	mcp_perf::lua_prof.start = prof_start;
	mcp_perf::lua_prof.frame = prof_frame;
	mcp_perf::lua_prof.stop = prof_stop;
	mcp_perf::lua_prof.report = prof_report;
}
