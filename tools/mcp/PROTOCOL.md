# Anomaly MCP engine protocol (v0.0x series)

Contract between the engine endpoint (`src/xrGame/mcp_server.cpp`, built with
`MCP_SERVER`) and the Python bridge (`tools/mcp/`). Both sides implement exactly
this; change this file first if either side needs something else.

## Transport
- TCP, `127.0.0.1`, port **28015** (engine listens; always on in `MCP_SERVER` builds).
- Up to **4 concurrent clients**. Each client sends one request, then waits for
  its reply before sending the next (strict request/response per connection).
- The bridge opens a **fresh connection per tool call** and closes it afterwards.
- No auth, no TLS. Loopback only.

## Framing
Every message in either direction:

    u32 length (little-endian)  |  `length` bytes of UTF-8 payload

Max payload 1 MiB. Payload may contain newlines (Lua code, log text).

## Requests
Payload = `<op>` optionally followed by one space and the argument (the rest of
the payload, verbatim).

| op        | argument          | where it runs          |
|-----------|-------------------|------------------------|
| `status`  | none              | listener thread, atomics only |
| `log`     | `<n>` lines (default 50, max = ring size 2000) | listener thread, under the ring-buffer lock |
| `console` | console command   | queued → main thread (`Console->Execute`) |
| `eval`    | Lua chunk         | queued → main thread (`mcp_helper.eval`) |

Unknown op → `-unknown op: <op>`.

## Replies
Payload = one status byte followed by text:
- `+` success, text = result (may be empty)
- `-` error, text = message

### `status` reply text (key=value, one per line)

    frame=<u32 frame counter from the main thread>
    paused=<0|1>
    level=<0|1 (a level is loaded, g_pGameLevel != nullptr)>
    pending=<number of queued main-thread requests not yet executed>

Fields may be appended later; the bridge must ignore unknown keys.

### `log` reply text
The last `n` log lines joined with `\n` (no trailing newline). Lines are
truncated to 1024 bytes in the ring buffer. The engine log is not guaranteed
UTF-8; the bridge decodes with `errors="replace"`.

### `console` reply
`+` with empty text once the command has executed on the main thread.
Console output is not captured in v1; read it with `log`.

### `eval` reply
The engine calls the Lua function `mcp_helper.eval(code)` (from
`gamedata/scripts/mcp_helper.script`) with the chunk as a string. That function
returns **one string** whose first character is `+` or `-`, which the engine
sends back unchanged as the reply payload.

- `+<serialized return values>` — values serialized by `mcp_helper.serialize`,
  one per line; empty text when the chunk returned nothing.
- `-<error message>` — compile or runtime error; a Lua traceback is also
  printed to the engine log.

If `mcp_helper` cannot be loaded (db0 not deployed) the engine replies
`-mcp_helper.script not available`.

## Busy engine
Queued ops wait until the main thread runs `seqFrame` again (not during level
loads). The engine never times out a queued request. The **bridge** applies a
per-tool timeout; on timeout it closes the connection. The request still
executes later; the engine drops the reply if the client is gone.
`status` and `log` are served while queued ops are waiting, so a client can
tell "busy" (status answers, pending > 0 or frame not advancing) from "dead"
(connection refused / process gone).

## Lifecycle
Listener thread starts in `CGamePersistent::OnAppStart` and stops in
`CGamePersistent::OnAppEnd`. The main-thread drain runs from `Device.seqFrame`.
