#include "stdafx.h"
#include "mcp_perf.h"
#include "mcp_server.h"
#include <atomic>
#include <algorithm>
#include <psapi.h>
#include <nlohmann/json.hpp>

using json = nlohmann::json;

extern int ps_framelimiter;
extern bool IsMainMenuActive();

namespace mcp_perf
{
bool active = false;
s64 (*lua_heap_bytes)() = nullptr;
void (*set_lua_hook)(bool on) = nullptr;

namespace
{
	const int ph_other = ph_count;
	const int ph_slots = ph_count + 1;
	const char* phase_names[ph_slots] = { "before_frame", "frame_move", "pre_render", "limiter", "render", "present", "wait_tasks", "inactive_sleep", "other" };
	const float hitch_min_ms = 33.3f;

	struct tls_state
	{
		int depth; // nested Lua entries; only the outermost one is timed
		int in_task;
		s64 lua_t0, gc_t0, task_t0[tk_count], sec_t0[sc_count], sec_lua0[sc_count];
	};
	thread_local tls_state tls = {};

	s64 qpf = 1;
	DWORD main_tid = 0;

	inline s64 now()
	{
		LARGE_INTEGER t;
		QueryPerformanceCounter(&t);
		return t.QuadPart;
	}
	inline double ms(double ticks) { return ticks * 1000.0 / double(qpf); }
	inline bool on_main() { return !tls.in_task && GetCurrentThreadId() == main_tid; }

	// Window state (main thread)
	float window_s = 0;
	s64 window_t0 = 0, window_t1 = 0; // first frame begin (0 = not yet), finish
	bool pending = false; // a frame is open
	s64 frame_t0 = 0, last_mark = 0;
	bool frame_limited = false, frame_menu = false;

	// Current frame (main thread)
	s64 cur_ph[ph_slots], cur_ph_lua[ph_slots];
	s64 main_lua = 0, main_gc = 0, lua_at_mark = 0;
	u32 main_entries = 0, main_gc_steps = 0;
	s64 cur_sec[sc_count], cur_sec_lua[sc_count];

	// Secondary tasks, drained at every frame close
	std::atomic<s64> w_lua, w_gc, w_task[tk_count];
	std::atomic<u32> w_entries, w_gc_steps;

	// Window totals
	xr_vector<float> frames_ms;
	double sum_ph[ph_slots], sum_ph_lua[ph_slots], sum_sec[sc_count], sum_sec_lua[sc_count];
	double sum_main_lua, sum_main_gc, sum_w_lua, sum_w_gc, sum_w_task[tk_count];
	u64 sum_main_entries, sum_main_gc_steps, sum_w_entries, sum_w_gc_steps;
	u32 limited_frames, menu_frames, loading_breaks;
	s64 heap_first, heap_last, heap_min, heap_max, heap_prev;
	double heap_growth;
	u32 alloc_prev;
	u64 alloc_sum;

	bool has_report = false;
	xr_string report;

	void reset()
	{
		window_t0 = window_t1 = 0;
		pending = false;
		frames_ms.clear();
		for (int i = 0; i < ph_slots; i++)
			sum_ph[i] = sum_ph_lua[i] = 0;
		for (int i = 0; i < sc_count; i++)
			sum_sec[i] = sum_sec_lua[i] = 0;
		sum_main_lua = sum_main_gc = sum_w_lua = sum_w_gc = 0;
		for (int i = 0; i < tk_count; i++)
			sum_w_task[i] = 0;
		sum_main_entries = sum_main_gc_steps = sum_w_entries = sum_w_gc_steps = 0;
		limited_frames = menu_frames = loading_breaks = 0;
		heap_first = heap_last = heap_min = heap_max = heap_prev = -1;
		heap_growth = 0;
		alloc_sum = 0;
	}

	void open_frame(s64 t)
	{
		pending = true;
		frame_t0 = last_mark = t;
		for (int i = 0; i < ph_slots; i++)
			cur_ph[i] = cur_ph_lua[i] = 0;
		for (int i = 0; i < sc_count; i++)
			cur_sec[i] = cur_sec_lua[i] = 0;
		main_lua = main_gc = lua_at_mark = 0;
		main_entries = main_gc_steps = 0;
		w_lua = 0;
		w_gc = 0;
		w_entries = 0;
		w_gc_steps = 0;
		for (int i = 0; i < tk_count; i++)
			w_task[i] = 0;
		frame_menu = Device.Paused() || IsMainMenuActive();
		frame_limited = frame_menu || ps_framelimiter;
		alloc_prev = Memory.stat_calls;

		// No Lua runs here: the previous frame's tasks were joined at the end of on_idle
		s64 heap = lua_heap_bytes ? lua_heap_bytes() : -1;
		if (heap >= 0)
		{
			if (heap_first < 0)
				heap_first = heap_min = heap_max = heap;
			heap_min = std::min(heap_min, heap);
			heap_max = std::max(heap_max, heap);
			if (heap_prev >= 0 && heap > heap_prev)
				heap_growth += double(heap - heap_prev);
			heap_last = heap_prev = heap;
		}
	}

	void close_frame(s64 t)
	{
		pending = false;
		s64 dt = t - frame_t0, known = 0;
		for (int i = 0; i < ph_count; i++)
			known += cur_ph[i];
		cur_ph[ph_other] = dt - known;
		cur_ph_lua[ph_other] = main_lua + main_gc - lua_at_mark;

		frames_ms.push_back(float(ms(double(dt))));
		for (int i = 0; i < ph_slots; i++)
		{
			sum_ph[i] += double(cur_ph[i]);
			sum_ph_lua[i] += double(cur_ph_lua[i]);
		}
		for (int i = 0; i < sc_count; i++)
		{
			sum_sec[i] += double(cur_sec[i]);
			sum_sec_lua[i] += double(cur_sec_lua[i]);
		}
		sum_main_lua += double(main_lua);
		sum_main_gc += double(main_gc);
		sum_main_entries += main_entries;
		sum_main_gc_steps += main_gc_steps;
		sum_w_lua += double(w_lua.load());
		sum_w_gc += double(w_gc.load());
		sum_w_entries += w_entries.load();
		sum_w_gc_steps += w_gc_steps.load();
		for (int i = 0; i < tk_count; i++)
			sum_w_task[i] += double(w_task[i].load());
		limited_frames += frame_limited;
		menu_frames += frame_menu;
		alloc_sum += u32(Memory.stat_calls - alloc_prev);
	}

	inline double r(double v, double scale = 100.0) { return std::round(v * scale) / scale; }

	double percentile(const xr_vector<float>& sorted, double p)
	{
		if (sorted.empty())
			return 0;
		size_t i = (size_t)std::ceil(p * sorted.size());
		return sorted[i ? i - 1 : 0];
	}

	void build_report()
	{
		size_t n = frames_ms.size();
		double nf = n ? double(n) : 1.0;
		double total_ms = 0;
		for (float f : frames_ms)
			total_ms += f;
		double avg = total_ms / nf;
		xr_vector<float> sorted = frames_ms;
		std::sort(sorted.begin(), sorted.end());
		double p50 = percentile(sorted, 0.50);
		double hitch_ms = std::max(2.0 * p50, double(hitch_min_ms));
		u32 hitches = 0;
		for (float f : frames_ms)
			hitches += f > hitch_ms;

		auto per_frame = [&](double ticks) { return ms(ticks) / nf; };
		auto pct = [&](double frame_ms) { return avg > 0 ? r(frame_ms * 100.0 / avg, 10.0) : 0.0; };

		json phases = json::object();
		for (int i = 0; i < ph_slots; i++)
		{
			double m = per_frame(sum_ph[i]);
			phases[phase_names[i]] = { { "ms", r(m, 1000.0) }, { "pct", pct(m) }, { "lua_ms", r(per_frame(sum_ph_lua[i]), 1000.0) } };
		}

		double limiter = per_frame(sum_ph[ph_limiter]);
		double busy = avg - limiter - per_frame(sum_ph[ph_inactive_sleep]);
		double lua_calls = per_frame(sum_main_lua), lua_gc = per_frame(sum_main_gc);
		bool vsync = !!psDeviceFlags.test(rsVSync);
		bool limiter_caps = avg > 0 && limiter / avg > 0.05;
		json capped_by = json::array();
		if (limiter_caps)
			capped_by.push_back(ps_framelimiter ? "r__framelimit" : "menu_or_paused_refresh_cap");
		if (vsync)
			capped_by.push_back("vsync");

		PROCESS_MEMORY_COUNTERS_EX pmc = {};
		pmc.cb = sizeof(pmc);
		K32GetProcessMemoryInfo(GetCurrentProcess(), (PROCESS_MEMORY_COUNTERS*)&pmc, sizeof(pmc));
		const double mb = 1024.0 * 1024.0;
		json heap = nullptr;
		if (heap_first >= 0)
			heap = { { "start", r(heap_first / mb) }, { "end", r(heap_last / mb) }, { "min", r(heap_min / mb) }, { "max", r(heap_max / mb) } };

		json res = {
			{ "frames", n },
			{ "seconds", r(total_ms / 1000.0, 1000.0) },
			{ "wall_seconds", r(ms(double(window_t1 - window_t0)) / 1000.0, 1000.0) },
			{ "loading_breaks", loading_breaks },
			{ "fps", total_ms > 0 ? r(n * 1000.0 / total_ms, 10.0) : 0.0 },
			{ "frame_ms", {
				{ "avg", r(avg, 1000.0) }, { "p50", r(p50, 1000.0) }, { "p95", r(percentile(sorted, 0.95), 1000.0) },
				{ "p99", r(percentile(sorted, 0.99), 1000.0) }, { "max", r(n ? sorted.back() : 0, 1000.0) }, { "min", r(n ? sorted.front() : 0, 1000.0) } } },
			{ "hitches", { { "count", hitches }, { "threshold_ms", r(hitch_ms) } } },
			{ "cap", {
				{ "capped", limiter_caps || vsync }, { "capped_by", capped_by }, { "r__framelimit", ps_framelimiter }, { "vsync", vsync },
				{ "refresh_hz", refresh_rate > 0 ? r(1.0 / refresh_rate, 10.0) : 0.0 },
				{ "limiter_frames", limited_frames }, { "menu_or_paused_frames", menu_frames }, { "limiter_ms", r(limiter, 1000.0) } } },
			{ "main", {
				{ "busy_ms", r(busy, 1000.0) }, { "busy_pct", pct(busy) }, { "busy_fps", busy > 0 ? r(1000.0 / busy, 10.0) : 0.0 },
				{ "lua_ms", r(lua_calls + lua_gc, 1000.0) }, { "lua_pct", pct(lua_calls + lua_gc) }, { "non_lua_busy_ms", r(busy - lua_calls - lua_gc, 1000.0) },
				{ "lua_calls_ms", r(lua_calls, 1000.0) }, { "lua_entries", r(sum_main_entries / nf, 10.0) },
				{ "gc_ms", r(lua_gc, 1000.0) }, { "gc_steps", r(sum_main_gc_steps / nf, 100.0) },
				{ "phases", phases },
				{ "scheduler_rt", { { "ms", r(per_frame(sum_sec[sc_scheduler_rt]), 1000.0) }, { "lua_ms", r(per_frame(sum_sec_lua[sc_scheduler_rt]), 1000.0) } } } } },
			{ "tasks", {
				{ "game_thread_ms", r(per_frame(sum_w_task[tk_game_thread]), 1000.0) }, { "scheduler_ms", r(per_frame(sum_w_task[tk_scheduler]), 1000.0) },
				{ "lua_ms", r(per_frame(sum_w_lua), 1000.0) }, { "lua_entries", r(sum_w_entries / nf, 10.0) },
				{ "gc_ms", r(per_frame(sum_w_gc), 1000.0) }, { "gc_steps", r(sum_w_gc_steps / nf, 100.0) } } },
			{ "memory", {
				{ "working_set_mb", r(pmc.WorkingSetSize / mb) }, { "peak_working_set_mb", r(pmc.PeakWorkingSetSize / mb) }, { "private_mb", r(pmc.PrivateUsage / mb) },
				{ "lua_heap_mb", heap }, { "lua_growth_kb_per_frame", r(heap_growth / 1024.0 / nf) },
				{ "engine_alloc_calls_per_frame", r(alloc_sum / nf, 10.0) } } },
		};
		report = res.dump(-1, ' ', false, json::error_handler_t::replace).c_str();
		has_report = true;
	}

	void finish(s64 t)
	{
		window_t1 = t;
		active = false;
		if (set_lua_hook)
			set_lua_hook(false);
		build_report();
		frames_ms.clear();
		frames_ms.shrink_to_fit();
	}

	bool window_over(s64 t) { return window_t0 && double(t - window_t0) >= double(window_s) * double(qpf); }
}

void frame_begin_impl()
{
	s64 t = now();
	if (pending)
		close_frame(t);
	if (!window_t0)
		window_t0 = t;
	else if (window_over(t))
	{
		finish(t);
		return;
	}
	open_frame(t);
}

void frame_cancel_impl()
{
	s64 t = now();
	if (pending)
	{
		pending = false; // a loading frame is not a frame
		loading_breaks++;
	}
	if (window_over(t))
		finish(t);
}

void mark_impl(phase p)
{
	if (!pending)
		return;
	s64 t = now();
	cur_ph[p] += t - last_mark;
	last_mark = t;
	s64 l = main_lua + main_gc;
	cur_ph_lua[p] += l - lua_at_mark;
	lua_at_mark = l;
}

void lua_hook(bool enter)
{
	if (enter)
	{
		if (tls.depth++ == 0)
			tls.lua_t0 = now();
		return;
	}
	if (--tls.depth)
		return;
	s64 d = now() - tls.lua_t0;
	if (on_main())
	{
		main_lua += d;
		main_entries++;
	}
	else
	{
		w_lua += d;
		w_entries++;
	}
}

void gc_hook(bool enter, bool worker)
{
	if (enter)
	{
		tls.gc_t0 = now();
		return;
	}
	s64 d = now() - tls.gc_t0;
	if (worker || !on_main())
	{
		w_gc += d;
		w_gc_steps++;
	}
	else
	{
		main_gc += d;
		main_gc_steps++;
	}
}

void task_hook(task t, bool enter)
{
	if (enter)
	{
		tls.in_task++;
		tls.task_t0[t] = now();
		return;
	}
	tls.in_task--;
	w_task[t] += now() - tls.task_t0[t];
}

void section_hook(section s, bool enter)
{
	if (!on_main())
		return;
	if (enter)
	{
		tls.sec_t0[s] = now();
		tls.sec_lua0[s] = main_lua + main_gc;
		return;
	}
	cur_sec[s] += now() - tls.sec_t0[s];
	cur_sec_lua[s] += main_lua + main_gc - tls.sec_lua0[s];
}

void register_methods()
{
	// {"seconds":S} starts (or restarts) a window; it ends at the first frame after S seconds
	mcp::register_method("perf.start", [](LPCSTR params, xr_string& out)
	{
		json p = json::parse(params, nullptr, false);
		auto it = p.is_object() ? p.find("seconds") : p.end();
		double s = it != p.end() && it->is_number() ? it->get<double>() : 5.0;
		if (s < 0.1 || s > 120)
		{
			out = "seconds must be 0.1..120";
			return false;
		}
		LARGE_INTEGER f;
		QueryPerformanceFrequency(&f);
		qpf = f.QuadPart;
		main_tid = GetCurrentThreadId();
		reset();
		window_s = float(s);
		has_report = false;
		report.clear();
		// Runs in seqFrame: no Lua is on the stack and no worker task is in flight
		if (!active && set_lua_hook)
			set_lua_hook(true);
		active = true;
		out = make_string("{\"started\":true,\"seconds\":%g}", s).c_str();
		return true;
	});

	// -> the finished window's report, {"running":true,...} while it runs
	mcp::register_method("perf.report", [](LPCSTR, xr_string& out)
	{
		if (active)
		{
			double el = window_t0 ? ms(double(now() - window_t0)) / 1000.0 : 0.0;
			out = make_string("{\"running\":true,\"elapsed_seconds\":%.3f,\"frames\":%u,\"seconds\":%g}", el, (u32)frames_ms.size(), window_s).c_str();
			return true;
		}
		if (!has_report)
		{
			out = "no perf window; call perf.start first";
			return false;
		}
		out = report;
		return true;
	});
}
}
