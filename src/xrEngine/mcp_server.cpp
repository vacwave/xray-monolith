#include "stdafx.h"
#include "mcp_server.h"
#include "mcp_perf.h"
#include "IGame_Level.h"
#include "XR_IOConsole.h"
#include "x_ray.h"

#include <winsock2.h>
#include <ws2tcpip.h>
#include <thread>
#include <mutex>
#include <condition_variable>
#include <atomic>
#include <algorithm>
#include <nlohmann/json.hpp>

#pragma comment(lib, "ws2_32.lib")

using json = nlohmann::json;

namespace mcp
{
namespace
{
	enum
	{
		protocol_version = 1,
		max_line = 1024 * 1024,
		log_ring_size = 10000, // lines kept for log.tail
		max_queued_events = 10000, // oldest events are dropped beyond this
		max_exec_lines = 500, // run_logged keeps the first lines, counts the rest as truncated
	};

	// JSON-RPC error codes
	enum
	{
		err_parse = -32700,
		err_invalid_request = -32600,
		err_method_not_found = -32601,
		err_internal = -32603,
	};

	struct request
	{
		u32 conn;
		json id;
		std::string method;
		std::string params;
	};

	struct outgoing
	{
		u32 conn; // 0 = any connection (events)
		std::string line;
	};

	SOCKET listen_sock = INVALID_SOCKET;
	std::thread net_thread;
	std::atomic<bool> running = false;
	std::atomic<u32> conn_id = 0; // id of the current client, 0 when none
	std::atomic<SOCKET> client_sock = INVALID_SOCKET;
	std::timed_mutex send_lock; // held while writing to client_sock; the crash path keeps it until exit
	crashhandler* prev_crash_handler = nullptr;

	std::mutex in_lock;
	xr_vector<request> inbox;
	std::timed_mutex out_lock; // timed: the crash path must not wait forever
	xr_deque<outgoing> outbox;
	u32 queued_events = 0; // events (conn 0) in outbox, guarded by out_lock

	struct log_line
	{
		u64 seq;
		std::string line; // UTF-8
	};

	std::mutex log_lock; // guards log_ring and last_seq; taken before out_lock
	xr_deque<log_line> log_ring;
	u64 last_seq = 0;
	bool log_captured = false;

	// run_logged brackets its work with "* [MCP] exec #<id>" / "... done" lines and waits for the logger
	// thread to print the end marker (the logger queue is FIFO, so every line queued between them is in the ring)
	enum { mark_wait_ms = 2000 };
	const char mark_prefix[] = "* [MCP] exec #";
	std::condition_variable mark_cv;
	u64 mark_id = 0, mark_begin = 0, mark_end = 0; // guarded by log_lock
	u64 last_mark_id = 0; // main thread

	xr_map<xr_string, handler> handlers;
	xr_string engine_version;
	string_path port_file = "";

	// <app_data_root>\mcp_port: actual port + newline, written via temp file + rename for the supervisor
	void write_port_file(u16 port)
	{
		string_path tmp;
		FS.update_path(port_file, "$app_data_root$", "mcp_port");
		xr_sprintf(tmp, "%s.tmp", port_file);
		FILE* f = fopen(tmp, "wb");
		bool ok = f && fprintf(f, "%u\n", port) > 0;
		if (f && fclose(f) != 0)
			ok = false;
		if (!ok || !MoveFileEx(tmp, port_file, MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH))
		{
			Msg("! [MCP] Failed to write %s", port_file);
			port_file[0] = 0;
		}
	}

	std::string dump(const json& j)
	{
		return j.dump(-1, ' ', false, json::error_handler_t::replace);
	}

	void queue_out(u32 conn, std::string line)
	{
		line += '\n';
		std::lock_guard<std::timed_mutex> lock(out_lock);
		outbox.push_back({ conn, std::move(line) });
	}

	// Never blocks on the client: a slow reader loses the oldest events (recoverable via log.tail)
	void queue_event(const json& msg)
	{
		std::string line = dump(msg);
		line += '\n';
		std::lock_guard<std::timed_mutex> lock(out_lock);
		if (queued_events >= max_queued_events)
		{
			auto it = std::find_if(outbox.begin(), outbox.end(), [](const outgoing& o) { return o.conn == 0; });
			if (it != outbox.end())
			{
				outbox.erase(it);
				queued_events--;
			}
		}
		outbox.push_back({ 0, std::move(line) });
		queued_events++;
	}

	// cp1251 -> UTF-8 (every cp1251 byte maps; unmapped 0x98 becomes U+FFFD)
	std::string to_utf8(const char* s)
	{
		int len = xr_strlen(s);
		if (!len)
			return std::string();
		std::wstring w(len, L'\0');
		int wn = MultiByteToWideChar(1251, 0, s, len, &w[0], len);
		std::string out(wn * 3, '\0');
		int n = WideCharToMultiByte(CP_UTF8, 0, w.data(), wn, &out[0], (int)out.size(), nullptr, nullptr);
		out.resize(n > 0 ? n : 0);
		return out;
	}

	// UTF-8 -> cp1251 (unmappable characters become '?')
	xr_string from_utf8(const std::string& s)
	{
		if (s.empty())
			return xr_string();
		std::wstring w(s.size(), L'\0');
		int wn = MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), &w[0], (int)w.size());
		std::string out(wn, '\0');
		int n = WideCharToMultiByte(1251, 0, w.data(), wn, &out[0], (int)out.size(), "?", nullptr);
		out.resize(n > 0 ? n : 0);
		return out.c_str();
	}

	// Logger thread, under logCallbackGuard: only short local locks, no I/O, no Msg
	void on_log(const char* line)
	{
		std::string utf8 = to_utf8(line);
		std::lock_guard<std::mutex> lock(log_lock);
		u64 seq = ++last_seq;
		if (mark_id && !strncmp(line, mark_prefix, sizeof(mark_prefix) - 1))
		{
			u64 id = 0;
			int n = 0;
			if (sscanf(line + sizeof(mark_prefix) - 1, "%llu%n", &id, &n) == 1 && id == mark_id)
			{
				if (!xr_strcmp(line + sizeof(mark_prefix) - 1 + n, " done"))
				{
					mark_end = seq;
					mark_cv.notify_all();
				}
				else if (!line[sizeof(mark_prefix) - 1 + n])
					mark_begin = seq;
			}
		}
		if (conn_id)
			queue_event({ { "event", "log" }, { "data", { { "seq", seq }, { "line", utf8 } } } });
		log_ring.push_back({ seq, std::move(utf8) });
		if (log_ring.size() > log_ring_size)
			log_ring.pop_front();
	}

	void queue_error(u32 conn, const json& id, int code, LPCSTR message)
	{
		queue_out(conn, dump({ { "id", id }, { "error", { { "code", code }, { "message", message } } } }));
	}

	// Net thread: parse one line and queue it for the main thread
	void on_line(u32 conn, const std::string& line)
	{
		json msg = json::parse(line, nullptr, false);
		if (msg.is_discarded())
		{
			queue_error(conn, nullptr, err_parse, "parse error");
			return;
		}

		json id = nullptr;
		if (msg.is_object())
		{
			auto it = msg.find("id");
			if (it != msg.end())
				id = *it;
		}

		auto method = msg.is_object() ? msg.find("method") : msg.end();
		if (!msg.is_object() || method == msg.end() || !method->is_string())
		{
			queue_error(conn, id, err_invalid_request, "invalid request");
			return;
		}

		// Answered on the net thread, so it works while the main thread is busy (level load) or hung
		if (method->get_ref<const std::string&>() == "engine.heartbeat")
		{
			bool loading = pApp && pApp->IsLoading();
			queue_out(conn, dump({ { "id", id }, { "result", { { "frame", Device.dwFrame }, { "loading", loading } } } }));
			return;
		}

		auto params = msg.find("params");
		request req{ conn, id, method->get<std::string>(), params != msg.end() ? dump(*params) : "{}" };

		std::lock_guard<std::mutex> lock(in_lock);
		inbox.push_back(std::move(req));
	}

	bool send_all(SOCKET s, const std::string& data)
	{
		const char* p = data.data();
		int left = (int)data.size();
		while (left > 0)
		{
			int n = send(s, p, left, 0);
			if (n == SOCKET_ERROR)
				return false;
			p += n;
			left -= n;
		}
		return true;
	}

	// Returns false when the client should be dropped
	bool flush_outbox(SOCKET s, u32 conn)
	{
		xr_deque<outgoing> pending;
		{
			std::lock_guard<std::timed_mutex> lock(out_lock);
			pending.swap(outbox);
			queued_events = 0;
		}
		std::lock_guard<std::timed_mutex> lock(send_lock);
		for (auto& o : pending)
		{
			if (o.conn != 0 && o.conn != conn)
				continue; // response for a previous client
			if (!send_all(s, o.line))
				return false;
		}
		return true;
	}

	void serve_client(SOCKET s, u32 conn)
	{
		std::string buf;
		char chunk[4096];

		while (running)
		{
			if (!flush_outbox(s, conn))
				return;

			fd_set rd;
			FD_ZERO(&rd);
			FD_SET(s, &rd);
			timeval tv{ 0, 5000 };
			int r = select(0, &rd, nullptr, nullptr, &tv);
			if (r == SOCKET_ERROR)
				return;
			if (r == 0)
				continue;

			int n = recv(s, chunk, sizeof(chunk), 0);
			if (n <= 0)
				return;
			buf.append(chunk, n);

			size_t start = 0, nl;
			while ((nl = buf.find('\n', start)) != std::string::npos)
			{
				std::string line = buf.substr(start, nl - start);
				if (!line.empty() && line.back() == '\r')
					line.pop_back();
				if (!line.empty())
					on_line(conn, line);
				start = nl + 1;
			}
			buf.erase(0, start);

			if (buf.size() > max_line)
			{
				Msg("! [MCP] Request line too long, dropping client");
				return;
			}
		}
	}

	void net_loop()
	{
		u32 next_conn = 1;
		while (running)
		{
			SOCKET s = accept(listen_sock, nullptr, nullptr);
			if (s == INVALID_SOCKET)
				continue; // listen socket closed by stop() or transient error

			{
				std::lock_guard<std::timed_mutex> lock(out_lock);
				outbox.clear();
				queued_events = 0;
			}
			conn_id = next_conn++;
			client_sock = s;
			Msg("* [MCP] Client connected");

			serve_client(s, conn_id);

			client_sock = INVALID_SOCKET;
			conn_id = 0;
			closesocket(s);
			Msg("* [MCP] Client disconnected");
		}
	}

	// Crash handler (crashing thread, any): synchronous, bounded send of the queued events plus the crash event.
	// Does not rely on the net thread, which may be frozen.
	void on_crash()
	{
		if (prev_crash_handler)
			prev_crash_handler();
		xrLogger::FlushLogSync();
		SOCKET s = client_sock;
		u32 conn = conn_id;
		if (s == INVALID_SOCKET || !conn)
			return;

		std::string data;
		if (!send_lock.try_lock_for(std::chrono::milliseconds(1000)))
			data = "\n"; // the net thread may be stuck mid-line; a fresh line keeps the event parseable
		// send_lock stays held from here on: the net thread must not interleave, and the process exits next
		if (out_lock.try_lock_for(std::chrono::milliseconds(1000)))
		{
			for (const auto& o : outbox)
				if (o.conn == 0 || o.conn == conn)
					data += o.line;
			outbox.clear();
			queued_events = 0;
			out_lock.unlock();
		}
		LPCSTR dmp = Debug.crash_dump();
		data += dump({ { "event", "crash" }, { "data", {
			{ "reason", to_utf8(Debug.crash_reason()) },
			{ "stack", to_utf8(Debug.crash_stack()) },
			{ "dump", *dmp ? json(to_utf8(dmp)) : json(nullptr) } } } });
		data += '\n';

		DWORD ms = 2000;
		setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, (const char*)&ms, sizeof(ms));
		if (!send_all(s, data))
			return;
		// Wait (bounded) for the client to close first, so process exit does not reset the connection and lose the data
		shutdown(s, SD_SEND);
		setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, (const char*)&ms, sizeof(ms));
		char buf[256];
		DWORD t0 = GetTickCount();
		while (GetTickCount() - t0 < 3000 && recv(s, buf, sizeof(buf), 0) > 0)
			;
	}

	// Main thread: run queued requests
	class frame_hook : public pureFrame
	{
	public:
		virtual void _BCL OnFrame()
		{
			xr_vector<request> pending;
			{
				std::lock_guard<std::mutex> lock(in_lock);
				pending.swap(inbox);
			}

			for (auto& req : pending)
			{
				auto it = handlers.find(req.method.c_str());
				if (it == handlers.end())
				{
					queue_error(req.conn, req.id, err_method_not_found, "method not found");
					continue;
				}

				xr_string out;
				if (!it->second(req.params.c_str(), out))
				{
					queue_error(req.conn, req.id, err_internal, to_utf8(out.c_str()).c_str());
					continue;
				}

				json result = json::parse(out.c_str(), nullptr, false);
				if (result.is_discarded())
				{
					queue_error(req.conn, req.id, err_internal, "handler returned invalid JSON");
					continue;
				}
				queue_out(req.conn, dump({ { "id", req.id }, { "result", result } }));
			}
		}
	} frame;

	void register_builtins()
	{
		register_method("ping", [](LPCSTR, xr_string& out)
		{
			out = "\"pong\"";
			return true;
		});

		register_method("engine.info", [](LPCSTR, xr_string& out)
		{
			// The level name is empty (null c_str) while the level is loading
			shared_str level = g_pGameLevel ? g_pGameLevel->name() : shared_str();
			json info = {
				{ "protocol_version", protocol_version },
				{ "version", engine_version.c_str() },
				{ "mt", true },
				{ "pid", GetCurrentProcessId() },
				{ "frame", Device.dwFrame },
				{ "active", Device.b_is_Active != FALSE },
				{ "rendered", Device.dwRenderedFrames },
				{ "iconic", Device.m_hWnd && IsIconic(Device.m_hWnd) },
				{ "silent", Device.IsSilent() },
				{ "level", level.size() ? json(level.c_str()) : json(nullptr) },
			};
			out = dump(info).c_str();
			return true;
		});

		// {"n":K} last K lines, or {"since":S} lines with seq > S still in the ring
		register_method("log.tail", [](LPCSTR params, xr_string& out)
		{
			json p = json::parse(params, nullptr, false);
			auto since = p.is_object() ? p.find("since") : p.end();
			auto n = p.is_object() ? p.find("n") : p.end();
			bool by_since = since != p.end() && since->is_number_integer();
			if (!by_since && (n == p.end() || !n->is_number_unsigned()))
			{
				out = "expected params {\"n\":K} or {\"since\":S}";
				return false;
			}

			xr_vector<log_line> lines;
			u64 first, last;
			{
				std::lock_guard<std::mutex> lock(log_lock);
				last = last_seq;
				first = log_ring.empty() ? last + 1 : log_ring.front().seq;
				size_t count;
				if (by_since)
				{
					s64 s = since->get<s64>();
					count = s < (s64)first ? log_ring.size() : s >= (s64)last ? 0 : (size_t)(last - (u64)s);
				}
				else
					count = std::min<u64>(n->get<u64>(), log_ring.size());
				lines.assign(log_ring.end() - count, log_ring.end());
			}

			json arr = json::array();
			for (auto& l : lines)
				arr.push_back({ { "seq", l.seq }, { "line", std::move(l.line) } });
			out = dump({ { "lines", std::move(arr) }, { "first_seq", first }, { "last_seq", last } }).c_str();
			return true;
		});

		// {"cmd":"..."} -> {"lines":[...]} log lines printed while the command ran
		register_method("console.exec", [](LPCSTR params, xr_string& out)
		{
			xr_string cmd;
			if (!get_string_param(params, "cmd", cmd))
			{
				out = "expected params {\"cmd\":\"...\"}";
				return false;
			}
			if (!Console)
			{
				out = "console is not ready";
				return false;
			}
			out = "{" + run_logged([&]() { Console->Execute(cmd.c_str()); }) + "}";
			return true;
		});
	}
}

void register_method(LPCSTR name, const handler& h)
{
	handlers[name] = h;
}

void send_event(LPCSTR name, LPCSTR data_json)
{
	if (!conn_id)
		return;
	json data = json::parse(data_json, nullptr, false);
	if (data.is_discarded())
		data = nullptr;
	queue_event({ { "event", name }, { "data", data } });
}

xr_string run_logged(const std::function<void()>& f)
{
	u64 id = ++last_mark_id;
	{
		std::lock_guard<std::mutex> lock(log_lock);
		mark_id = log_captured ? id : 0;
		mark_begin = mark_end = 0;
	}
	Msg("%s%llu", mark_prefix, id);
	f();
	Msg("%s%llu done", mark_prefix, id);

	json arr = json::array();
	u64 truncated = 0;
	bool complete;
	{
		// Bounded wait: the logger thread only takes log_lock briefly and never waits on the main thread
		std::unique_lock<std::mutex> lock(log_lock);
		complete = mark_id && mark_cv.wait_for(lock, std::chrono::milliseconds(mark_wait_ms), [] { return mark_end != 0; });
		if (mark_begin)
		{
			u64 end = complete ? mark_end : last_seq + 1;
			for (const auto& l : log_ring)
				if (l.seq > mark_begin && l.seq < end)
				{
					if (arr.size() < max_exec_lines)
						arr.push_back(l.line);
					else
						truncated++;
				}
		}
		mark_id = 0;
	}
	if (!complete)
		Msg("! [MCP] exec #%llu: log lines incomplete (logger did not catch up in %u ms)", id, (u32)mark_wait_ms);
	return xr_string("\"lines\":") + dump(arr).c_str() + ",\"truncated\":" + std::to_string(truncated).c_str();
}

bool get_string_param(LPCSTR params, LPCSTR key, xr_string& out)
{
	json p = json::parse(params, nullptr, false);
	auto it = p.is_object() ? p.find(key) : p.end();
	if (it == p.end() || !it->is_string())
		return false;
	out = from_utf8(it->get_ref<const std::string&>());
	return true;
}

xr_string to_json_string(LPCSTR s)
{
	return dump(json(to_utf8(s))).c_str();
}

void capture_log()
{
	if (log_captured)
		return;
	log_captured = true;
	xrLogger::AddLogCallback(on_log);
}

void start(u16 port, LPCSTR version)
{
	engine_version = version;

	WSADATA wsa;
	if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0)
	{
		Msg("! [MCP] WSAStartup failed");
		return;
	}

	listen_sock = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
	sockaddr_in addr{};
	addr.sin_family = AF_INET;
	addr.sin_port = htons(port);
	addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);

	if (listen_sock == INVALID_SOCKET
		|| bind(listen_sock, (sockaddr*)&addr, sizeof(addr)) == SOCKET_ERROR
		|| listen(listen_sock, 1) == SOCKET_ERROR)
	{
		Msg("! [MCP] Failed to listen on 127.0.0.1:%u (error %d)", port, WSAGetLastError());
		if (listen_sock != INVALID_SOCKET)
			closesocket(listen_sock);
		listen_sock = INVALID_SOCKET;
		WSACleanup();
		return;
	}

	sockaddr_in bound{};
	int bound_len = sizeof(bound);
	if (getsockname(listen_sock, (sockaddr*)&bound, &bound_len) == 0)
		port = ntohs(bound.sin_port);
	write_port_file(port);

	register_builtins();
	mcp_perf::register_methods();
	Device.seqFrame.Add(&frame, REG_PRIORITY_LOW);
	prev_crash_handler = Debug.get_crashhandler(); // chain: the console dumps its variables first
	Debug.set_crashhandler(on_crash);

	running = true;
	net_thread = std::thread(net_loop);
	Msg("* [MCP] Listening on 127.0.0.1:%u", port);
}

void stop()
{
	if (log_captured)
	{
		xrLogger::RemoveLogCallback(on_log);
		log_captured = false;
	}
	if (!running)
		return;

	running = false;
	closesocket(listen_sock); // unblocks accept()
	listen_sock = INVALID_SOCKET;
	net_thread.join();

	Device.seqFrame.Remove(&frame);
	if (Debug.get_crashhandler() == on_crash)
		Debug.set_crashhandler(prev_crash_handler);
	WSACleanup();
	if (port_file[0])
		DeleteFile(port_file);
	Msg("* [MCP] Stopped");
}
}
