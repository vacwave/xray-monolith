// Copyright (c) 2003 Daniel Wallin and Arvid Norberg

// Permission is hereby granted, free of charge, to any person obtaining a
// copy of this software and associated documentation files (the "Software"),
// to deal in the Software without restriction, including without limitation
// the rights to use, copy, modify, merge, publish, distribute, sublicense,
// and/or sell copies of the Software, and to permit persons to whom the
// Software is furnished to do so, subject to the following conditions:

// The above copyright notice and this permission notice shall be included
// in all copies or substantial portions of the Software.

// THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF
// ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED
// TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
// PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT
// SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR
// ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
// ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
// OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE
// OR OTHER DEALINGS IN THE SOFTWARE.
#include "luabind_api.h"
#include <luabind/detail/pcall.hpp>
#include <luabind/error.hpp>
#include <luabind/lua_include.hpp>

namespace luabind { namespace detail
{
	pcall_hook_fun pcall_hook = 0;

	// Error handler closure kept in the registry of the state bound by pcall_cache_reset (other states push a fresh one)
	static const void* cached_handler_registry = 0;
	static pcall_callback_fun cached_handler_fun = 0;
	static int cached_handler_ref = LUA_NOREF;

	void pcall_cache_reset(lua_State* L)
	{
		cached_handler_fun = 0;
		cached_handler_ref = LUA_NOREF;
		cached_handler_registry = L ? lua_topointer(L, LUA_REGISTRYINDEX) : 0;
	}

	static void push_pcall_callback(lua_State* L, pcall_callback_fun e)
	{
		if (cached_handler_registry == lua_topointer(L, LUA_REGISTRYINDEX))
		{
			if (cached_handler_fun == e)
			{
				lua_rawgeti(L, LUA_REGISTRYINDEX, cached_handler_ref);
				return;
			}
			if (cached_handler_ref == LUA_NOREF)
			{
				lua_pushcfunction(L, e);
				lua_pushvalue(L, -1);
				cached_handler_ref = luaL_ref(L, LUA_REGISTRYINDEX);
				cached_handler_fun = e;
				return;
			}
		}
		lua_pushcfunction(L, e);
	}

	int pcall(lua_State *L, int nargs, int nresults)
	{
		pcall_hook_fun hook = pcall_hook;
		pcall_callback_fun e = get_pcall_callback();
		int en = 0;
		int base = lua_gettop(L) - nargs;
		if ( e )
		{
			push_pcall_callback(L, e);
			lua_insert(L, base);  // push pcall_callback under chunk and args
			en = base;
			base++;
  		}
		if (hook) hook(L, base, true);
		int result = lua_pcall(L, nargs, nresults, en);
		if (hook) hook(L, 0, false);
		if ( en )
			lua_remove(L, en);  // remove pcall_callback
		return result;
	}

	int resume_impl(lua_State *L, int nargs, int)
	{
		pcall_hook_fun hook = pcall_hook;
		if (hook) hook(L, 0, true);
		int result = lua_resume(L, nargs);
		if (hook) hook(L, 0, false);
		return result;
	}

}}
luabind::memory_allocation_function_pointer		luabind::allocator = 0;
luabind::memory_allocation_function_parameter	luabind::allocator_parameter = 0;