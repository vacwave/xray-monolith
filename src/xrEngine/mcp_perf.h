#pragma once

// Windowed frame/Lua timing for the MCP perf.* methods. With no window active every hook is one flag test.
namespace mcp_perf
{
	// Main-thread phases of CRenderDevice::on_idle, in order; time outside them is "other"
	enum phase { ph_before_frame, ph_frame_move, ph_pre_render, ph_limiter, ph_render, ph_present, ph_wait_tasks, ph_inactive_sleep, ph_count };
	// Secondary task sections (any thread)
	enum task { tk_game_thread, tk_scheduler, tk_count };
	// Main-thread sub-sections inside a phase
	enum section { sc_scheduler_rt, sc_count };

	// Flips only on the main thread between frames (no worker task in flight)
	ENGINE_API extern bool active;

	ENGINE_API void frame_begin_impl();
	ENGINE_API void frame_cancel_impl();
	ENGINE_API void mark_impl(phase p);
	// true for the outermost entry/leave of this thread; on the outermost leave ticks = its duration (QPC)
	ENGINE_API bool lua_hook(bool enter, s64& ticks);
	// Whether Lua running on this thread now counts as main-thread Lua (not a worker task)
	ENGINE_API bool lua_on_main();
	// A window frame is open (false before the first frame, during loading and after the window)
	ENGINE_API bool in_frame();
	ENGINE_API s64 ticks_now();
	ENGINE_API double ticks_ms(double ticks);
	ENGINE_API void gc_hook(bool enter, bool worker);
	ENGINE_API void task_hook(task t, bool enter);
	ENGINE_API void section_hook(section s, bool enter);

	// on_idle after the loading early-out / in it; mark: time since the previous mark goes to p
	inline void frame_begin() { if (active) frame_begin_impl(); }
	inline void frame_cancel() { if (active) frame_cancel_impl(); }
	inline void mark(phase p) { if (active) mark_impl(p); }

	// Explicit lua_gc step (worker = the parallel GC task)
	struct gc_scope
	{
		bool on, worker;
		gc_scope(bool w) : on(active), worker(w) { if (on) gc_hook(true, worker); }
		~gc_scope() { if (on) gc_hook(false, worker); }
	};

	// Secondary task section; Lua inside it counts as worker Lua even when PPL runs it inline on the main thread
	struct task_scope
	{
		task t;
		bool on;
		task_scope(task _t) : t(_t), on(active) { if (on) task_hook(t, true); }
		~task_scope() { if (on) task_hook(t, false); }
	};

	// Main-thread sub-section (time and Lua inside it are also part of the enclosing phase)
	struct section_scope
	{
		section s;
		bool on;
		section_scope(section _s) : s(_s), on(active) { if (on) section_hook(s, true); }
		~section_scope() { if (on) section_hook(s, false); }
	};

	// Set by xrGame: Lua heap in bytes (-1 if no state) and the luabind pcall hook switch
	ENGINE_API extern s64 (*lua_heap_bytes)();
	ENGINE_API extern void (*set_lua_hook)(bool on);

	// Lua profiler (xrGame) run by a perf window started with "lua_profile"; called on the main thread with no Lua running
	struct lua_profiler
	{
		bool (*start)(LPCSTR mode, int top, xr_string& err);
		void (*frame)(float frame_ms); // a window frame closed
		void (*stop)();
		void (*report)(double avg_frame_ms, u32 frames, xr_string& out); // JSON object text, after stop
	};
	ENGINE_API extern lua_profiler lua_prof;

	void register_methods();
}
