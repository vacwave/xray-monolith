#include "pch_script.h"
#include "../xrEngine/mcp_server.h"
#include "../xrEngine/x_ray.h"
#include "../xrEngine/xr_input.h"
#include "ai_space.h"
#include "script_engine.h"
#include "Level.h"
#include "UIGameCustom.h"
#include "UIDialogHolder.h"
#include "MainMenu.h"
#include "GamePersistent.h"
#include "ui_base.h"
#include "UICursor.h"
#include "inventory_item.h"
#include "string_table.h"
#include "ui/UIDialogWnd.h"
#include "ui/UIStatic.h"
#include "ui/UIButton.h"
#include "ui/UICheckButton.h"
#include "ui/UITabButton.h"
#include "ui/UITabControl.h"
#include "ui/UICustomEdit.h"
#include "ui/UITrackBar.h"
#include "ui/UIComboBox.h"
#include "ui/UIListBox.h"
#include "ui/UIListBoxItem.h"
#include "ui/UIScrollView.h"
#include "ui/UIDragDropListEx.h"
#include "ui/UICellItem.h"
#include "ui/UICellCustomItems.h"
#include "ui/UIPropertiesBox.h"
#include "ui/UITalkDialogWnd.h"
#include "ui/UIMMShniaga.h"
#include <luabind/detail/object_rep.hpp>
#include <luabind/detail/class_rep.hpp>
#include <typeinfo>

// MCP UI: the virtual cursor hooks for input.run "cursor" steps and ui.tree, a flat dump of the visible windows
// of the top dialog(s) (class, text, absolute UI-space rect, state, inventory cells, talk phrases/options).
namespace
{
	xr_string js(LPCSTR s) { return s ? mcp::to_json_string(s) : xr_string("null"); }

	xr_string fnum(double v, int prec = 1)
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
		jobj& num(LPCSTR k, double v, int prec = 1) { return raw(k, fnum(v, prec)); }
		jobj& i(LPCSTR k, s64 v) { return raw(k, xr_string(std::to_string(v).c_str())); }
		jobj& b(LPCSTR k, bool v) { return raw(k, v ? "true" : "false"); }
		jobj& rect(LPCSTR k, const Frect& r)
		{
			return raw(k, "[" + fnum(r.x1) + "," + fnum(r.y1) + "," + fnum(r.x2) + "," + fnum(r.y2) + "]");
		}
		xr_string done() const { return s.empty() ? xr_string("{}") : s + "}"; }
	};

	xr_string class_name(const std::type_info& t)
	{
		LPCSTR n = t.name();
		if (!strncmp(n, "class ", 6))
			n += 6;
		else if (!strncmp(n, "struct ", 7))
			n += 7;
		return n;
	}

	// Protected members read without side effects (CUIStatic::TextItemControl() would create the text control)
	struct static_acc : CUIStatic
	{
		static CUILines* lines(CUIStatic* s) { return s->*(&static_acc::m_pTextControl); }
	};
	struct edit_acc : CUICustomEdit
	{
		static bool focus(CUICustomEdit* e) { return e->*(&edit_acc::m_bInputFocus); }
	};
	struct track_acc : CUITrackBar
	{
		static bool is_float(CUITrackBar* t) { return t->*(&track_acc::m_b_is_float); }
	};

	LPCSTR static_text(CUIStatic* s)
	{
		CUILines* l = static_acc::lines(s);
		LPCSTR t = l ? l->GetText() : nullptr;
		return t && *t ? t : nullptr;
	}

	LPCSTR nonempty(LPCSTR t) { return t && *t ? t : nullptr; }

	// own text, else the first text among the visible children (buttons whose caption is a child window)
	LPCSTR widget_text(CUIStatic* s, u32 depth = 0)
	{
		if (LPCSTR t = static_text(s))
			return t;
		if (depth >= 3)
			return nullptr;
		for (CUIWindow* c : s->GetChildWndList())
		{
			if (!c->IsShown())
				continue;
			if (CUITextWnd* tw = smart_cast<CUITextWnd*>(c))
				if (LPCSTR t = nonempty(tw->GetText()))
					return t;
			if (CUIStatic* cs = smart_cast<CUIStatic*>(c))
				if (LPCSTR t = widget_text(cs, depth + 1))
					return t;
		}
		return nullptr;
	}

	lua_State* lua() { return g_ai_space ? ai().script_engine().lua() : nullptr; }

	// Pushes the Lua object of a script-derived window; false (nothing pushed) for plain C++ windows
	bool push_lua_self(CUIWindow* w, lua_State* L)
	{
		luabind::wrap_base* wb = dynamic_cast<luabind::wrap_base*>(w);
		if (!wb || !L || !lua_checkstack(L, 16))
			return false;
		const luabind::weak_ref& self = luabind::detail::wrap_access::ref(*wb);
		if (!*(void* const*)&self) // weak_ref::m_impl (private) is null until luabind binds the Lua object
			return false;
		self.get(L);
		return true;
	}

	xr_string lua_class(CUIWindow* w)
	{
		lua_State* L = lua();
		xr_string r;
		if (!L)
			return r;
		int top = lua_gettop(L);
		if (push_lua_self(w, L))
		{
			luabind::detail::object_rep* o = luabind::detail::is_class_object(L, -1);
			if (o && o->crep() && o->crep()->name())
				r = o->crep()->name();
		}
		lua_settop(L, top);
		return r;
	}

	struct cell_info
	{
		xr_string section, container;
		s64 id = -1;
		u32 count = 1;
	};

	// Anomaly script inventories (utils_ui.UICellContainer in the window's CC table): cell statics -> item
	const char* cells_chunk =
		"local w = ...\n"
		"local function get(o, k) local ok, v = pcall(function() return o[k] end) if ok then return v end end\n"
		"local cc = get(w, 'CC') if type(cc) ~= 'table' then return nil end\n"
		"local out = {}\n"
		"for cname, c in pairs(cc) do\n"
		"  local cells = get(c, 'cell')\n"
		"  if type(cells) == 'table' then\n"
		"    for _, ci in pairs(cells) do\n"
		"      local sec, st = get(ci, 'section'), get(ci, 'cell')\n"
		"      if sec and st then\n"
		"        local n, ch = 1, get(ci, 'childs')\n"
		"        if type(ch) == 'table' then for _ in pairs(ch) do n = n + 1 end end\n"
		"        out[#out + 1] = { st, tostring(sec), tonumber(get(ci, 'ID')) or -1, n, tostring(cname) }\n"
		"      end\n"
		"    end\n"
		"  end\n"
		"end\n"
		"return out\n";

	void script_cells(CUIWindow* w, xr_map<CUIWindow*, cell_info>& cells)
	{
		lua_State* L = lua();
		if (!L)
			return;
		int top = lua_gettop(L);
		if (luaL_loadbuffer(L, cells_chunk, xr_strlen(cells_chunk), "=ui.tree") == 0 && push_lua_self(w, L) &&
			lua_pcall(L, 1, 1, 0) == 0 && lua_istable(L, -1))
		{
			int t = lua_gettop(L);
			int n = (int)lua_objlen(L, t);
			for (int k = 1; k <= n; k++)
			{
				lua_rawgeti(L, t, k);
				int e = lua_gettop(L);
				lua_rawgeti(L, e, 1);
				luabind::object o(L);
				o.set();
				std::optional<CUIWindow*> cw = luabind::object_cast_nothrow<CUIWindow*>(o);
				if (cw && *cw)
				{
					cell_info& ci = cells[*cw];
					lua_rawgeti(L, e, 2);
					ci.section = lua_tostring(L, -1) ? lua_tostring(L, -1) : "";
					lua_rawgeti(L, e, 3);
					ci.id = (s64)lua_tonumber(L, -1);
					lua_rawgeti(L, e, 4);
					ci.count = (u32)lua_tonumber(L, -1);
					lua_rawgeti(L, e, 5);
					ci.container = lua_tostring(L, -1) ? lua_tostring(L, -1) : "";
				}
				lua_settop(L, t);
			}
		}
		lua_settop(L, top);
	}

	xr_string item_json(LPCSTR section, s64 id, u32 count, LPCSTR container, CInventoryItem* it)
	{
		jobj o;
		o.str("section", section);
		if (!it && id >= 0 && id < 0xffff && g_pGameLevel)
			it = smart_cast<CInventoryItem*>(Level().Objects.net_Find((u16)id));
		LPCSTR name = it ? it->NameItem() : nullptr;
		shared_str tr;
		if (!name && section && pSettings->section_exist(section) && pSettings->line_exist(section, "inv_name"))
		{
			tr = CStringTable().translate(pSettings->r_string(section, "inv_name"));
			name = tr.c_str();
		}
		o.str("name", name);
		if (id >= 0)
			o.i("id", id);
		if (count > 1)
			o.i("count", count);
		if (container && *container)
			o.str("container", container);
		return o.done();
	}

	struct walker
	{
		u32 max_nodes = 3000, max_depth = 40, count = 0, truncated = 0;
		xr_string out;
		xr_map<CUIWindow*, cell_info> cells;

		bool clips(CUIWindow* w)
		{
			return w->IsClipEnabled() || smart_cast<CUIScrollView*>(w) || smart_cast<CUIDragDropListEx*>(w);
		}

		// type-specific fields; returns the kind
		LPCSTR describe(CUIWindow* w, jobj& n)
		{
			if (CUIQuestionItem* q = smart_cast<CUIQuestionItem*>(w))
			{
				n.str("text", q->m_text ? static_text(q->m_text) : nullptr);
				if (q->m_num_text)
					n.str("num", nonempty(q->m_num_text->GetText()));
				return "question";
			}
			if (smart_cast<CUIAnswerItem*>(w))
			{
				// children: m_text, m_name (CUIAnswerItem ctor)
				CUIWindow::WINDOW_LIST& ch = w->GetChildWndList();
				CUITextWnd* t = ch.size() > 0 ? smart_cast<CUITextWnd*>(ch[0]) : nullptr;
				CUITextWnd* s = ch.size() > 1 ? smart_cast<CUITextWnd*>(ch[1]) : nullptr;
				n.str("speaker", s ? nonempty(s->GetText()) : nullptr);
				n.str("text", t ? nonempty(t->GetText()) : nullptr);
				return "answer";
			}
			if (CUITabButton* tb = smart_cast<CUITabButton*>(w))
			{
				n.str("text", widget_text(tb));
				if (tb->m_btn_id.size())
					n.str("tab_id", tb->m_btn_id.c_str());
				if (tb->GetButtonState() == CUIButton::BUTTON_PUSHED)
					n.b("active", true);
				return "tab";
			}
			if (CUICheckButton* cb = smart_cast<CUICheckButton*>(w))
			{
				n.str("text", widget_text(cb));
				n.b("checked", cb->GetCheck());
				return "check";
			}
			if (CUICustomEdit* e = smart_cast<CUICustomEdit*>(w))
			{
				n.str("text", e->GetText());
				if (edit_acc::focus(e))
					n.b("focus", true);
				return "edit";
			}
			if (CUITrackBar* t = smart_cast<CUITrackBar*>(w))
			{
				if (track_acc::is_float(t))
					n.num("value", t->GetFValue(), 3);
				else
					n.i("value", t->GetIValue());
				return "track";
			}
			if (CUIComboBox* c = smart_cast<CUIComboBox*>(w))
			{
				n.str("text", nonempty(c->GetText()));
				if (s32(c->GetSelectedIDX()) >= 0)
					n.i("selected", c->GetSelectedIDX());
				return "combo";
			}
			if (CUIListBoxItem* li = smart_cast<CUIListBoxItem*>(w))
			{
				n.str("text", nonempty(li->GetText()));
				if (li->GetSelected())
					n.b("selected", true);
				return "list_item";
			}
			if (CUICellItem* ci = smart_cast<CUICellItem*>(w))
			{
				CUIInventoryCellItem* ic = smart_cast<CUIInventoryCellItem*>(ci);
				CInventoryItem* it = ic ? ic->object() : nullptr;
				if (it)
					n.raw("item", item_json(it->object().cNameSect().c_str(), it->object().ID(), ci->ChildsCount() + 1, nullptr, it));
				return "cell";
			}
			if (CUIButton* b = smart_cast<CUIButton*>(w))
			{
				n.str("text", widget_text(b));
				if (b->GetButtonState() == CUIButton::BUTTON_PUSHED)
					n.b("pushed", true);
				return "button";
			}
			if (CUITabControl* tc = smart_cast<CUITabControl*>(w))
			{
				n.str("active_tab", nonempty(tc->GetActiveId().c_str()));
				return "tabs";
			}
			if (CUIScrollView* sv = smart_cast<CUIScrollView*>(w))
			{
				n.raw("scroll", "[" + fnum(sv->GetCurrentScrollPos(), 0) + "," + fnum(sv->GetMinScrollPos(), 0) + "," +
					fnum(sv->GetMaxScrollPos(), 0) + "]");
				if (sv->WindowName() == "---UIQuestionsList")
					return "talk_options";
				if (sv->WindowName() == "---UIAnswersList")
					return "talk_log";
				return smart_cast<CUIListBox*>(w) ? "list" : "scroll";
			}
			if (smart_cast<CUIDragDropListEx*>(w))
				return "cell_list";
			if (smart_cast<CUIPropertiesBox*>(w))
				return "menu";
			if (CUIStatic* s = smart_cast<CUIStatic*>(w))
			{
				n.str("text", static_text(s));
				auto it = cells.find(w);
				if (it != cells.end())
				{
					const cell_info& c = it->second;
					n.raw("item", item_json(c.section.c_str(), c.id, c.count, c.container.c_str(), nullptr));
					return "cell";
				}
				return "static";
			}
			if (CUITextWnd* t = smart_cast<CUITextWnd*>(w))
			{
				n.str("text", nonempty(t->GetText()));
				// main menu entries: text windows inside CUIMMShniaga (clicked through the shniaga)
				for (CUIWindow* p = w->GetParent(); p; p = p->GetParent())
					if (smart_cast<CUIMMShniaga*>(p))
						return "menu_item";
				return "text";
			}
			return "window";
		}

		void walk(CUIWindow* w, int parent, u32 depth, const Fvector2& origin, const Frect& clip, bool enabled, const xr_string& path)
		{
			if (!w->IsShown())
				return;
			if (count >= max_nodes)
			{
				truncated++;
				return;
			}
			Frect r = w->GetWndRect();
			r.add(origin.x, origin.y);
			enabled = enabled && w->IsEnabled();

			if (push_script_cells(w))
				script_cells(w, cells);

			int idx = (int)count++;
			jobj n;
			n.i("i", idx).i("p", parent).str("path", path.c_str()).i("d", depth);
			n.str("class", class_name(typeid(*w)).c_str());
			xr_string lc = dynamic_cast<luabind::wrap_base*>(w) ? lua_class(w) : xr_string();
			if (lc.size())
				n.str("script_class", lc.c_str());
			if (w->WindowName().size())
				n.str("name", w->WindowName().c_str());
			LPCSTR kind = describe(w, n);
			n.str("kind", kind);
			n.rect("rect", r);
			if (!enabled)
				n.b("enabled", false);
			if (w->CursorOverWindow())
				n.b("hover", true);
			if (r.x2 <= clip.x1 || r.x1 >= clip.x2 || r.y2 <= clip.y1 || r.y1 >= clip.y2)
				n.str("clip", "out");
			else if (r.x1 < clip.x1 || r.x2 > clip.x2 || r.y1 < clip.y1 || r.y2 > clip.y2)
				n.str("clip", "partial");
			out += (out.empty() ? "" : ",") + n.done();

			// children of buttons, check boxes, tabs and track bars are their own visuals (and slider)
			if (depth >= max_depth || !xr_strcmp(kind, "button") || !xr_strcmp(kind, "check") || !xr_strcmp(kind, "tab") ||
				!xr_strcmp(kind, "track"))
				return;
			Frect cc = clip;
			if (clips(w))
			{
				Frect wr = r;
				if (w->IsClipEnabled())
					w->GetClipRect(wr);
				if (!cc.intersection(clip, wr))
					cc.set(-1.f, -1.f, -1.f, -1.f);
			}
			xr_vector<CUIWindow*> children;
			{
				xrCriticalSectionGuard guard(w->csUi);
				children.assign(w->GetChildWndList().begin(), w->GetChildWndList().end());
			}
			for (u32 k = 0; k < children.size(); k++)
				walk(children[k], idx, depth + 1, r.lt, cc, enabled, path + "." + std::to_string(k).c_str());
		}

		// script windows that may own utils_ui cell containers
		bool push_script_cells(CUIWindow* w) { return smart_cast<CUIDialogWnd*>(w) && dynamic_cast<luabind::wrap_base*>(w); }
	};

	xr_string cursor_json()
	{
		jobj c;
		if (!g_pGamePersistent || !GamePersistent().m_pUI_core)
			return "null";
		CUICursor& cur = GetUICursor();
		Fvector2 p = cur.GetCursorPosition();
		c.num("x", p.x).num("y", p.y).b("visible", cur.IsVisible()).b("mcp_owned", cur.McpOwned());
		return c.done();
	}

	bool ui_tree(LPCSTR params, xr_string& out)
	{
		CTimer timer;
		timer.Start();
		xr_string root = "top";
		mcp::get_string_param(params, "root", root);
		s64 v;
		walker wk;
		if (mcp::get_int_param(params, "max_nodes", v))
			wk.max_nodes = (u32)_max(1ll, _min(v, 20000ll));
		if (mcp::get_int_param(params, "max_depth", v))
			wk.max_depth = (u32)_max(0ll, _min(v, 100ll));

		jobj res;
		res.raw("screen", "[" + fnum(Device.dwWidth, 0) + "," + fnum(Device.dwHeight, 0) + "]");
		res.raw("ui_size", "[1024,768]");
		bool loading = pApp && pApp->IsLoading();
		if (loading || !g_pGamePersistent || !GamePersistent().m_pUI_core)
		{
			res.b("loading", true).raw("dialogs", "[]").raw("nodes", "[]");
			out = res.done();
			return true;
		}
		res.raw("cursor", cursor_json());

		bool level = g_pGameLevel && g_pGameLevel->name().size();
		CMainMenu* mm = MainMenu();
		bool menu = mm && mm->IsActive();
		CUIGameCustom* gui = level ? CurrentGameUI() : nullptr;

		struct rootw { LPCSTR tag; CUIWindow* w; };
		xr_vector<rootw> roots;
		CUIDialogWnd* mtop = menu ? mm->TopInputReceiver() : nullptr;
		CUIDialogWnd* gtop = gui ? gui->TopInputReceiver() : nullptr;
		if (root == "top")
		{
			if (menu)
			{
				if (mtop)
					roots.push_back({ "menu", mtop });
			}
			else if (gtop)
				roots.push_back({ "game", gtop });
		}
		else if (root == "menu" || root == "all")
		{
			if (mtop)
				roots.push_back({ "menu", mtop });
		}
		if ((root == "game" || root == "all") && gtop)
			roots.push_back({ "game", gtop });
		if ((root == "hud" || root == "all") && gui && gui->GetWindow())
			roots.push_back({ "hud", gui->GetWindow() });
		if (root != "top" && root != "menu" && root != "game" && root != "hud" && root != "all")
		{
			out = "root must be one of top, menu, game, hud, all";
			return false;
		}

		xr_string dialogs;
		Frect screen;
		screen.set(0.f, 0.f, UI_BASE_WIDTH, UI_BASE_HEIGHT);
		for (u32 k = 0; k < roots.size(); k++)
		{
			jobj d;
			d.str("root", roots[k].tag).str("class", class_name(typeid(*roots[k].w)).c_str());
			xr_string lc = lua_class(roots[k].w);
			if (lc.size())
				d.str("script_class", lc.c_str());
			d.i("node", wk.count);
			dialogs += (dialogs.empty() ? "" : ",") + d.done();
			wk.walk(roots[k].w, -1, 0, Fvector2().set(0.f, 0.f), screen, true, std::to_string(k).c_str());
		}
		res.raw("dialogs", "[" + dialogs + "]");
		res.raw("nodes", "[" + wk.out + "]");
		if (wk.truncated)
			res.i("truncated", wk.truncated);
		res.num("ms", timer.GetElapsed_sec() * 1000.f, 2);
		out = res.done();
		return true;
	}
}

// --- input.run "cursor" step hooks (xrGame/mcp_input.cpp) ---

bool mcp_ui_cursor_get(float& x, float& y)
{
	if (!g_pGamePersistent || !GamePersistent().m_pUI_core)
		return false;
	Fvector2 p = GetUICursor().GetCursorPosition();
	x = p.x;
	y = p.y;
	return true;
}

// Through IR_OnMouseMove like a real move when the cursor is up (hover, drag, capture); else only placed
bool mcp_ui_cursor_move(float x, float y)
{
	if (!g_pGamePersistent || !GamePersistent().m_pUI_core || !pInput)
		return false;
	CUICursor& c = GetUICursor();
	Fvector2 cur = c.GetCursorPosition();
	c.McpMoveTo(Fvector2().set(x, y));
	if (c.IsVisible())
		pInput->CurrentIR()->IR_OnMouseMove(iFloor(x - cur.x + 0.5f), iFloor(y - cur.y + 0.5f));
	else
		c.SetUICursorPosition(Fvector2().set(_min(_max(x, 0.f), UI_BASE_WIDTH), _min(_max(y, 0.f), UI_BASE_HEIGHT)));
	return true;
}

void mcp_ui_cursor_release()
{
	if (g_pGamePersistent && GamePersistent().m_pUI_core)
		GetUICursor().McpRelease();
}

// state.get ui.cursor_pos
xr_string mcp_ui_cursor_json() { return cursor_json(); }

void mcp_register_ui()
{
	if (!Core.ParamsData.test(ECoreParams::mcp_port))
		return;
	mcp::register_method("ui.tree", ui_tree);
}
