#include "stdafx.h"
#include "../xrEngine/mcp_input.h"
#include "xr_level_controller.h"
#include "Actor.h"
#include "../xrEngine/CameraBase.h"

// Game side of MCP input (xrEngine/mcp_input.cpp): bind/key names and the actor camera
extern _keyboard keyboards[];

namespace
{
	const char* action_aliases[][2] = {
		{ "fire", "wpn_fire" }, { "aim", "wpn_zoom" }, { "zoom", "wpn_zoom" }, { "reload", "wpn_reload" },
		{ "pda", "active_jobs" }, { "sprint", "sprint_toggle" }, { "walk", "accel" }, { "menu", "quit" },
		{ "strafe_left", "lstrafe" }, { "strafe_right", "rstrafe" }, { "lean_left", "llookout" }, { "lean_right", "rlookout" },
		{ "detector", "show_detector" }, { "flashlight", "torch" },
	};
	const char* key_aliases[][2] = {
		{ "ESC", "kESCAPE" }, { "ENTER", "kRETURN" }, { "BACKSPACE", "kBACK" }, { "CTRL", "kLCONTROL" },
		{ "SHIFT", "kLSHIFT" }, { "ALT", "kLMENU" }, { "PGUP", "kPRIOR" }, { "PGDN", "kNEXT" },
		{ "CAPSLOCK", "kCAPITAL" }, { "TILDE", "kGRAVE" }, { "LMB", "mouse1" }, { "RMB", "mouse2" }, { "MMB", "mouse3" },
	};

	// "kESCAPE" -> "ESCAPE" (the names agents type); mouse1.. and kCUSTOMn stay as they are
	LPCSTR short_name(LPCSTR n)
	{
		return n[0] == 'k' && (isupper(n[1]) || isdigit(n[1])) && strncmp(n, "kCUSTOM", 7) ? n + 1 : n;
	}

	_keyboard* find_key(LPCSTR name)
	{
		for (auto& a : key_aliases)
			if (!stricmp(name, a[0]))
				name = a[1];
		for (int i = 0; keyboards[i].key_name; i++)
		{
			LPCSTR k = keyboards[i].key_name;
			if (!stricmp(name, k) || !stricmp(name, short_name(k)))
				return &keyboards[i];
		}
		return nullptr;
	}

	bool resolve(LPCSTR name, bool action, int& dik, xr_string& err)
	{
		if (!action)
		{
			if (_keyboard* k = find_key(name))
			{
				dik = k->dik;
				return true;
			}
			err = xr_string("unknown key \"") + name + "\"; keys:";
			for (int i = 0; keyboards[i].key_name; i++)
				if (strncmp(keyboards[i].key_name, "kCUSTOM", 7))
					err = err + " " + short_name(keyboards[i].key_name);
			err += " kCUSTOM0..99; aliases:";
			for (auto& a : key_aliases)
				err = err + " " + a[0];
			return false;
		}

		LPCSTR bind = name;
		for (auto& a : action_aliases)
			if (!stricmp(name, a[0]))
				bind = a[1];
		for (int i = 0; actions[i].action_name; i++)
			if (!stricmp(bind, actions[i].action_name))
			{
				dik = get_action_dik(actions[i].id);
				if (dik)
					return true;
				err = xr_string("action \"") + name + "\" is not bound to a key";
				return false;
			}
		err = xr_string("unknown action \"") + name + "\"; actions:";
		for (int i = 0; actions[i].action_name; i++)
			err = err + " " + actions[i].action_name;
		err += "; aliases:";
		for (auto& a : action_aliases)
			err = err + " " + a[0] + "=" + a[1];
		return false;
	}

	LPCSTR key_name(int dik)
	{
		LPCSTR n = dik_to_keyname(dik, false);
		return n ? short_name(n) : nullptr;
	}

	// CCameraBase::Move: kRIGHT grows yaw (turn right), kDOWN lowers pitch (look up); 0 means "default speed", so skip it
	bool look(float yaw, float pitch)
	{
		if (!g_actor)
			return false;
		CCameraBase* c = g_actor->cam_Active();
		if (yaw != 0.f)
			c->Move(yaw > 0.f ? kRIGHT : kLEFT, _abs(yaw));
		if (pitch != 0.f)
			c->Move(pitch > 0.f ? kDOWN : kUP, _abs(pitch));
		return true;
	}
}

void mcp_register_input()
{
	mcp_input::init({ resolve, key_name, look });
}
