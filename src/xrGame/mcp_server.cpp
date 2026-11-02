////////////////////////////////////////////////////////////////////////////
//	Module 		: mcp_server.cpp
//	Description : MCP engine endpoint (loopback TCP), contract: tools/mcp/PROTOCOL.md
//
//	Threads:
//	- main thread:     mcp_server_start/stop, mcp_frame::OnFrame (Device.seqFrame).
//	                   The only place that touches Console, Lua and g_pGameLevel.
//	- listener thread: mcp_listener_thread. Touches only sockets, the log ring
//	                   (g_log_lock), the request/reply queues (g_queue_lock) and atomics.
//	- logger thread:   mcp_log_callback. Only appends to the log ring and the console
//	                   capture buffer under g_log_lock.
////////////////////////////////////////////////////////////////////////////

#include "pch_script.h"

#ifdef MCP_SERVER

#include <winsock2.h>
#include <atomic>
#include "mcp_server.h"
#include "ai_space.h"
#include "../xrServerEntities/script_engine.h"
#include "../xrEngine/xr_ioconsole.h"
#include "../xrEngine/IGame_Level.h"

#pragma comment(lib, "ws2_32.lib")

namespace
{
	const u16 MCP_PORT = 28015;
	const u32 MCP_MAX_CLIENTS = 4;
	const u32 MCP_MAX_PAYLOAD = 1024 * 1024;
	const u32 MCP_LOG_LINES = 2000;
	const u32 MCP_LOG_LINE_MAX = 1024;
	const u32 MCP_LOG_DEFAULT = 50;
	const u32 MCP_CAPTURE_MAX = 64 * 1024;
	// CConsole::ExecuteCommand _alloca's the command three times; keep it to the console input size
	const u32 MCP_CONSOLE_MAX = CConsole::CONSOLE_BUF_SIZE - 1;

	enum EMcpOp
	{
		eMcpConsole,
		eMcpEval,
	};

	struct mcp_request
	{
		u32			client_id;
		EMcpOp		op;
		xr_string	arg;
	};

	struct mcp_reply
	{
		u32			client_id;
		xr_string	payload;
	};

	struct mcp_client
	{
		SOCKET		sock;
		u32			id;
		bool		busy;		// request queued to the main thread, reply not received yet
		xr_string	in;
		xr_string	out;
		u32			out_pos;
	};

	// main thread writes, listener thread reads
	std::atomic<u32>	g_frame(0);
	std::atomic<bool>	g_paused(false);
	std::atomic<bool>	g_level(false);
	// listener increments on enqueue, main thread decrements after executing
	std::atomic<u32>	g_pending(0);
	// main thread sets on stop, listener thread exits its loop
	std::atomic<bool>	g_stop(false);

	// log ring, guarded by g_log_lock (logger thread writes, listener thread reads)
	xrCriticalSection	g_log_lock;
	char				g_log_ring[MCP_LOG_LINES][MCP_LOG_LINE_MAX];
	u32					g_log_len[MCP_LOG_LINES];
	u32					g_log_head = 0;		// next slot to write
	u32					g_log_count = 0;
	// console output capture, also under g_log_lock: lines logged while a console command runs
	bool				g_capture = false;
	xr_string			g_capture_text;

	// request/reply queues, guarded by g_queue_lock (listener thread <-> main thread)
	xrCriticalSection		g_queue_lock;
	xr_vector<mcp_request>	g_requests;
	xr_vector<mcp_reply>	g_replies;

	// main thread only (start/stop)
	SOCKET	g_listen_sock = INVALID_SOCKET;
	HANDLE	g_thread = nullptr;
	bool	g_wsa_started = false;

	// logger thread, called under xrLogger::logCallbackGuard. Must not call Msg().
	void mcp_log_callback(const char* line)
	{
		u32 len = line ? xr_strlen(line) : 0;
		if (len > MCP_LOG_LINE_MAX)
			len = MCP_LOG_LINE_MAX;

		xrCriticalSectionGuard guard(&g_log_lock);
		if (len)
			CopyMemory(g_log_ring[g_log_head], line, len);
		g_log_len[g_log_head] = len;
		g_log_head = (g_log_head + 1) % MCP_LOG_LINES;
		if (g_log_count < MCP_LOG_LINES)
			++g_log_count;

		if (g_capture && g_capture_text.size() + len + 1 <= MCP_CAPTURE_MAX)
		{
			g_capture_text.append(line, len);
			g_capture_text += '\n';
		}
	}

	// main thread. Runs a console command and returns what it logged as the reply text.
	void mcp_console(const xr_string& cmd, xr_string& reply)
	{
		{
			xrCriticalSectionGuard guard(&g_log_lock);
			g_capture_text.clear();
			g_capture = true;
		}
		Console->Execute(cmd.c_str());
		// the log callback runs on the logger thread; wait (bounded) until it has seen every line the command logged
		xrLogger::FlushLog();
		xrCriticalSectionGuard guard(&g_log_lock);
		g_capture = false;
		if (!g_capture_text.empty())
			g_capture_text.pop_back();	// trailing newline
		reply = "+" + g_capture_text;
		g_capture_text.clear();
	}

	// listener thread. Appends the last n lines (oldest first, '\n' separated) to text.
	void mcp_log_tail(u32 n, xr_string& text)
	{
		xrCriticalSectionGuard guard(&g_log_lock);
		if (n > g_log_count)
			n = g_log_count;
		u32 idx = (g_log_head + MCP_LOG_LINES - n) % MCP_LOG_LINES;
		for (u32 i = 0; i < n; ++i)
		{
			if (i)
				text += '\n';
			text.append(g_log_ring[idx], g_log_len[idx]);
			idx = (idx + 1) % MCP_LOG_LINES;
		}
	}

	// listener thread. Frames a reply into the client's output buffer.
	void mcp_send_reply(mcp_client& c, const xr_string& payload)
	{
		xr_string too_large;
		const xr_string* p = &payload;
		if (payload.size() > MCP_MAX_PAYLOAD)
		{
			too_large = FormatString("-reply too large (%u bytes)", u32(payload.size()));
			p = &too_large;
		}
		const u32 len = u32(p->size());
		c.out.append((const char*)&len, sizeof(len));
		c.out.append(*p);
	}

	// listener thread
	void mcp_close_client(mcp_client& c)
	{
		closesocket(c.sock);
		c.sock = INVALID_SOCKET;
		c.busy = false;
		c.in.clear();
		c.out.clear();
		c.out_pos = 0;
	}

	// listener thread. Handles one complete request payload.
	void mcp_handle_request(mcp_client& c, const char* data, u32 size)
	{
		const char* space = (const char*)memchr(data, ' ', size);
		const xr_string op(data, space ? u32(space - data) : size);
		const xr_string arg = space ? xr_string(space + 1, size - u32(space - data) - 1) : xr_string();

		if (op == "status")
		{
			mcp_send_reply(c, FormatString("+frame=%u\npaused=%d\nlevel=%d\npending=%u",
				g_frame.load(), g_paused.load() ? 1 : 0, g_level.load() ? 1 : 0, g_pending.load()));
		}
		else if (op == "log")
		{
			u32 n = MCP_LOG_DEFAULT;
			if (!arg.empty())
			{
				char* end = nullptr;
				const unsigned long v = strtoul(arg.c_str(), &end, 10);
				if (end == arg.c_str() || *end || arg[0] == '-')
				{
					mcp_send_reply(c, "-invalid line count: " + arg);
					return;
				}
				n = v > MCP_LOG_LINES ? MCP_LOG_LINES : u32(v);
			}
			xr_string text = "+";
			mcp_log_tail(n, text);
			mcp_send_reply(c, text);
		}
		else if (op == "console" || op == "eval")
		{
			const bool console = op == "console";
			if (console && arg.size() > MCP_CONSOLE_MAX)
			{
				mcp_send_reply(c, FormatString("-console command too long (max %u bytes)", MCP_CONSOLE_MAX));
				return;
			}
			mcp_request r;
			r.client_id = c.id;
			r.op = console ? eMcpConsole : eMcpEval;
			r.arg = arg;
			c.busy = true;
			xrCriticalSectionGuard guard(&g_queue_lock);
			g_requests.push_back(r);
			++g_pending;
		}
		else
			mcp_send_reply(c, "-unknown op: " + op);
	}

	// listener thread. Reads what is available; returns false if the client must be closed.
	bool mcp_read_client(mcp_client& c)
	{
		char buf[16384];
		for (;;)
		{
			const int r = recv(c.sock, buf, sizeof(buf), 0);
			if (r == 0)
				return false;
			if (r == SOCKET_ERROR)
				return WSAGetLastError() == WSAEWOULDBLOCK;
			c.in.append(buf, r);
			if (c.in.size() > sizeof(u32) + MCP_MAX_PAYLOAD)
				return false;
		}
	}

	// listener thread. Handles buffered requests while the client is not waiting on the main thread.
	// Returns false if the client must be closed.
	bool mcp_parse_client(mcp_client& c)
	{
		while (!c.busy && c.in.size() >= sizeof(u32))
		{
			u32 len;
			CopyMemory(&len, c.in.data(), sizeof(len));
			if (len > MCP_MAX_PAYLOAD)
				return false;
			if (c.in.size() < sizeof(u32) + len)
				break;
			mcp_handle_request(c, c.in.data() + sizeof(u32), len);
			c.in.erase(0, sizeof(u32) + len);
		}
		return true;
	}

	// listener thread. Sends buffered output; returns false if the client must be closed.
	bool mcp_write_client(mcp_client& c)
	{
		while (c.out_pos < c.out.size())
		{
			const int r = send(c.sock, c.out.data() + c.out_pos, int(c.out.size() - c.out_pos), 0);
			if (r == SOCKET_ERROR)
				return WSAGetLastError() == WSAEWOULDBLOCK;
			c.out_pos += u32(r);
		}
		c.out.clear();
		c.out_pos = 0;
		return true;
	}

	// listener thread
	void mcp_listener_thread(void*)
	{
		mcp_client clients[MCP_MAX_CLIENTS];
		for (mcp_client& c : clients)
		{
			c.sock = INVALID_SOCKET;
			c.id = 0;
			c.busy = false;
			c.out_pos = 0;
		}
		u32 next_id = 1;
		xr_vector<mcp_reply> replies;

		while (!g_stop.load())
		{
			fd_set rd, wr;
			FD_ZERO(&rd);
			FD_ZERO(&wr);
			FD_SET(g_listen_sock, &rd);
			for (mcp_client& c : clients)
			{
				if (c.sock == INVALID_SOCKET)
					continue;
				FD_SET(c.sock, &rd);
				if (!c.out.empty())
					FD_SET(c.sock, &wr);
			}

			timeval tv = { 0, 50 * 1000 };
			if (select(0, &rd, &wr, nullptr, &tv) == SOCKET_ERROR)
			{
				Sleep(50);
				continue;
			}

			if (FD_ISSET(g_listen_sock, &rd))
			{
				const SOCKET s = accept(g_listen_sock, nullptr, nullptr);
				if (s != INVALID_SOCKET)
				{
					mcp_client* slot = nullptr;
					for (mcp_client& c : clients)
					{
						if (c.sock == INVALID_SOCKET)
						{
							slot = &c;
							break;
						}
					}
					// all slots taken: refuse by closing right away
					u_long nonblocking = 1;
					if (!slot || ioctlsocket(s, FIONBIO, &nonblocking) == SOCKET_ERROR)
						closesocket(s);
					else
					{
						BOOL nodelay = TRUE;
						setsockopt(s, IPPROTO_TCP, TCP_NODELAY, (const char*)&nodelay, sizeof(nodelay));
						slot->sock = s;
						slot->id = next_id++;
					}
				}
			}

			for (mcp_client& c : clients)
			{
				if (c.sock == INVALID_SOCKET || !FD_ISSET(c.sock, &rd))
					continue;
				if (!mcp_read_client(c) || !mcp_parse_client(c))
					mcp_close_client(c);
			}

			{
				xrCriticalSectionGuard guard(&g_queue_lock);
				replies.swap(g_replies);
			}
			for (const mcp_reply& r : replies)
			{
				// the client may be gone (bridge timeout): then the reply is dropped
				for (mcp_client& c : clients)
				{
					if (c.sock != INVALID_SOCKET && c.id == r.client_id)
					{
						c.busy = false;
						mcp_send_reply(c, r.payload);
						break;
					}
				}
			}
			replies.clear();

			for (mcp_client& c : clients)
			{
				if (c.sock == INVALID_SOCKET)
					continue;
				// requests buffered while busy, then flush output
				if (!mcp_parse_client(c) || !mcp_write_client(c))
					mcp_close_client(c);
			}
		}

		for (mcp_client& c : clients)
		{
			if (c.sock != INVALID_SOCKET)
				mcp_close_client(c);
		}
	}

	// main thread. Calls mcp_helper.eval(code) with pcall semantics; never raises into the engine.
	void mcp_eval(const xr_string& code, xr_string& reply)
	{
		::luabind::functor<void> fn;
		if (!FS.exist("$game_scripts$", "mcp_helper.script") || !ai().script_engine().functor("mcp_helper.eval", fn))
		{
			reply = "-mcp_helper.script not available";
			return;
		}

		// raw lua_pcall instead of calling the luabind functor: luabind's error path is fatal here
		lua_State* L = ai().script_engine().lua();
		const int top = lua_gettop(L);
		fn.pushvalue();
		lua_pushlstring(L, code.data(), code.size());
		if (lua_pcall(L, 1, 1, 0))
		{
			const char* err = lua_tostring(L, -1);
			reply = "-mcp_helper.eval failed: ";
			reply += err ? err : "(non-string error)";
		}
		else if (lua_type(L, -1) == LUA_TSTRING)
		{
			size_t len = 0;
			const char* s = lua_tolstring(L, -1, &len);
			if (len)
				reply.assign(s, len);
			else
				reply = "-mcp_helper.eval returned an empty string";
		}
		else
			reply = FormatString("-mcp_helper.eval returned %s, expected string", lua_typename(L, lua_type(L, -1)));
		lua_settop(L, top);
	}

	class mcp_frame : public pureFrame
	{
	public:
		// main thread (Device.seqFrame). Publishes frame state and drains queued requests.
		virtual void _BCL OnFrame()
		{
			g_frame.store(Device.dwFrame);
			g_paused.store(Device.Paused());
			g_level.store(g_pGameLevel != nullptr);

			xr_vector<mcp_request> requests;
			{
				xrCriticalSectionGuard guard(&g_queue_lock);
				requests.swap(g_requests);
			}
			for (const mcp_request& r : requests)
			{
				mcp_reply reply;
				reply.client_id = r.client_id;
				if (r.op == eMcpConsole)
					mcp_console(r.arg, reply.payload);
				else
					mcp_eval(r.arg, reply.payload);

				xrCriticalSectionGuard guard(&g_queue_lock);
				g_replies.push_back(reply);
				--g_pending;
			}
		}
	};

	mcp_frame g_frame_hook;
}

// main thread (CGamePersistent::OnAppStart)
void mcp_server_start()
{
	WSADATA wsa;
	const int wsa_err = WSAStartup(MAKEWORD(2, 2), &wsa);
	if (wsa_err)
	{
		Msg("! [mcp] WSAStartup failed (%d), server disabled", wsa_err);
		return;
	}
	g_wsa_started = true;

	sockaddr_in addr = {};
	addr.sin_family = AF_INET;
	addr.sin_port = htons(MCP_PORT);
	addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);

	u_long nonblocking = 1;
	g_listen_sock = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
	if (g_listen_sock == INVALID_SOCKET
		|| bind(g_listen_sock, (const sockaddr*)&addr, sizeof(addr)) == SOCKET_ERROR
		|| listen(g_listen_sock, SOMAXCONN) == SOCKET_ERROR
		|| ioctlsocket(g_listen_sock, FIONBIO, &nonblocking) == SOCKET_ERROR)
	{
		Msg("! [mcp] cannot listen on 127.0.0.1:%u (error %d), server disabled", MCP_PORT, WSAGetLastError());
		mcp_server_stop();
		return;
	}

	xrLogger::AddLogCallback(mcp_log_callback);
	Device.seqFrame.Add(&g_frame_hook, REG_PRIORITY_LOW);

	g_stop.store(false);
	g_thread = thread_spawn_ex(mcp_listener_thread, "X-Ray MCP Server", 0, nullptr);
	if (!g_thread)
	{
		Msg("! [mcp] cannot start listener thread, server disabled");
		mcp_server_stop();
		return;
	}
	Msg("* [mcp] server listening on 127.0.0.1:%u", MCP_PORT);
}

// main thread (CGamePersistent::OnAppEnd). Also cleans up after a failed start.
void mcp_server_stop()
{
	if (!g_wsa_started)
		return;

	const bool was_running = g_thread != nullptr;
	if (g_thread)
	{
		g_stop.store(true);
		WaitForSingleObject(g_thread, INFINITE);
		CloseHandle(g_thread);
		g_thread = nullptr;
	}

	Device.seqFrame.Remove(&g_frame_hook);
	xrLogger::RemoveLogCallback(mcp_log_callback);

	if (g_listen_sock != INVALID_SOCKET)
	{
		closesocket(g_listen_sock);
		g_listen_sock = INVALID_SOCKET;
	}
	WSACleanup();
	g_wsa_started = false;

	{
		xrCriticalSectionGuard guard(&g_queue_lock);
		g_requests.clear();
		g_replies.clear();
		g_pending.store(0);
	}

	if (was_running)
		Msg("* [mcp] server stopped");
}

#endif // MCP_SERVER
