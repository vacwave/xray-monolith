#include "stdafx.h"
#include "mcp_time.h"
#include "mcp_server.h"
#include "IGame_Level.h"
#include "XR_IOConsole.h"
#include "x_ray.h"

#include <nlohmann/json.hpp>

using json = nlohmann::json;

extern bool IsMainMenuActive();
extern ENGINE_API BOOL bShowPauseString;

namespace mcp_time
{
namespace
{
	// A paused timer's frozen value; shift moves it (and its resume point) by d unscaled ticks
	struct timer_access : CTimer_paused
	{
		static u64 frozen(CTimerBase& t) { return t.*(&timer_access::qwPausedTime); }
		static void shift(CTimerBase& t, u64 d)
		{
			t.*(&timer_access::qwPausedTime) += d;
			t.*(&timer_access::qwPauseAccum) -= d;
		}
	};

	bool enabled = false;
	u32 owner = 0; // client that set the current state

	bool want = false; // MCP pause requested
	bool applied = false; // the pause is in effect (MCP paused the device, or joined a game pause)
	bool menu_prev = false;
	u64 frozen_ticks = 0; // TimerGlobal's frozen value in the last MCP-paused frame
	IGame_Level* level = nullptr;

	u32 steps_left = 0, step_dt = 20, step_id = 0, step_done = 0;
	bool in_step = false; // pauseMngr is unpaused for a step frame (TimerGlobal stays frozen, advanced by dt)
	xr_string step_cancelled; // why the last step was cut short

	bool factor_set = false;
	float factor_orig = 1.f;

	bool engaged() { return want || applied || steps_left || in_step || factor_set; }

	bool loading() { return pApp && pApp->IsLoading(); }

	void set_factor(float f)
	{
		string64 cmd;
		xr_sprintf(cmd, "time_factor %.4f", f);
		Console->Execute(cmd);
	}

	void cancel_steps(LPCSTR why)
	{
		if (steps_left)
			step_cancelled = why;
		steps_left = 0;
		if (in_step)
		{
			in_step = false;
			if (!g_pauseMngr().Paused())
				g_pauseMngr().Pause(true); // TimerGlobal is already paused; back to a full pause
		}
		step_done = step_id;
	}

	void release(LPCSTR why)
	{
		if (want || applied)
			Msg("* [MCP] time: pause released (%s)", why);
		cancel_steps(why);
		if (applied && Device.Paused() && !IsMainMenuActive())
			Device.Pause(FALSE, TRUE, TRUE, "mcp_time");
		want = applied = false;
	}

	void reset(LPCSTR why)
	{
		release(why);
		if (factor_set)
		{
			Msg("* [MCP] time: time_factor restored to %.3f (%s)", factor_orig, why);
			set_factor(factor_orig);
			factor_set = false;
		}
	}

	// A request from another client than the one that set the state starts from a clean state
	void claim()
	{
		u32 c = mcp::client_id();
		if (engaged() && c != owner)
			reset("client changed");
		owner = c;
	}

	json state()
	{
		json r = {
			{ "mcp_paused", want },
			{ "applied", applied },
			{ "paused", Device.Paused() },
			{ "main_menu", IsMainMenuActive() },
			{ "loading", loading() },
			{ "frame", Device.dwFrame },
			{ "time_global", Device.dwTimeGlobal },
			{ "time_async", Device.TimerAsync() },
			{ "time_factor", Device.time_factor() },
			{ "step_id", step_id },
			{ "step_done", step_done },
			{ "steps_left", steps_left },
			{ "step_dt_ms", step_dt },
		};
		if (step_cancelled.size())
			r["step_cancelled"] = step_cancelled.c_str();
		if (factor_set)
			r["time_factor_orig"] = factor_orig;
		return r;
	}

	bool ok(xr_string& out)
	{
		out = state().dump(-1, ' ', false, json::error_handler_t::replace).c_str();
		return true;
	}

	bool fail(xr_string& out, LPCSTR msg)
	{
		out = msg;
		return false;
	}

	bool get_number(const json& p, LPCSTR key, double& v)
	{
		auto it = p.find(key);
		if (it == p.end() || !it->is_number())
			return false;
		v = it->get<double>();
		return true;
	}
}

bool paused() { return want; }

bool input_delta(u32 wall_ms, u32& out_ms)
{
	// Menus and the console run in wall time, plus one frame after they close (a tap that closed one still releases)
	static bool free_prev = false;
	bool free_now = !engaged() || IsMainMenuActive() || (Console && Console->bVisible);
	bool free = free_now || free_prev;
	free_prev = free_now;
	out_ms = wall_ms;
	if (free)
		return true;
	if (Device.Paused())
		return false;
	out_ms = Device.dwTimeDelta;
	return true;
}

void on_frame_time()
{
	if (!enabled || !engaged())
		return;
	if (mcp::client_id() != owner)
	{
		reset("client changed");
		return;
	}
	if ((want || applied) && (g_pGameLevel != level || loading()))
	{
		release(g_pGameLevel ? "level load" : "level unloaded");
		return;
	}

	bool menu = IsMainMenuActive();
	if (steps_left && (menu || !want))
		cancel_steps(menu ? "main menu" : "resumed");

	if (steps_left && (in_step || (applied && Device.Paused())))
	{
		// Step frame: the world runs, TimerGlobal (dwTimeGlobal, TimerAsync, game time) advances by exactly dt
		CTimer_paused* tg = Device.GetTimerGlobal();
		u64 frozen = timer_access::frozen(*tg); // paused: by pauseMngr or since the previous step frame
		if (g_pauseMngr().Paused())
			g_pauseMngr().Pause(false); // sound emitters stay paused
		tg->Pause(true);
		double f = Device.time_factor();
		u64 dt = u64(double(step_dt) * double(CPU::qpc_freq) / 1000.0 / (f > 0 ? f : 1.0) + 0.5);
		timer_access::shift(*tg, frozen + dt - timer_access::frozen(*tg));
		u32 old = Device.dwTimeGlobal;
		Device.dwTimeGlobal = tg->GetElapsed_ms();
		Device.dwTimeDelta = Device.dwTimeGlobal - old;
		Device.fTimeGlobal = tg->GetElapsed_sec();
		Device.fTimeDelta = float(step_dt) / 1000.f;
		in_step = true;
		if (!--steps_left)
			step_cancelled.clear();
		menu_prev = menu;
		return;
	}
	if (in_step)
	{
		// The frame after the last step frame is paused again
		in_step = false;
		if (!g_pauseMngr().Paused())
			g_pauseMngr().Pause(true);
		Device.fTimeDelta = 0.f;
		Device.dwTimeDelta = 0;
		step_done = step_id;
	}

	if (want)
	{
		if (!Device.Paused())
		{
			// Unpaused under us: re-pause after the ESC menu closes (it always unpauses), otherwise the game
			// resumed on purpose (pause key, console load, a script) and that ends the MCP pause
			if (!applied || menu_prev)
			{
				Device.Pause(TRUE, TRUE, TRUE, "mcp_time");
				bShowPauseString = FALSE;
				Device.fTimeDelta = 0.f;
				if (applied)
				{
					// Undo the wall time that ran since the menu unpaused: the world stays exactly where it was
					CTimer_paused* tg = Device.GetTimerGlobal();
					timer_access::shift(*tg, frozen_ticks - timer_access::frozen(*tg));
					Device.dwTimeGlobal = tg->GetElapsed_ms();
					Device.fTimeGlobal = tg->GetElapsed_sec();
					Device.dwTimeDelta = 0;
				}
				applied = true;
			}
			else
				release("resumed by the game");
		}
		else
			applied = true;
		if (Device.Paused())
			frozen_ticks = timer_access::frozen(*Device.GetTimerGlobal());
	}
	else if (applied)
	{
		if (!menu && Device.Paused())
			Device.Pause(FALSE, TRUE, TRUE, "mcp_time");
		applied = false;
	}
	menu_prev = menu;
}

void register_methods()
{
	enabled = true;

	// -> state
	mcp::register_method("time.get", [](LPCSTR, xr_string& out) { return ok(out); });

	// {"on":bool} -> state (takes effect at the next frame: poll time.get for applied)
	mcp::register_method("time.pause", [](LPCSTR params, xr_string& out)
	{
		json p = json::parse(params, nullptr, false);
		auto it = p.is_object() ? p.find("on") : p.end();
		if (it == p.end() || !it->is_boolean())
			return fail(out, "params: {\"on\": true|false}");
		claim();
		bool on = it->get<bool>();
		if (on && (!g_pGameLevel || loading()))
			return fail(out, "no level loaded");
		if (on && !want)
		{
			want = true;
			level = g_pGameLevel;
			menu_prev = IsMainMenuActive();
			step_cancelled.clear();
		}
		else if (!on)
		{
			cancel_steps("resumed");
			want = false;
		}
		return ok(out);
	});

	// {"frames":N (1..100000),"dt_ms":D (1..100, default 20)} -> state; pauses first if needed. Each step frame
	// advances the simulation by exactly D ms; done when step_done == step_id (step_cancelled says why if cut short)
	mcp::register_method("time.step", [](LPCSTR params, xr_string& out)
	{
		json p = json::parse(params, nullptr, false);
		if (!p.is_object())
			p = json::object();
		double frames = 0, dt = 20;
		if (!get_number(p, "frames", frames) || frames < 1 || frames > 100000)
			return fail(out, "frames: 1..100000 required");
		if (p.contains("dt_ms") && (!get_number(p, "dt_ms", dt) || dt < 1 || dt > 100))
			return fail(out, "dt_ms: 1..100");
		claim();
		if (!g_pGameLevel || loading())
			return fail(out, "no level loaded");
		if (IsMainMenuActive())
			return fail(out, "main menu is open");
		if (!want)
		{
			want = true;
			level = g_pGameLevel;
			menu_prev = false;
		}
		if (steps_left)
			cancel_steps("replaced by a new step");
		steps_left = u32(frames);
		step_dt = u32(dt);
		step_id++;
		step_cancelled.clear();
		return ok(out);
	});

	// {"value":F (0.01..100)} -> state; simulation speed (console time_factor: timers, physics, sound pitch)
	mcp::register_method("time.factor", [](LPCSTR params, xr_string& out)
	{
		json p = json::parse(params, nullptr, false);
		double v = 0;
		if (!p.is_object() || !get_number(p, "value", v) || v < 0.01 || v > 100)
			return fail(out, "value: 0.01..100 required");
		claim();
		if (!factor_set)
		{
			factor_orig = Device.time_factor();
			factor_set = true;
		}
		set_factor(float(v));
		return ok(out);
	});
}
}
