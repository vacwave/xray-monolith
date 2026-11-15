"""MCP supervisor for X-Ray Monolith (Stalker Anomaly): launches and drives game instances.

Agent <-> MCP stdio (this process) <-> TCP JSON lines <-> engine (-mcp_port). stdout belongs to MCP; logs go to stderr.
"""
import ctypes
import ctypes.wintypes as wt
import functools
import glob
import itertools
import json
import logging
import msvcrt
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

log = logging.getLogger("anomaly-mcp")

REPO = Path(__file__).resolve().parent.parent
RUN_DIR = REPO / ".mcp-run"
TEMPLATE_USER_LTX = Path(__file__).resolve().parent / "templates" / "user.ltx"
DEFAULT_EXE = REPO / "_build" / "_game" / "bin_dbg" / "AnomalyDX11AVX.exe"
DEFAULT_GAME_ROOT = Path(r"C:\Anomaly")
GAMMA_ROOT = Path(r"C:\GAMMA")
STATE_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "anomaly-mcp"
SLOT_DIR = STATE_DIR / "slots"

RING_SIZE = 20000  # log lines kept per instance
MAX_OUTPUT_CHARS = 20000  # per tool result, lines part
MAX_LINE_CHARS = 2000
QUICK_EXIT_S = 15  # an exit within this time after launch is reported as a failed/blocked launch
PORT_WAIT_S = 120
STOP_WAIT_S = 30


def load_caps():
    caps = {"vanilla": 3, "gamma": 2}
    cfg = STATE_DIR / "config.json"
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            for k in caps:
                if isinstance(data.get(f"max_{k}"), int):
                    caps[k] = data[f"max_{k}"]
        except Exception as e:
            log.warning("bad %s: %s", cfg, e)
    for k in caps:
        v = os.environ.get(f"ANOMALY_MCP_MAX_{k.upper()}")
        if v:
            caps[k] = int(v)
    return caps


# ---------------------------------------------------------------- Win32 helpers

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
SYNCHRONIZE = 0x00100000
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
k32.OpenProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
k32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
k32.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
k32.CloseHandle.argtypes = [wt.HANDLE]
k32.CreateJobObjectW.restype = wt.HANDLE
k32.CreateJobObjectW.argtypes = [wt.LPVOID, wt.LPCWSTR]
k32.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, wt.LPVOID, wt.DWORD]
k32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]


class ProcHandle:
    """Process handle by pid (works for launched and attached games)."""

    def __init__(self, pid):
        self.pid = pid
        self.h = k32.OpenProcess(SYNCHRONIZE | PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not self.h:
            raise OSError(f"cannot open process {pid} (error {ctypes.get_last_error()})")

    def wait(self, timeout_s=None):
        """True when the process has exited."""
        ms = 0xFFFFFFFF if timeout_s is None else int(timeout_s * 1000)
        return k32.WaitForSingleObject(self.h, ms) == 0

    def exit_code(self):
        code = wt.DWORD()
        k32.GetExitCodeProcess(self.h, ctypes.byref(code))
        return None if code.value == STILL_ACTIVE else code.value

    def kill(self):
        k32.TerminateProcess(self.h, 1)


class _JobLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]


class _JobExtLimits(ctypes.Structure):
    _fields_ = [("Basic", _JobLimits), ("Io", ctypes.c_uint64 * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


_job = None


def kill_with_supervisor(pid):
    """Games we launch die with this process, so a dead supervisor never leaks a running game past its slot."""
    global _job
    if _job is None:
        _job = k32.CreateJobObjectW(None, None)
        info = _JobExtLimits()
        info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject(_job, 9, ctypes.byref(info), ctypes.sizeof(info))  # ExtendedLimitInformation
    h = k32.OpenProcess(0x0100 | 0x0001, False, pid)  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
    if not h or not k32.AssignProcessToJobObject(_job, h):
        log.warning("could not add pid %d to the job (error %d)", pid, ctypes.get_last_error())
    if h:
        k32.CloseHandle(h)


# ---------------------------------------------------------------- slots

class Slot:
    """Machine-wide instance slot: an exclusively locked file, released by the OS if this process dies."""

    def __init__(self, profile, index, f):
        self.profile, self.index, self.f = profile, index, f
        self.lock = threading.Lock()

    @property
    def name(self):
        return f"{self.profile}-{self.index}"

    def release(self):
        with self.lock:
            if self.f:
                try:
                    self.f.seek(0)
                    msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
                self.f.close()
                self.f = None


def acquire_slot(profile):
    cap = load_caps()[profile]
    SLOT_DIR.mkdir(parents=True, exist_ok=True)
    for i in range(1, cap + 1):
        f = open(SLOT_DIR / f"{profile}-{i}.lock", "a+b")
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return Slot(profile, i, f)
        except OSError:
            f.close()
    raise RuntimeError(f"instance cap reached: {cap} {profile} instances already run on this machine "
                       f"(slots in {SLOT_DIR}; cap via ANOMALY_MCP_MAX_{profile.upper()} or {STATE_DIR / 'config.json'})")


# ---------------------------------------------------------------- instance

class Instance:
    def __init__(self, iid, appdata=None, slot=None, profile="vanilla"):
        self.id = iid
        self.appdata = Path(appdata) if appdata else None
        self.slot = slot
        self.profile = profile
        self.proc = None  # ProcHandle
        self.pid = None
        self.port = None
        self.exit_code = None
        self.dead = False
        self.started = time.time()
        self.ring = deque(maxlen=RING_SIZE)  # (seq, line, arrival time or 0 for gap fills)
        self.last_seq = 0
        self.lock = threading.Lock()
        self.sock = None
        self.wfile_lock = threading.Lock()
        self.pending = {}  # id -> [event, response]
        self.ids = itertools.count(1)
        self.filling = False
        self.stopping = False

    # -- log buffer

    def _merge(self, items, live):
        """items: [(seq, line)] in any order. Caller holds self.lock."""
        now = time.time() if live else 0
        if live:  # engine events arrive in seq order, so an old seq was already fetched by log.tail
            if items[0][0] > self.last_seq:
                self.ring.append((items[0][0], items[0][1], now))
                self.last_seq = items[0][0]
            return
        merged = {e[0]: e for e in self.ring}
        for seq, line in items:
            if seq not in merged:
                merged[seq] = (seq, line, now)
        self.ring = deque((merged[s] for s in sorted(merged)), maxlen=RING_SIZE)
        if self.ring:
            self.last_seq = max(self.last_seq, self.ring[-1][0])

    def fill_gap(self, since):
        try:
            res = self.call("log.tail", {"since": since}, timeout=15)
            with self.lock:
                self._merge([(l["seq"], l["line"]) for l in res["lines"]], live=False)
        except Exception as e:
            log.warning("[%s] gap fill failed: %s", self.id, e)
        finally:
            self.filling = False

    def lines(self, n=None, since=None):
        with self.lock:
            items = list(self.ring)
        if since is not None:
            items = [e for e in items if e[0] > since]
        if n is not None:
            items = items[-n:] if n > 0 else []
        return items

    def rate(self, window=10.0):
        now = time.time()
        count = 0
        with self.lock:
            for e in reversed(self.ring):
                if e[2] and e[2] < now - window:
                    break
                if e[2]:
                    count += 1
        return round(count / window, 1)

    # -- connection

    def connect(self, port, timeout=10):
        self.port = port
        s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        s.settimeout(None)
        self.sock = s
        threading.Thread(target=self._reader, args=(s,), daemon=True, name=f"reader-{self.id}").start()
        res = self.call("log.tail", {"since": self.last_seq}, timeout=timeout)
        with self.lock:
            self._merge([(l["seq"], l["line"]) for l in res["lines"]], live=False)

    def _reader(self, s):
        f = s.makefile("rb")
        try:
            for raw in f:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    log.warning("[%s] bad line from engine: %r", self.id, raw[:200])
                    continue
                if msg.get("event") == "log":
                    d = msg.get("data") or {}
                    seq, line = d.get("seq", 0), d.get("line", "")
                    gap = None
                    with self.lock:
                        if self.last_seq and seq > self.last_seq + 1 and not self.filling:
                            self.filling, gap = True, self.last_seq
                        self._merge([(seq, line)], live=True)
                    if gap is not None:
                        threading.Thread(target=self.fill_gap, args=(gap,), daemon=True).start()
                elif "id" in msg:
                    p = self.pending.get(msg["id"])
                    if p:
                        p[1] = msg
                        p[0].set()
        except OSError:
            pass
        finally:
            if self.sock is s:
                self.sock = None
            try:
                s.close()
            except OSError:
                pass
            for p in list(self.pending.values()):
                p[0].set()
            log.info("[%s] disconnected", self.id)
            if not self.dead and not self.stopping and self.proc:
                threading.Thread(target=self._reconnect, daemon=True).start()

    def _reconnect(self):
        while not self.dead and not self.stopping and self.sock is None:
            time.sleep(2)
            try:
                self.connect(self.port)
                log.info("[%s] reconnected", self.id)
                return
            except Exception:
                pass

    def call(self, method, params=None, timeout=30):
        s = self.sock
        if s is None:
            raise RuntimeError(f"instance {self.id} is not connected" + (" (dead)" if self.dead else ""))
        rid = next(self.ids)
        ev = threading.Event()
        self.pending[rid] = [ev, None]
        try:
            data = (json.dumps({"id": rid, "method": method, "params": params or {}}) + "\n").encode()
            with self.wfile_lock:
                s.sendall(data)
            if not ev.wait(timeout):
                raise TimeoutError(f"{method}: no reply in {timeout} s (the main thread may be busy, e.g. loading a level)")
            msg = self.pending[rid][1]
            if msg is None:
                raise RuntimeError(f"{method}: connection lost")
            if "error" in msg:
                raise RuntimeError(f"{method} failed: {msg['error'].get('message')}")
            return msg.get("result")
        finally:
            self.pending.pop(rid, None)

    # -- process

    def watch(self, proc):
        self.proc = proc
        self.pid = proc.pid
        threading.Thread(target=self._watcher, daemon=True, name=f"watch-{self.id}").start()

    def _watcher(self):
        self.proc.wait()
        self.exit_code = self.proc.exit_code()
        self.dead = True
        log.info("[%s] process %s exited with code %s", self.id, self.pid, self.exit_code)
        s = self.sock
        if s:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self.slot:
            self.slot.release()

    def log_file_tail(self, n=30):
        """Newest log file in the instance's appdata: (path, last n lines) or None."""
        if not self.appdata:
            return None
        files = sorted(glob.glob(str(self.appdata / "logs" / "*.log")), key=os.path.getmtime)
        if not files:
            return None
        try:
            text = Path(files[-1]).read_text(encoding="cp1251", errors="replace")
        except OSError:
            return None
        return files[-1], text.splitlines()[-n:]


# ---------------------------------------------------------------- output safeguards

def collapse(lines):
    """Consecutive identical lines -> 'line ×N'."""
    out = []
    prev, count = None, 0
    for l in lines:
        if l == prev:
            count += 1
            continue
        if prev is not None:
            out.append(prev if count == 1 else f"{prev} ×{count}")
        prev, count = l, 1
    if prev is not None:
        out.append(prev if count == 1 else f"{prev} ×{count}")
    return out


def bound(lines, keep="tail", max_chars=MAX_OUTPUT_CHARS):
    """Collapse repeats, clip long lines, cap total size. Returns (lines, note); note says what was omitted."""
    total = len(lines)
    lines = collapse(lines)
    collapsed = total - len(lines)
    clipped = 0
    for i, l in enumerate(lines):
        if len(l) > MAX_LINE_CHARS:
            lines[i] = l[:MAX_LINE_CHARS] + f" …[+{len(l) - MAX_LINE_CHARS} chars]"
            clipped += 1
    kept, size = [], 0
    seq = lines if keep == "head" else reversed(lines)
    for l in seq:
        if size + len(l) + 1 > max_chars:
            break
        kept.append(l)
        size += len(l) + 1
    if keep != "head":
        kept.reverse()
    dropped = len(lines) - len(kept)
    notes = []
    if collapsed:
        notes.append(f"{collapsed} repeated lines collapsed (×N)")
    if clipped:
        notes.append(f"{clipped} long lines clipped to {MAX_LINE_CHARS} chars")
    if dropped:
        where = "last" if keep == "head" else "first"
        notes.append(f"{dropped} {where} lines omitted (output cap {max_chars} chars)")
    return kept, "; ".join(notes) or None


# ---------------------------------------------------------------- supervisor state

instances = {}
current = [None]
reg_lock = threading.Lock()


def get(instance=None):
    if instance:
        if instance not in instances:
            raise ValueError(f"no instance {instance!r}; known: {list(instances) or 'none'}")
        return instances[instance]
    if current[0] in instances:
        return instances[current[0]]
    raise ValueError("no instances; use game_launch or game_attach")


def add(inst):
    with reg_lock:
        instances[inst.id] = inst
        current[0] = inst.id


def read_port_file(path):
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def seed_appdata(appdata, game_root):
    appdata.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(TEMPLATE_USER_LTX, appdata / "user.ltx")
    src = game_root / "appdata" / "shaders_cache"
    dst = appdata / "shaders_cache"
    if src.is_dir() and not dst.exists():
        shutil.copytree(src, dst)


def copy_missing_dlls(game_root, exe_dir):
    copied = []
    for dll in (game_root / "bin").glob("*.dll"):
        if not (exe_dir / dll.name).exists():
            shutil.copy2(dll, exe_dir / dll.name)
            copied.append(dll.name)
    return copied


def info_or_error(inst, timeout=5):
    try:
        return inst.call("engine.info", timeout=timeout)
    except Exception as e:
        return {"error": str(e)}


def launch(exe, game_root, show_window, new_game, extra_args, timeout):
    exe = Path(exe) if exe else DEFAULT_EXE
    game_root = Path(game_root) if game_root else DEFAULT_GAME_ROOT
    if not exe.is_file():
        raise FileNotFoundError(f"exe not found: {exe}")
    if not (game_root / "fsgame.ltx").is_file():
        raise FileNotFoundError(f"no fsgame.ltx in {game_root}")
    profile = "gamma" if game_root.resolve() == GAMMA_ROOT.resolve() else "vanilla"
    slot = acquire_slot(profile)
    try:
        iid = slot.name
        appdata = RUN_DIR / iid / "appdata"
        copied = copy_missing_dlls(game_root, exe.parent)
        seed_appdata(appdata, game_root)
        port_file = appdata / "mcp_port"
        port_file.unlink(missing_ok=True)
        args = [str(exe), "-fsltx", str(game_root / "fsgame.ltx"), "-appdata", str(appdata), "-mcp_port", "0"]
        if show_window:
            args.append("-showwindow")
        if new_game:
            args += ["-start", "server(all/single/alife/new)", "client(localhost)"]
        if extra_args:
            args += extra_args.split() if isinstance(extra_args, str) else list(extra_args)
        p = subprocess.Popen(args, cwd=str(game_root), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True)
        kill_with_supervisor(p.pid)
        inst = Instance(iid, appdata, slot, profile)
        inst.watch(ProcHandle(p.pid))
    except Exception:
        slot.release()
        raise
    with reg_lock:
        instances[iid] = inst  # replaces a dead instance that held this slot before
        current[0] = iid
    log.info("[%s] launched pid %d: %s", iid, p.pid, " ".join(args))

    t0 = time.time()
    while True:
        if inst.dead:
            quick = time.time() - t0 < QUICK_EXIT_S
            why = ("exited at once with code 0: another instance with the same exe dir and appdata holds the presence "
                   "mutex, or startup failed" if quick and inst.exit_code == 0
                   else f"exited during startup with code {inst.exit_code}")
            tail = inst.log_file_tail()
            raise RuntimeError(f"launch failed: {why}" +
                               (f"\nlog tail ({tail[0]}):\n" + "\n".join(tail[1]) if tail else " (no log)"))
        port = read_port_file(port_file)
        if port:
            break
        if time.time() - t0 > PORT_WAIT_S:
            raise TimeoutError(f"no mcp_port file after {PORT_WAIT_S} s; instance {iid} left running (pid {p.pid})")
        time.sleep(0.25)
    inst.connect(port)
    info = info_or_error(inst, timeout=timeout)
    return {"instance": iid, "pid": p.pid, "port": port, "profile": profile, "appdata": str(appdata),
            "args": args[1:], "copied_dlls": copied, "engine_info": info}


def attach(port, appdata):
    if not port:
        if not appdata:
            raise ValueError("give port or appdata")
        port = read_port_file(Path(appdata) / "mcp_port")
        if not port:
            raise FileNotFoundError(f"no mcp_port file in {appdata}")
    iid = f"attach-{port}"
    inst = Instance(iid, appdata)
    inst.connect(port)
    info = inst.call("engine.info", timeout=10)
    if info.get("pid"):
        try:
            inst.watch(ProcHandle(info["pid"]))
        except OSError as e:
            log.warning("[%s] %s", iid, e)
    add(inst)
    return {"instance": iid, "pid": inst.pid, "port": port, "engine_info": info}


def stop(inst):
    if inst.dead:
        if inst.slot:
            inst.slot.release()
        return {"instance": inst.id, "already_dead": True, "exit_code": inst.exit_code}
    inst.stopping = True
    graceful = False
    try:
        inst.call("console.exec", {"cmd": "quit"}, timeout=5)
    except Exception as e:
        log.info("[%s] quit: %s", inst.id, e)
    if inst.proc:
        graceful = inst.proc.wait(STOP_WAIT_S)
        if not graceful:
            inst.proc.kill()
            inst.proc.wait(10)
        inst.exit_code = inst.proc.exit_code()
        inst.dead = True
    elif inst.sock:
        inst.sock.close()
    if inst.slot:
        inst.slot.release()
    return {"instance": inst.id, "graceful": graceful, "exit_code": inst.exit_code}


def status(inst, n):
    alive = not inst.dead
    items = inst.lines(n=n)
    lines, note = bound([l for _, l, _ in items])
    return {"instance": inst.id, "pid": inst.pid, "alive": alive, "exit_code": inst.exit_code, "port": inst.port,
            "connected": inst.sock is not None, "profile": inst.profile, "slot": inst.slot.name if inst.slot else None,
            "appdata": str(inst.appdata) if inst.appdata else None, "uptime_s": round(time.time() - inst.started),
            "engine_info": info_or_error(inst) if inst.sock else None,
            "log_rate_lines_per_s": inst.rate(), "last_seq": inst.last_seq, "buffered_lines": len(inst.ring),
            "last_lines": lines, "omitted": note}


def exec_result(res):
    lines, note = bound(res.get("lines", []), keep="head")
    if res.get("truncated"):
        note = (note + "; " if note else "") + f"engine dropped {res['truncated']} more lines (cap 500), see log_tail"
    out = {"lines": lines, "omitted": note}
    if "results" in res:
        out = {"results": res["results"], **out}
    return out


# ---------------------------------------------------------------- MCP tools

server = MCPServer("anomaly", instructions=(
    "Drives X-Ray Monolith (Stalker Anomaly) game instances. Start with game_launch, then use lua_exec / "
    "console_exec / log_tail / game_status, and game_stop when done. Each tool takes an optional instance id "
    "(default: the last launched or attached)."))


def run(fn, *args):
    return anyio.to_thread.run_sync(lambda: fn(*args))


def tool(f):
    """server.tool() that reports any exception to the agent with its message (the SDK hides non-ToolError text)."""
    @functools.wraps(f)
    async def wrapper(*args, **kwargs):
        try:
            return await f(*args, **kwargs)
        except ToolError:
            raise
        except Exception as e:
            log.info("%s failed: %s: %s", f.__name__, type(e).__name__, e)
            raise ToolError(f"{type(e).__name__}: {e}") from e
    return server.tool()(wrapper)


@tool
async def game_launch(exe: str | None = None, game_root: str | None = None, show_window: bool = False,
                      new_game: bool = False, extra_args: str | None = None, timeout: float = 180) -> dict:
    """Launch a game instance (silent/minimized unless show_window) and connect to it.

    exe: default <repo>/_build/_game/bin_dbg/AnomalyDX11AVX.exe. game_root: default C:\\Anomaly (C:\\GAMMA counts
    against the GAMMA cap). new_game: start a new game straight into a level. extra_args: more engine args.
    timeout: seconds to wait for the first engine.info reply after connecting (a new_game level load blocks it).
    Refused when the machine-wide instance cap is reached."""
    return await run(launch, exe, game_root, show_window, new_game, extra_args, timeout)


@tool
async def game_attach(port: int | None = None, appdata: str | None = None) -> dict:
    """Attach to a running game started with -mcp_port, by TCP port or by its -appdata dir (reads mcp_port there).
    Attached games take no slot and are not killed when the supervisor exits."""
    return await run(attach, port, appdata)


@tool
async def game_status(instance: str | None = None, lines: int = 20) -> dict:
    """pid, alive, port, engine.info, log rate (lines/s) and the last log lines of an instance."""
    return await run(status, get(instance), max(0, min(lines, 200)))


@tool
async def game_list() -> dict:
    """Instances known to this supervisor, plus the machine-wide caps."""
    return {"current": current[0], "caps": load_caps(), "instances": [
        {"instance": i.id, "pid": i.pid, "alive": not i.dead, "exit_code": i.exit_code, "port": i.port,
         "connected": i.sock is not None, "profile": i.profile, "slot": i.slot.name if i.slot else None,
         "last_seq": i.last_seq} for i in instances.values()]}


@tool
async def game_stop(instance: str | None = None) -> dict:
    """Quit the game gracefully (console 'quit'), kill it after 30 s, and release its slot. The log buffer stays."""
    return await run(stop, get(instance))


@tool
async def console_exec(cmd: str, instance: str | None = None, timeout: float = 30) -> dict:
    """Run an engine console command; returns the log lines it printed (repeats collapsed, size capped)."""
    inst = get(instance)
    return exec_result(await run(inst.call, "console.exec", {"cmd": cmd}, timeout))


@tool
async def lua_exec(code: str, instance: str | None = None, timeout: float = 30) -> dict:
    """Run Lua in the game's script engine (main thread). An expression list returns its values as strings
    (e.g. 'level.name()'); statements run as a chunk. print() output and other log lines come back in lines.
    Errors are returned as tool errors with the traceback."""
    inst = get(instance)
    return exec_result(await run(inst.call, "lua.exec", {"code": code}, timeout))


@tool
async def log_tail(instance: str | None = None, n: int = 100, grep: str | None = None,
                   since: int | None = None) -> dict:
    """Last n (max 1000) log lines from the supervisor buffer (survives reconnects and game exit).
    grep: regex filter applied before taking n. since: only lines with seq > since. Repeats are collapsed."""
    inst = get(instance)
    n = max(1, min(n, 1000))
    items = inst.lines(since=since)
    if grep:
        rx = re.compile(grep)
        items = [e for e in items if rx.search(e[1])]
    matched = len(items)
    items = items[-n:]
    lines, note = bound([l for _, l, _ in items])
    return {"instance": inst.id, "alive": not inst.dead, "first_seq": items[0][0] if items else None,
            "last_seq": items[-1][0] if items else inst.last_seq, "matched": matched, "returned_raw": len(items),
            "lines": lines, "omitted": note}


def main():
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server.run("stdio")
    # stdin closed: quit launched games gracefully (the job object kills any that remain when we exit)
    ts = [threading.Thread(target=stop, args=(i,)) for i in instances.values() if i.slot and not i.dead]
    for t in ts:
        t.start()
    for t in ts:
        t.join()


if __name__ == "__main__":
    main()
