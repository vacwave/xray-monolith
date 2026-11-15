#include "stdafx.h"
#include "mcp_server.h"
#include "IGame_Level.h"

#include <winsock2.h>
#include <ws2tcpip.h>
#include <thread>
#include <mutex>
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

	std::mutex in_lock;
	xr_vector<request> inbox;
	std::mutex out_lock;
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
		std::lock_guard<std::mutex> lock(out_lock);
		outbox.push_back({ conn, std::move(line) });
	}

	// Never blocks on the client: a slow reader loses the oldest events (recoverable via log.tail)
	void queue_event(const json& msg)
	{
		std::string line = dump(msg);
		line += '\n';
		std::lock_guard<std::mutex> lock(out_lock);
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

	// Logger thread, under logCallbackGuard: only short local locks, no I/O, no Msg
	void on_log(const char* line)
	{
		std::string utf8 = to_utf8(line);
		std::lock_guard<std::mutex> lock(log_lock);
		u64 seq = ++last_seq;
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
			std::lock_guard<std::mutex> lock(out_lock);
			pending.swap(outbox);
			queued_events = 0;
		}
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
				std::lock_guard<std::mutex> lock(out_lock);
				outbox.clear();
				queued_events = 0;
			}
			conn_id = next_conn++;
			Msg("* [MCP] Client connected");

			serve_client(s, conn_id);

			conn_id = 0;
			closesocket(s);
			Msg("* [MCP] Client disconnected");
		}
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
					queue_error(req.conn, req.id, err_internal, out.c_str());
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
			json info = {
				{ "protocol_version", protocol_version },
				{ "version", engine_version.c_str() },
				{ "mt", true },
				{ "frame", Device.dwFrame },
				{ "active", Device.b_is_Active != FALSE },
				{ "level", g_pGameLevel ? json(g_pGameLevel->name().c_str()) : json(nullptr) },
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
	Device.seqFrame.Add(&frame, REG_PRIORITY_LOW);

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
	WSACleanup();
	if (port_file[0])
		DeleteFile(port_file);
	Msg("* [MCP] Stopped");
}
}
