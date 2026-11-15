"""MCP supervisor for X-Ray Monolith (Stalker Anomaly): launches and drives game instances.

Agent <-> MCP stdio (this process) <-> TCP JSON lines <-> engine (-mcp_port). stdout belongs to MCP; logs go to stderr.
"""
import ctypes
import ctypes.wintypes as wt
import fnmatch
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
import struct
import time
import winreg
from collections import deque
from pathlib import Path

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ImageContent, TextContent

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
HEARTBEAT_S = 2  # hang monitor poll interval
WER_WAIT_S = 15  # after a crash-like exit, how long to wait for the WER dump to appear
WER_KEY = r"SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps"
# MiniDumpWithThreadInfo|IndirectlyReferencedMemory|DataSegs|HandleData|UnloadedModules|ProcessThreadData|FullMemoryInfo
MID_DUMP_FLAGS = 0x1000 | 0x40 | 0x1 | 0x4 | 0x20 | 0x100 | 0x800

DEFAULTS = {"max_vanilla": 3, "max_gamma": 2, "hang_timeout_s": 30, "load_hang_timeout_s": 600,
            "dump_keep": 5, "dump_cap_gb": 40}


def load_config():
    """DEFAULTS < %LOCALAPPDATA%\\anomaly-mcp\\config.json < env ANOMALY_MCP_<KEY upper>."""
    conf = dict(DEFAULTS)
    cfg = STATE_DIR / "config.json"
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            for k in conf:
                if isinstance(data.get(k), (int, float)):
                    conf[k] = data[k]
        except Exception as e:
            log.warning("bad %s: %s", cfg, e)
    for k in conf:
        v = os.environ.get(f"ANOMALY_MCP_{k.upper()}")
        if v:
            conf[k] = float(v) if "." in v else int(v)
    return conf


def load_caps():
    conf = load_config()
    return {"vanilla": conf["max_vanilla"], "gamma": conf["max_gamma"]}


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


dbghelp = ctypes.WinDLL("dbghelp", use_last_error=True)
dbghelp.MiniDumpWriteDump.argtypes = [wt.HANDLE, wt.DWORD, wt.HANDLE, ctypes.c_int, wt.LPVOID, wt.LPVOID, wt.LPVOID]
dbghelp.MiniDumpWriteDump.restype = wt.BOOL


def write_minidump(pid, path, flags=MID_DUMP_FLAGS):
    """Minidump of a live process (all thread stacks; MiniDumpWriteDump suspends it meanwhile)."""
    h = k32.OpenProcess(0x0400 | 0x0010 | 0x0040, False, pid)  # QUERY_INFORMATION | VM_READ | DUP_HANDLE
    if not h:
        raise OSError(f"cannot open process {pid} for dumping (error {ctypes.get_last_error()})")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            ok = dbghelp.MiniDumpWriteDump(h, pid, msvcrt.get_osfhandle(f.fileno()), flags, None, None, None)
        if not ok:
            err = ctypes.get_last_error()
            path.unlink(missing_ok=True)
            raise OSError(f"MiniDumpWriteDump failed (error {err:#x})")
    finally:
        k32.CloseHandle(h)
    return path


NTSTATUS = {
    0xC0000005: "access violation", 0xC0000006: "in-page error", 0xC00000FD: "stack overflow",
    0xC0000409: "fast fail / stack buffer overrun (__fastfail)", 0xC0000374: "heap corruption",
    0xC0000602: "fail fast exception (RaiseFailFastException)",
    0x80000003: "breakpoint", 0xC0000420: "assertion failure", 0xC0000094: "integer divide by zero",
    0xC000001D: "illegal instruction", 0xC0000096: "privileged instruction", 0xE06D7363: "unhandled C++ exception",
    0xC0000017: "out of memory", 0xC000013A: "Ctrl+C", 0xC0000142: "DLL initialization failed",
    0xC0000135: "DLL not found",
}
FAST_FAIL = {2: "STACK_COOKIE_CHECK_FAILURE", 3: "CORRUPT_LIST_ENTRY", 4: "INCORRECT_STACK", 5: "INVALID_ARG",
             6: "GS_COOKIE_INIT", 7: "FATAL_APP_EXIT (abort)", 8: "RANGE_CHECK_FAILURE", 10: "GUARD_ICALL_CHECK_FAILURE",
             13: "INVALID_FIBER_SWITCH", 14: "INVALID_SET_OF_CONTEXT", 15: "INVALID_REFERENCE_COUNT",
             18: "INVALID_JUMP_BUFFER", 19: "MRDATA_MODIFIED", 23: "INVALID_FLS_DATA", 38: "HEAP_METADATA_CORRUPTION"}


def exit_code_name(code):
    """Plain words for a process exit code."""
    if code is None:
        return None
    if code in NTSTATUS:
        return f"{code:#010x} {NTSTATUS[code]}"
    if code == 3:
        return "3: abort() / CRT termination (the engine's handlers were bypassed)"
    if code == 1:
        return "1: engine error exit (FATAL/assert) or killed"
    if code == 0:
        return "0: clean exit"
    return f"{code} ({code:#010x})"


STACK_SKIP_RX = re.compile(r"^(SymInit:|OS-Version:)|SymType:|xrDebugNew\.cpp|::on_crash$")


def compact_stack(text):
    """Engine stack text -> frames 'file (line): function', without StackWalker headers and crash-handler frames."""
    return [re.sub(r"^.*[\\/]", "", l.strip()) for l in text.splitlines()
            if l.strip() and not STACK_SKIP_RX.search(l.strip())]


def drop_cvar_dump(lines):
    """The crash handler logs ~1000 console variables; keep one line for that block."""
    out, skipped = [], None
    for l in lines:
        if skipped is None and l.endswith("Console variables: start ---"):
            skipped = 0
        elif skipped is not None and l.endswith("Console variables: end ---"):
            out.append(f"(console variables dump: {skipped} lines omitted)")
            skipped = None
        elif skipped is not None:
            skipped += 1
        else:
            out.append(l)
    if skipped is not None:
        out.append(f"(console variables dump: {skipped} lines omitted, unterminated)")
    return out


FIELD_RX = re.compile(r"^(?:\[error\])?(Expression|Function|File|Line|Description|Arguments?(?: \d)?)\s*:\s?(.*)$")


def crash_summary(engine_reason, exit_code, wer_exception, log_lines):
    """One line in plain words: FATAL description/args/expression at file:line, or exception + module, or exit code."""
    text = engine_reason or ""
    lines = text.splitlines()
    if not text:  # no crash event: look for the FATAL block the engine logs ("[error]Expression : ...")
        lines = [l.split("] ", 1)[1] if l.startswith("[") and "] " in l and not l.startswith("[error]") else l
                 for l in log_lines]
    fields = {}
    for l in lines:
        m = FIELD_RX.match(l.strip())
        if m and m.group(1) not in fields:
            fields[m.group(1)] = m.group(2).strip()
    if "Description" in fields or "Expression" in fields:
        parts = [fields.get("Description", "")]
        args = [v for k, v in fields.items() if k.startswith("Argument")]
        parts += args
        if fields.get("Expression") and fields["Expression"] != "<no expression>":
            parts.append(f"expression: {fields['Expression']}")
        out = "FATAL: " + " | ".join(p for p in parts if p)
        if fields.get("File"):
            out += f" at {fields['File']}:{fields.get('Line', '?')}"
        if fields.get("Function"):
            out += f" ({fields['Function']})"
        return out
    if text:
        return lines[0]
    if wer_exception:
        return f"crashed outside the engine handler: {wer_exception}"
    if exit_code is None:
        return "connection lost, process still running"
    return f"exited with {exit_code_name(exit_code)}"


def dump_exception(path):
    """Exception stream of a minidump: plain description (code, fast fail code, faulting module+offset) or None."""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
            sig, _, count, rva = struct.unpack("<4sIII", head)
            if sig != b"MDMP":
                return None
            f.seek(rva)
            streams = {}
            for _ in range(count):
                stype, size, srva = struct.unpack("<III", f.read(12))
                streams[stype] = (size, srva)
            if 6 not in streams:  # ExceptionStream
                return None
            f.seek(streams[6][1])
            tid, _, code, _, _, addr, nparams, _ = struct.unpack("<IIIIQQII", f.read(40))
            params = struct.unpack("<15Q", f.read(120))[:nparams]
            module = None
            if 4 in streams:  # ModuleListStream
                f.seek(streams[4][1])
                (n,) = struct.unpack("<I", f.read(4))
                mods = [struct.unpack("<QIIII", f.read(108)[:24]) for _ in range(n)]
                for base, size, _, _, name_rva in mods:
                    if base <= addr < base + size:
                        f.seek(name_rva)
                        (ln,) = struct.unpack("<I", f.read(4))
                        module = f"{Path(f.read(ln).decode('utf-16-le')).name}+{addr - base:#x}"
                        break
    except (OSError, struct.error, UnicodeDecodeError):
        return None
    text = f"exception {code:#010x}" + (f" {NTSTATUS[code]}" if code in NTSTATUS else "")
    if code == 0xC0000409 and params:
        text += f", fast fail code {params[0]}" + (f" {FAST_FAIL[params[0]]}" if params[0] in FAST_FAIL else "")
    if code == 0xC0000005 and len(params) >= 2:
        text += f" ({ {0: 'reading', 1: 'writing', 8: 'executing'}.get(params[0], 'accessing')} {params[1]:#x})"
    return text + f" at {module or hex(addr)}, thread {tid}"


def wer_folder(exe_name):
    """DumpFolder of the WER LocalDumps key for exe_name, or None when the key is missing."""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f"{WER_KEY}\\{exe_name}") as k:
            return os.path.expandvars(winreg.QueryValueEx(k, "DumpFolder")[0])
    except OSError:
        return None


def wer_set_dump_type(exe_name, full):
    """DumpType 2 (full) or 0 (custom mid dump). Global per exe name: the last launch to set it wins."""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f"{WER_KEY}\\{exe_name}", 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "DumpType", 0, winreg.REG_DWORD, 2 if full else 0)
        return None
    except OSError as e:
        return f"could not set WER DumpType ({e}); run mcp/setup_wer.ps1 as admin"


def wer_warning(exe_name):
    if wer_folder(exe_name):
        return None
    return (f"WER LocalDumps key for {exe_name} is missing: crashes that bypass the engine handler leave no dump. "
            f"Run mcp/setup_wer.ps1 once as admin.")


def prune_dumps(profile):
    """Keep the newest dump_keep dumps of a profile (across instance dirs) within dump_cap_gb; returns deleted paths."""
    conf = load_config()
    files = [p for p in RUN_DIR.glob(f"{profile}-*/dumps/*") if p.suffix.lower() in (".dmp", ".mdmp")]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    cap = conf["dump_cap_gb"] * 1024 ** 3
    deleted, total = [], 0
    for i, p in enumerate(files):
        size = p.stat().st_size
        if i >= conf["dump_keep"] or total + size > cap:
            try:
                p.unlink()
                deleted.append(str(p))
            except OSError as e:
                log.warning("cannot delete %s: %s", p, e)
        else:
            total += size
    return deleted


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


def acquire_slot(profile, index=None):
    """First free slot, or exactly slot `index` (game_restart reuses its slot and appdata)."""
    cap = load_caps()[profile]
    SLOT_DIR.mkdir(parents=True, exist_ok=True)
    for i in [index] if index else range(1, cap + 1):
        f = open(SLOT_DIR / f"{profile}-{i}.lock", "a+b")
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return Slot(profile, i, f)
        except OSError:
            f.close()
    if index:
        raise RuntimeError(f"slot {profile}-{index} is taken (another launch grabbed it)")
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
        self.events = EventBuffer()  # engine "game" events
        self.lock = threading.Lock()
        self.sock = None
        self.wfile_lock = threading.Lock()
        self.pending = {}  # id -> [event, response]
        self.ids = itertools.count(1)
        self.filling = False
        self.stopping = False
        self.launch_params = None  # launch() kwargs, for game_restart
        self.exe_name = DEFAULT_EXE.name
        conf = load_config()
        self.hang_timeout = conf["hang_timeout_s"]
        self.load_timeout = conf["load_hang_timeout_s"]
        # crash capture
        self.crash_lock = threading.Lock()
        self.crash_event = None  # data of the engine's crash event
        self.crash = None  # record for game_last_crash
        self.dumps = []  # dump files moved into dumps_dir
        self.dumps_deleted = []  # retention deletions not yet reported
        # hang monitor (engine.heartbeat is answered by the engine's net thread)
        self.hung = False
        self.loading = None
        self.stall_s = 0.0
        self.last_frame = None
        self.progress_t = time.time()

    @property
    def dumps_dir(self):
        return RUN_DIR / self.id / "dumps"

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
        self.events.fill(self, self.events.last_seq)

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
                elif msg.get("event") == "game":
                    self.events.on_live(self, msg.get("data") or {})
                elif msg.get("event") == "crash":
                    self.crash_event = msg.get("data") or {}
                    log.info("[%s] crash event: %s", self.id, (self.crash_event.get("reason") or "")[:300])
                    break  # last message; closing lets the engine exit without resetting the connection
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
                f.close()
                s.close()
            except OSError:
                pass
            for p in list(self.pending.values()):
                p[0].set()
            log.info("[%s] disconnected", self.id)
            if self.crash_event is not None:
                self.collect_crash()
            elif not self.dead and not self.stopping and self.proc:
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
        threading.Thread(target=self._monitor, daemon=True, name=f"monitor-{self.id}").start()

    def _watcher(self):
        self.proc.wait()
        self.exit_code = self.proc.exit_code()
        self.dead = True
        self.hung = False
        log.info("[%s] process %s exited with code %s", self.id, self.pid, self.exit_code)
        s = self.sock
        if s:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self.slot:
            self.slot.release()
        if self.crash_event is not None or (not self.stopping and self.exit_code != 0):
            self.collect_crash(wait_wer=True)

    @property
    def crashed(self):
        return self.crash is not None

    def _monitor(self):
        """Hung = engine frame stalled (or heartbeat unanswered) longer than the threshold; longer while loading."""
        while not self.dead:
            time.sleep(HEARTBEAT_S)
            if self.sock is None or self.stopping or self.crash_event is not None:
                continue
            try:
                hb = self.call("engine.heartbeat", timeout=5)
            except Exception:
                hb = None
            now = time.time()
            if hb:
                self.loading = bool(hb.get("loading"))
                if hb.get("frame") != self.last_frame:
                    self.last_frame, self.progress_t = hb.get("frame"), now
            self.stall_s = round(now - self.progress_t, 1)
            limit = self.load_timeout if self.loading else self.hang_timeout
            hung = self.stall_s > limit and not self.dead
            if hung != self.hung:
                log.info("[%s] %s (frame stalled %.0f s, loading %s)", self.id, "HUNG" if hung else "no longer hung",
                         self.stall_s, self.loading)
            self.hung = hung

    def write_dump(self, tag="hang"):
        """Supervisor-side minidump of the live process into dumps_dir."""
        if self.dead or not self.pid:
            raise RuntimeError(f"instance {self.id} is not running")
        path = self.dumps_dir / f"{tag}_{self.pid}_{time.strftime('%Y%m%d_%H%M%S')}.dmp"
        write_minidump(self.pid, path)
        with self.crash_lock:
            self.dumps.append(str(path))
            self.dumps_deleted += prune_dumps(self.profile)
        return path

    def _gather_dumps(self, wait_wer):
        """Move this run's engine .mdmp files and the WER dump (by pid) into dumps_dir. Caller holds crash_lock."""
        found = []
        if self.appdata:
            found += [Path(p) for p in glob.glob(str(self.appdata / "logs" / "*.mdmp"))
                      if os.path.getmtime(p) >= self.started - 1]
        folder = wer_folder(self.exe_name)
        if folder and self.pid:
            wer = Path(folder) / f"{self.exe_name}.{self.pid}.dmp"
            crash_like = self.exit_code is not None and (self.exit_code >= 0x80000000 or self.exit_code == 3)
            deadline = time.time() + (WER_WAIT_S if wait_wer and crash_like else 0)
            while not wer.exists() and time.time() < deadline:
                time.sleep(0.5)
            if wer.exists():
                # WER may still be writing: wait until the size settles
                size = -1
                while size != wer.stat().st_size:
                    size = wer.stat().st_size
                    time.sleep(1)
                found.append(wer)
        moved = {}
        for src in found:
            dst = self.dumps_dir / src.name
            try:
                self.dumps_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                self.dumps.append(str(dst))
                moved[str(src)] = str(dst)
            except OSError as e:
                log.warning("[%s] cannot move %s: %s", self.id, src, e)
        if moved:
            self.dumps_deleted += prune_dumps(self.profile)
        return moved

    def collect_crash(self, wait_wer=False):
        """(Re)build the crash record from the crash event, exit code, dumps and logs. Idempotent."""
        with self.crash_lock:
            ev = self.crash_event or {}
            moved = self._gather_dumps(wait_wer)
            dump = ev.get("dump")
            dump = moved.get(dump, dump) if dump else None
            if dump and not os.path.exists(dump):
                dump = next((d for d in self.dumps if Path(d).name == Path(dump).name), dump)
            tail = self.log_file_tail(3000)
            if tail:
                tail = (tail[0], drop_cvar_dump(tail[1])[-60:])
            buffered = drop_cvar_dump([l for _, l, _ in self.lines(n=3000)])[-60:]
            wer = next((d for d in self.dumps if d.endswith(f".{self.pid}.dmp")), None)
            exc = dump_exception(wer) if wer and os.path.exists(wer) else None
            stack = compact_stack(ev.get("stack") or "")
            self.crash = {
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "pid": self.pid,
                "exit_code": self.exit_code,
                "exit_code_name": exit_code_name(self.exit_code),
                "reason": crash_summary(ev.get("reason"), self.exit_code, exc, (tail[1] if tail else []) + buffered),
                "event_received": bool(ev),
                "engine_reason": ev.get("reason"),
                "wer_exception": exc,
                "stack": stack,
                "dump": dump,
                "wer_dump": wer,
                "dumps": [d for d in self.dumps if os.path.exists(d)],
                "log_file": tail[0] if tail else None,
                "log_tail": tail[1] if tail else [],
                "last_lines": buffered,
            }
            log.info("[%s] crash record: %s", self.id, self.crash["reason"])

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


def launch(exe=None, game_root=None, show_window=False, new_game=False, extra_args=None, timeout=180,
           full_dumps=False, hang_timeout=None, load_timeout=None, slot_index=None):
    params = {k: v for k, v in locals().items() if k != "slot_index"}
    exe = Path(exe) if exe else DEFAULT_EXE
    game_root = Path(game_root) if game_root else DEFAULT_GAME_ROOT
    if not exe.is_file():
        raise FileNotFoundError(f"exe not found: {exe}")
    if not (game_root / "fsgame.ltx").is_file():
        raise FileNotFoundError(f"no fsgame.ltx in {game_root}")
    profile = "gamma" if game_root.resolve() == GAMMA_ROOT.resolve() else "vanilla"
    warnings = [w for w in [wer_warning(exe.name)] if w]
    if not warnings:
        w = wer_set_dump_type(exe.name, full_dumps)
        if w:
            warnings.append(w)
    slot = acquire_slot(profile, slot_index)
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
        inst.launch_params, inst.exe_name = params, exe.name
        if hang_timeout:
            inst.hang_timeout = hang_timeout
        if load_timeout:
            inst.load_timeout = load_timeout
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
            try:
                dump = f"dump: {inst.write_dump('launch_timeout')}"
            except Exception as e:
                dump = f"dump failed: {e}"
            inst.stopping = True
            inst.proc.kill()
            inst.proc.wait(10)
            tail = inst.log_file_tail()
            raise TimeoutError(f"no mcp_port file after {PORT_WAIT_S} s; killed pid {p.pid}; {dump}" +
                               (f"\nlog tail ({tail[0]}):\n" + "\n".join(tail[1]) if tail else " (no log)"))
        time.sleep(0.25)
    inst.connect(port)
    info = info_or_error(inst, timeout=timeout)
    return {"instance": iid, "pid": p.pid, "port": port, "profile": profile, "appdata": str(appdata),
            "args": args[1:], "copied_dlls": copied, "full_dumps": full_dumps, "engine_info": info,
            "warnings": warnings or None}


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
    hung = inst.hung
    if not hung:  # a hung main thread never runs the quit command
        try:
            inst.call("console.exec", {"cmd": "quit"}, timeout=5)
        except Exception as e:
            log.info("[%s] quit: %s", inst.id, e)
    if inst.proc:
        graceful = not hung and inst.proc.wait(STOP_WAIT_S)
        if not graceful:
            inst.proc.kill()
            inst.proc.wait(10)
        inst.exit_code = inst.proc.exit_code()
        inst.dead = True
    elif inst.sock:
        inst.sock.close()
    if inst.slot:
        inst.slot.release()
    return {"instance": inst.id, "graceful": graceful, "killed_hung": hung, "exit_code": inst.exit_code}


def restart(inst, new_game):
    """Relaunch a launched instance with the same exe, game root and args into the same slot (and appdata)."""
    if not inst.launch_params or not inst.slot:
        raise ValueError(f"{inst.id} was not launched by this supervisor (attached games cannot be restarted)")
    stopped = stop(inst) if not inst.dead else None
    inst.slot.release()
    params = dict(inst.launch_params)
    if new_game is not None:
        params["new_game"] = new_game
    res = launch(**params, slot_index=inst.slot.index)
    res["previous"] = {"exit_code": inst.exit_code, "crashed": inst.crashed, "stopped": stopped}
    return res


def status(inst, n):
    alive = not inst.dead
    items = inst.lines(n=n)
    lines, note = bound([l for _, l, _ in items])
    return {"instance": inst.id, "pid": inst.pid, "alive": alive, "exit_code": inst.exit_code, "port": inst.port,
            "connected": inst.sock is not None, "crashed": inst.crashed,
            "crash_reason": inst.crash["reason"] if inst.crash else None,
            "hung": inst.hung, "frame_stall_s": inst.stall_s if alive else None, "loading": inst.loading,
            "hang_threshold_s": inst.load_timeout if inst.loading else inst.hang_timeout,
            "profile": inst.profile, "slot": inst.slot.name if inst.slot else None,
            "appdata": str(inst.appdata) if inst.appdata else None, "uptime_s": round(time.time() - inst.started),
            "engine_info": info_or_error(inst) if inst.sock and not inst.hung else None,
            "log_rate_lines_per_s": inst.rate(), "last_seq": inst.last_seq, "buffered_lines": len(inst.ring),
            "warnings": [w for w in [wer_warning(inst.exe_name)] if w] or None, "events": events_status(inst),
            "last_lines": lines, "omitted": note}


def last_crash(inst, n):
    if inst.crash is None:
        return {"instance": inst.id, "crashed": False, "alive": not inst.dead, "exit_code": inst.exit_code,
                "exit_code_name": exit_code_name(inst.exit_code)}
    c = dict(inst.crash)
    stack = c.pop("stack")
    c["stack"], stack_note = bound(stack[:25], keep="head", max_chars=4000)
    if len(stack) > 25:
        stack_note = f"{len(stack) - 25} more frames in the log"
    c["log_tail"], _ = bound(c["log_tail"][-n:], max_chars=8000)
    c["last_lines"], _ = bound(c["last_lines"][-n:], max_chars=8000)
    if c["engine_reason"] and c["engine_reason"].splitlines()[0] == c["reason"]:
        c["engine_reason"] = None  # same text
    with inst.crash_lock:
        deleted, inst.dumps_deleted = inst.dumps_deleted, []
    return {"instance": inst.id, "crashed": True, **c, "stack_omitted": stack_note, "script_error": last_fatal_script_error(inst),
            "dumps_deleted_by_retention": deleted or None}


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
    "console_exec / log_tail / game_status, and game_stop when done. After a crash use game_last_crash and "
    "game_restart; on a hang (game_status hung) use game_dump, then game_stop. Each tool takes an optional instance id "
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
                      new_game: bool = False, extra_args: str | None = None, timeout: float = 180,
                      full_dumps: bool = False, hang_timeout: float | None = None,
                      load_timeout: float | None = None) -> dict:
    """Launch a game instance (silent/minimized unless show_window) and connect to it.

    exe: default <repo>/_build/_game/bin_dbg/AnomalyDX11AVX.exe. game_root: default C:\\Anomaly (C:\\GAMMA counts
    against the GAMMA cap). new_game: start a new game straight into a level. extra_args: more engine args.
    timeout: seconds to wait for the first engine.info reply after connecting (a new_game level load blocks it).
    full_dumps: Windows Error Reporting writes full dumps (GBs) instead of mid dumps; the setting is global per exe
    name, so the last launch wins for all running instances. hang_timeout / load_timeout: seconds of stalled frames
    before the instance is marked hung (defaults 30 / 600 while a level loads).
    Refused when the machine-wide instance cap is reached. If the engine never opens its port, the game is dumped
    and killed, and the error names the dump."""
    return await run(launch, exe, game_root, show_window, new_game, extra_args, timeout, full_dumps, hang_timeout,
                     load_timeout)


@tool
async def game_restart(instance: str | None = None, new_game: bool | None = None) -> dict:
    """Stop the instance if it still runs (a hung one is killed), then relaunch it with the same exe, game root and
    args into the same slot and appdata. new_game overrides the original launch's choice when given."""
    return await run(restart, get(instance), new_game)


@tool
async def game_last_crash(instance: str | None = None, lines: int = 30) -> dict:
    """The instance's crash record: plain-words reason (FATAL description/expression/file:line, or exception code +
    module), exit code name, engine stack, dump paths (engine .mdmp, WER dump by pid, in .mcp-run/<id>/dumps),
    the .log path and tail, and the last buffered lines. crashed=false if it has not crashed."""
    return await run(last_crash, get(instance), max(1, min(lines, 200)))


@tool
async def game_dump(instance: str | None = None) -> dict:
    """Write a minidump of the running game from the supervisor (all thread stacks; use it on a hung instance).
    Saved in .mcp-run/<id>/dumps; old dumps beyond the retention limits are deleted and listed."""
    inst = get(instance)
    path = await run(inst.write_dump)
    with inst.crash_lock:
        deleted, inst.dumps_deleted = inst.dumps_deleted, []
    return {"instance": inst.id, "dump": str(path), "size_mb": round(path.stat().st_size / 2 ** 20, 1),
            "dumps_deleted_by_retention": deleted or None}


@tool
async def game_attach(port: int | None = None, appdata: str | None = None) -> dict:
    """Attach to a running game started with -mcp_port, by TCP port or by its -appdata dir (reads mcp_port there).
    Attached games take no slot and are not killed when the supervisor exits."""
    return await run(attach, port, appdata)


@tool
async def game_status(instance: str | None = None, lines: int = 20) -> dict:
    """pid, alive, port, crashed (see game_last_crash), hung (frames stalled past the threshold; see game_dump),
    engine.info, log rate (lines/s) and the last log lines of an instance."""
    return await run(status, get(instance), max(0, min(lines, 200)))


@tool
async def game_list() -> dict:
    """Instances known to this supervisor, plus the machine-wide caps."""
    return {"current": current[0], "caps": load_caps(), "instances": [
        {"instance": i.id, "pid": i.pid, "alive": not i.dead, "exit_code": i.exit_code, "port": i.port,
         "connected": i.sock is not None, "crashed": i.crashed, "hung": i.hung, "profile": i.profile, "slot": i.slot.name if i.slot else None,
         "last_seq": i.last_seq} for i in instances.values()]}


@tool
async def game_stop(instance: str | None = None) -> dict:
    """Quit the game gracefully (console 'quit'), kill it after 30 s, and release its slot. The log buffer stays."""
    return await run(stop, get(instance))


@tool
async def console_exec(cmd: str, instance: str | None = None, timeout: float = 30) -> dict:
    """Run an engine console command; returns the log lines it printed (repeats collapsed, size capped)."""
    inst = get(instance)
    return with_error_hint(inst, exec_result(await run(inst.call, "console.exec", {"cmd": cmd}, timeout)))


@tool
async def lua_exec(code: str, instance: str | None = None, timeout: float = 30) -> dict:
    """Run Lua in the game's script engine (main thread). An expression list returns its values as strings
    (e.g. 'level.name()'); statements run as a chunk. print() output and other log lines come back in lines.
    Errors are returned as tool errors with the traceback."""
    inst = get(instance)
    return with_error_hint(inst, exec_result(await run(inst.call, "lua.exec", {"code": code}, timeout)))


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


# ---------------------------------------------------------------- game events
# The engine sends {"event":"game","data":{"seq","type","time" (unix ms),"frame","data"}} and keeps the last 1000 for
# events.tail {"since":S}. Script errors are coalesced engine-side: repeats of a kind+message within 5 s are counted
# and reported once per 5 s (data.count = total, data.since_last = occurrences this event stands for).

EVENT_RING = 5000
EVENT_TEXT_CHARS = 4000  # per string field in game_events results (tracebacks)


class EventBuffer:
    def __init__(self):
        self.items = deque(maxlen=EVENT_RING)  # event dicts sorted by seq
        self.last_seq = 0
        self.lost = 0  # events the engine ring no longer held when we filled a gap
        self.lock = threading.Lock()
        self.filling = False
        self.seen = {}  # consumer -> last seq it reported

    def merge(self, evs, first_seq=None, since=None):
        with self.lock:
            if first_seq is not None and since is not None and first_seq > since + 1:
                self.lost += first_seq - since - 1 - sum(1 for e in self.items if since < e["seq"] < first_seq)
            m = {e["seq"]: e for e in self.items}
            for e in evs:
                m.setdefault(e.get("seq", 0), e)
            self.items = deque((m[s] for s in sorted(m)), maxlen=EVENT_RING)
            if self.items:
                self.last_seq = max(self.last_seq, self.items[-1]["seq"])

    def fill(self, inst, since):
        try:
            res = inst.call("events.tail", {"since": since}, timeout=15)
            self.merge(res.get("events", []), res.get("first_seq"), since)
        except Exception as e:
            log.warning("[%s] events.tail failed: %s", inst.id, e)
        finally:
            self.filling = False

    def on_live(self, inst, e):
        seq = e.get("seq", 0)
        gap = None
        with self.lock:
            if seq <= self.last_seq:
                return  # already fetched by events.tail
            if seq > self.last_seq + 1 and not self.filling:
                self.filling, gap = True, self.last_seq
            self.items.append(e)
            self.last_seq = seq
        if e.get("type") == "script_error":
            d = e.get("data") or {}
            log.info("[%s] script error (%s): %s", inst.id, d.get("kind"), (d.get("message") or "")[:200])
        if gap is not None:
            threading.Thread(target=self.fill, args=(inst, gap), daemon=True).start()

    def select(self, since=None, types=None):
        with self.lock:
            items = list(self.items)
        if since is not None:
            items = [e for e in items if e["seq"] > since]
        if types:
            items = [e for e in items if any(fnmatch.fnmatchcase(e.get("type", ""), t) for t in types)]
        return items

    def new_counts(self, consumer):
        """Events / script errors since this consumer last asked; marks them seen."""
        with self.lock:
            since = self.seen.get(consumer, 0)
            new = [e for e in self.items if e["seq"] > since]
            self.seen[consumer] = self.last_seq
        errs = [e for e in new if e.get("type") == "script_error"]
        return {"new_events": len(new), "new_script_errors": sum((e.get("data") or {}).get("since_last", 1) for e in errs),
                "new_fatal_script_errors": sum(1 for e in errs if (e.get("data") or {}).get("fatal")),
                "new_types": sorted({e.get("type") for e in new}) or None}


def event_out(e):
    d = dict(e.get("data") or {})
    for k, v in d.items():
        if isinstance(v, str) and len(v) > EVENT_TEXT_CHARS:
            d[k] = v[:EVENT_TEXT_CHARS] + f"... [{len(v) - EVENT_TEXT_CHARS} chars cut]"
    return {"seq": e.get("seq"), "time": round(e.get("time", 0) / 1000, 3), "type": e.get("type"), "frame": e.get("frame"),
            "data": d}


def events_status(inst):
    ev = inst.events
    return {"last_seq": ev.last_seq, "buffered": len(ev.items), "lost": ev.lost or None, **ev.new_counts("status")}


def with_error_hint(inst, out):
    """Adds a short note when script errors arrived since the last exec-like call."""
    c = inst.events.new_counts("exec")
    if c["new_script_errors"]:
        out["script_errors"] = (f"{c['new_script_errors']} new script error(s)"
                                + (f", {c['new_fatal_script_errors']} fatal" if c["new_fatal_script_errors"] else "")
                                + "; see game_events(types=['script_error'])")
    return out


def last_fatal_script_error(inst):
    errs = [e for e in inst.events.select(types=["script_error"]) if (e.get("data") or {}).get("fatal")]
    return event_out(errs[-1]) if errs else None


@tool
async def game_events(instance: str | None = None, since: int | None = None, types: list[str] | None = None,
                      limit: int = 100) -> dict:
    """Structured game events from the supervisor buffer (survives reconnects and game exit), oldest first.
    Types: script_error (data: kind, fatal, message, file, line, script, traceback, count, since_last), level_loaded
    (level, previous), level_unloaded, actor_spawned, actor_died (killer_id/name/section), game_saved, game_loaded,
    menu_opened, menu_closed, task_given, task_completed, task_failed, task_in_progress.
    since: only events with seq > since (page with the returned last_seq); without it the newest `limit` are returned.
    types: type names or fnmatch patterns (e.g. ['task_*']). limit: max 500. Script errors repeating within 5 s are
    coalesced: count = total so far, since_last = occurrences this event stands for. Fatal errors (kind callback/
    luabind/cast/panic) end the game; see game_last_crash."""
    inst = get(instance)
    limit = max(1, min(limit, 500))
    items = inst.events.select(since, types)
    matched = len(items)
    items = items[:limit] if since is not None else items[-limit:]
    return {"instance": inst.id, "alive": not inst.dead, "matched": matched, "events": [event_out(e) for e in items],
            "last_seq": items[-1]["seq"] if items else (since if since is not None else inst.events.last_seq),
            "newest_seq": inst.events.last_seq, "more": matched > len(items), "lost": inst.events.lost or None}


# ---------------------------------------------------------------- game_teleport
# New game = the stock faction-select path: ui_mm_faction_select reads axr_main.config [character_creation] on the
# fake_start load and ChangeLevel()s to new_game_map (a new_game_start_locations.ltx section) on the first update.
# Everything else is lua.exec: ChangeLevel to a start location / smart terrain graph vertex, set_actor_position.

TP_PRELUDE = r'''
local FAC, LVL, POS, LOC = @FAC@, @LVL@, @POS@, @LOC@
local ini_map = ini_file("plugins\\new_game_start_locations.ltx")
local ini_itm = ini_file("items\\settings\\new_game_loadouts.ltx")
local maps = ini_file("game_maps_single.ltx")
local levels, facs, locs = {}, {}, {}
for i = 0, maps:line_count("level_maps_single") - 1 do
  local _, id = maps:r_line("level_maps_single", i, "", "")
  if id ~= "fake_start" then levels[#levels + 1] = id end
end
ini_map:section_for_each(function(sec)
  local f = sec:match("^(.+)_start_locations$")
  if not f then return end
  if ini_itm:section_exist(f .. "_loadout") then facs[#facs + 1] = f end
  for i = 0, ini_map:line_count(sec) - 1 do
    local _, id, v = ini_map:r_line(sec, i, "", "")
    local l = v:match("^([^,]+)")
    if l and ini_map:section_exist(id) then locs[#locs + 1] = {f, id, l, v:find(",true") ~= nil} end
  end
end)
table.sort(levels); table.sort(facs)
local function has(t, x) for _, v in ipairs(t) do if v == x then return true end end end
local function fmt(p) return string.format("%.2f,%.2f,%.2f", p.x, p.y, p.z) end
local function pick_loc(lvl, fac) -- the faction's own start (story start first), else any faction's start on lvl
  local own, any
  for _, e in ipairs(locs) do
    if e[3] == lvl or (lvl == nil and e[1] == fac) then
      if e[1] == fac then
        if not own or (e[4] and not own[4]) then own = e end
      elseif not any then any = e end
    end
  end
  return own or any
end
local function community() return (character_community(db.actor):gsub("^actor_", "")) end
'''

TP_INFO = r'''
local e
if FAC and not has(facs, FAC) then e = "unknown faction '" .. FAC .. "'; valid: " .. table.concat(facs, ", ") end
if LVL and not has(levels, LVL) then e = "unknown level '" .. LVL .. "'; valid: " .. table.concat(levels, ", ") end
local ingame = level.present() and db.actor ~= nil
local loc = pick_loc(LVL, FAC or "stalker")
return e or "", ingame and "game" or "menu", ingame and level.name() or "", ingame and community() or "",
  loc and loc[2] or "", loc and loc[3] or ""
'''

TP_NEWGAME = r'''
local kit = {}
for i = 0, ini_itm:line_count(FAC .. "_loadout") - 1 do -- the menu's preselected kit (PDA etc. come from [shared])
  local _, sec, v = ini_itm:r_line(FAC .. "_loadout", i, "", "")
  local t = str_explode(v, ",")
  if t[1] == "false" and ini_sys:section_exist(sec) and (not t[4] or tonumber(t[4]) >= 1) then
    for _ = 1, tonumber(t[2]) or 1 do kit[#kit + 1] = sec end
  end
end
local money = ini_itm:r_float_ex(FAC .. "_money", "money")
local c, cc = axr_main.config, "character_creation"
c:w_value(cc, "new_game_difficulty", 1)
c:w_value(cc, "new_game_economy", 1)
c:w_value(cc, "new_game_economy_treasure", 1)
c:w_value(cc, "new_game_story_mode", FAC ~= "zombied" and true or nil)
c:w_value(cc, "new_game_character_name", "MCP_Agent")
c:w_value(cc, "new_game_faction", FAC)
c:w_value(cc, "new_game_loadout", table.concat(kit, ","))
c:w_value(cc, "new_game_map", LOC)
c:w_value(cc, "new_game_money", money)
c:save()
exec_console_cmd("keypress_on_start off")
device():pause(false)
exec_console_cmd("start server(all/single/alife/new) client(localhost)")
exec_console_cmd("main_menu off")
return #kit, money or 0
'''

TP_GOTO = r'''
exec_console_cmd("main_menu off")
local gg, sim = game_graph(), alife()
local function lvl_of(g) return sim:level_name(gg:vertex(g):level_id()) end
local p, lv, gv, how
if POS then -- nearest graph vertex; the exact spot is set after the load
  local t, bd = vector():set(POS[1], POS[2], POS[3])
  local g = 0
  while gg:valid_vertex_id(g) do
    local v = gg:vertex(g)
    if sim:level_name(v:level_id()) == LVL then
      local d = v:level_point():distance_to_sqr(t)
      if not bd or d < bd then bd, gv = d, g end
    end
    g = g + 1
  end
  how = "graph vertex nearest to the position"
end
local loc = not gv and pick_loc(LVL, community())
if loc and lvl_of(ini_map:r_float_ex(loc[2], "gvid")) == LVL then
  p = vector():set(ini_map:r_float_ex(loc[2], "x"), ini_map:r_float_ex(loc[2], "y"), ini_map:r_float_ex(loc[2], "z"))
  lv, gv, how = ini_map:r_float_ex(loc[2], "lvid"), ini_map:r_float_ex(loc[2], "gvid"), "start location " .. loc[2]
end
if not gv then -- a smart terrain's graph vertex (a valid AI node); bases first, then by name
  local best
  for _, v in pairs(SIMBOARD.smarts) do
    local s = v.smrt
    if lvl_of(s.m_game_vertex_id) == LVL then
      local b = (s.props and s.props.base or 0) > 0
      if not best or (b and not best[2]) or (b == best[2] and s:name() < best[1]:name()) then best = {s, b} end
    end
  end
  if best then gv, how = best[1].m_game_vertex_id, "smart terrain " .. best[1]:name() end
end
if not gv then return "error", "level " .. LVL .. " has no graph vertices in this game" end
if not p then p, lv = gg:vertex(gv):level_point(), gg:vertex(gv):level_vertex_id() end
ChangeLevel(p, lv, gv, VEC_ZERO)
return "ok", how, fmt(p)
'''

TP_PLACE = r'''
exec_console_cmd("main_menu off")
local p = vector():set(POS[1], POS[2], POS[3])
local bad, r = 4294967295, 0
if level.vertex_id(p) >= bad then -- off the AI map: snap to the nearest node within 30 m
  local found
  for d = 1, 30 do
    local n = math.max(8, math.floor(2 * math.pi * d / 1.5))
    for k = 0, n - 1 do
      local a = 2 * math.pi * k / n
      local v = level.vertex_id(vector():set(p.x + d * math.cos(a), p.y, p.z + d * math.sin(a)))
      if v < bad then found = v break end
    end
    if found then r = d break end
  end
  if not found then return "error", "position " .. fmt(p) .. " is not near the AI map of " .. level.name() end
  p = level.vertex_position(found)
end
db.actor:set_actor_position(p)
return "ok", fmt(p), r
'''

TP_PROBE = r'''
local a = db.actor
if not (level.present() and a) then return "" end
return level.name(), tostring(a.afterFirstUpdate == true), tostring(a:alive())
'''

TP_FINAL = r'''
local a, gave = db.actor, ""
if FAC then a:set_character_community("actor_" .. FAC, 0, 0) end
local pda = a:object("device_pda_1") or a:object("device_pda_2") or a:object("device_pda_3")
if not pda then alife_create_item("device_pda_1", a) gave = "device_pda_1" end
-- the kit leaves it in the backpack; the 3D PDA key only draws the PDA slot (8)
if pda and not a:item_in_slot(8) then a:move_to_slot(pda, 8) end
return level.name(), fmt(a:position()), community(), pda and pda:section() or gave, gave, a:money()
'''

TP_NAME_RX = re.compile(r"^[A-Za-z0-9_]+$")


def tp_lua(code, fac=None, lvl=None, pos=None, loc=None):
    def s(v):
        return f'"{v}"' if v else "nil"
    p = "{%s}" % ",".join(repr(float(c)) for c in pos) if pos else "nil"
    pre = TP_PRELUDE.replace("@FAC@", s(fac)).replace("@LVL@", s(lvl)).replace("@POS@", p).replace("@LOC@", s(loc))
    return pre + code


def tp_exec(inst, code, timeout=30, **kw):
    return inst.call("lua.exec", {"code": tp_lua(code, **kw)}, timeout=timeout)["results"]


def tp_wait(inst, want_level, deadline):
    """Until the actor is alive on want_level, its first update ran and no load is in progress."""
    last = None
    while True:
        if inst.dead:
            reason = inst.crash["reason"] if inst.crash else None
            raise RuntimeError(f"game exited with code {inst.exit_code} while waiting for {want_level}" +
                               (f": {reason}" if reason else "") + " (see game_last_crash)")
        if time.time() > deadline:
            raise TimeoutError(f"actor not in control on {want_level} in time; last probe: {last}")
        try:  # heartbeat is answered while the main thread loads; lua.exec only after it
            if not inst.call("engine.heartbeat", timeout=5).get("loading"):
                last = tp_exec(inst, TP_PROBE, timeout=10)
                if last[:3] == [want_level, "true", "true"]:
                    return
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(1)


def teleport(inst, faction, level, position, cvars, timeout):
    t0 = time.time()
    deadline = t0 + timeout
    for name in (faction, level):
        if name is not None and not TP_NAME_RX.match(name):
            raise ValueError(f"bad name {name!r}")
    if position is not None and len(position) != 3:
        raise ValueError("position must be [x, y, z]")
    err, mode, cur_level, cur_fac, loc, loc_level = tp_exec(inst, TP_INFO, fac=faction, lvl=level)
    if err:
        raise ValueError(err)
    notes, load_s, fac_set = [], 0.0, None
    if mode == "menu":
        faction = faction or "stalker"
        level = level or loc_level
        if not level:
            raise ValueError(f"faction {faction} has no start location; give a level")
        kit, money = tp_exec(inst, TP_NEWGAME, fac=faction, loc=loc or None)
        notes.append(f"new game as {faction}: {kit} kit items, {money} RU, story mode " +
                     ("off" if faction == "zombied" else "on"))
        t = time.time()
        if loc:  # the faction-select script jumps from fake_start to the start location by itself
            notes.append(f"start location {loc}")
            tp_wait(inst, level, deadline)
        else:
            tp_wait(inst, "fake_start", deadline)
        load_s += time.time() - t
        cur_level = level if loc else "fake_start"
    else:
        if faction and faction != cur_fac:
            fac_set = faction
            notes.append(f"in-game faction change: community {cur_fac} -> {faction} in place (inventory, story and "
                         "relations untouched); for a clean start as a faction use game_restart, then game_teleport")
    if level and level != cur_level:
        how = tp_exec(inst, TP_GOTO, lvl=level, pos=position)
        if how[0] == "error":
            raise RuntimeError(how[1])
        notes.append(f"level change to {level} via {how[1]} ({how[2]})")
        t = time.time()
        tp_wait(inst, level, deadline)
        load_s += time.time() - t
    if position is not None:
        placed = tp_exec(inst, TP_PLACE, pos=position)
        if placed[0] == "error":
            raise RuntimeError(placed[1])
        if placed[2] != "0":
            notes.append(f"position off the AI map, snapped {placed[2]} m to {placed[1]}")
    lvl, pos, fac, pda, gave, money = tp_exec(inst, TP_FINAL, fac=fac_set)
    if gave:
        notes.append(f"actor had no PDA; gave {gave}")
    applied = []
    for cmd in cvars or []:
        res = inst.call("console.exec", {"cmd": cmd}, timeout=30)
        lines, _ = bound(res.get("lines", []), keep="head", max_chars=2000)
        applied.append({"cmd": cmd, "lines": lines})
    return {"instance": inst.id, "level": lvl, "position": [float(c) for c in pos.split(",")], "faction": fac,
            "pda": pda or None, "money": int(float(money)), "load_s": round(load_s, 1),
            "total_s": round(time.time() - t0, 1), "cvars": applied or None, "notes": notes or None}


@tool
async def game_teleport(instance: str | None = None, faction: str | None = None, level: str | None = None,
                        position: list[float] | None = None, cvars: list[str] | None = None,
                        timeout: float = 600) -> dict:
    """Get a playable session on a map: returns once the actor is in control there (or errors/times out).

    Main menu: starts a new game as faction (default stalker) with its stock kit (PDA included) on level (default:
    the faction's start). The stock start location on that level is used, else a smart terrain's graph vertex;
    two loads (fake_start, then the level). In game: other level -> level change; position [x, y, z] -> moves the
    actor there (snapped to the nearest AI node if off the map). faction in game only switches the actor's
    community in place. cvars: console commands applied after arrival (e.g. ["g_god on"]).
    Unknown level/faction -> error listing the valid ones. Result: level, position, faction, load_s, notes."""
    inst = get(instance)
    return with_error_hint(inst, await run(teleport, inst, faction, level, position, cvars, timeout))


# ---------------------------------------------------------------- game_state

STATE_SECTIONS = ("inventory", "tasks", "stats")


def game_state_get(inst, sections):
    """state.get on the main thread; while a level loads (main thread blocked) or the game is hung, the answer comes
    from engine.heartbeat (net thread) / the hang monitor instead, so the call never waits for the load."""
    if inst.dead:
        raise RuntimeError(f"instance {inst.id} is not running (exit code {inst.exit_code})")
    t0 = time.perf_counter()

    def quick(mode, **kw):
        return {"instance": inst.id, "mode": mode, "source": "supervisor", **kw}

    if inst.hung:
        return quick("hung", frame_stall_s=inst.stall_s, loading=inst.loading)
    hb = inst.call("engine.heartbeat", timeout=5)
    if hb.get("loading"):
        return quick("loading", frame=hb.get("frame"))
    try:
        res = inst.call("state.get", {"sections": ",".join(sections)} if sections else {}, timeout=3)
    except TimeoutError:
        hb = inst.call("engine.heartbeat", timeout=5)
        if hb.get("loading"):
            return quick("loading", frame=hb.get("frame"))
        raise TimeoutError("state.get: no reply in 3 s and no level is loading (main thread stalled; see game_status)")
    res["call_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return {"instance": inst.id, **res}


@tool
async def game_state(instance: str | None = None, sections: list[str] | None = None) -> dict:
    """Where am I and what is the game doing: one cheap structured snapshot (no screenshot / lua_exec needed).

    mode: main_menu (no game), loading, in_game, in_game_menu (ESC menu over a game), dialog (a UI window owns input,
    see ui.dialog), dead, paused, tutorial, console (console open; under_console = the mode beneath), hung.
    ui: main_menu, console, paused, cursor, input_receiver (engine class on top of the input stack), actor_control
    (the actor takes movement/fire input now), dialog / menu_dialog {kind: actor_menu (menu_mode inventory / trade /
    upgrade / loot_body / loot_box) | pda | talk | message_box | script | other, class, script_class (Lua class of
    script windows, e.g. sleep), partner_id/name/section}, tutorial, input_disabled.
    In a level: actor {id, name, position, yaw/pitch (deg; yaw 0 = +z, +90 = +x, i.e. + = turned right like
    game_input look; pitch + = up), level_vertex, game_vertex, alive, health, stamina, radiation, bleeding,
    psy_health, satiety, thirst/sleepiness (0..1, Anomaly scripts), move (stand/run/walk/sprint/crouch/crouch_low/
    jump/landing/climb), speed (m/s), weight/max_weight/max_walk_weight, money, community, rank, reputation, god_mode,
    outfit, helmet}; hands {active_slot, item {section, name, condition, state (idle/showing/hiding/hidden/fire/
    reload/misfire/...), ammo, mag_size, ammo_type, spare_ammo, fire_mode (-1 = auto), zoomed, jammed}, torch,
    night_vision, detector {working}}; target (crosshair) {distance, id, section, object, name, alive, relation};
    world {level, game_time, game_time_factor, time_factor, weather, surge, psi_storm, in_cover}.
    sections (optional, cheap but bigger): "inventory" (slots, belt, backpack grouped by section; capped at 100),
    "tasks" (active tasks: id, title, type, active), "stats" (frame, fps, frame_ms, times); ["all"] = all of them.
    Fields that do not apply are absent. While a level loads the answer is {mode: loading, source: supervisor}."""
    inst = get(instance)
    sections = sections or []
    bad = [s for s in sections if s not in STATE_SECTIONS + ("all",)]
    if bad:
        raise ValueError(f"unknown sections {bad}; valid: {list(STATE_SECTIONS)} or ['all']")
    return await run(game_state_get, inst, sections)


# ---------------------------------------------------------------- game_objects

WORLD_KINDS = ("stalker", "monster", "corpse", "item", "weapon", "ammo", "artefact", "anomaly", "campfire",
               "container", "door", "physic", "vehicle", "ladder", "level_changer", "smart_terrain", "restrictor",
               "cover", "squad", "other", "npc", "box", "zone", "loot", "creature", "all")


def world_call(inst, method, params):
    """world.* on the main thread; answers {available: false, reason: loading} without waiting while a level loads."""
    if inst.dead:
        raise RuntimeError(f"instance {inst.id} is not running (exit code {inst.exit_code})")
    if inst.hung:
        return {"instance": inst.id, "available": False, "reason": "hung"}
    t0 = time.perf_counter()
    if inst.call("engine.heartbeat", timeout=5).get("loading"):
        return {"instance": inst.id, "available": False, "reason": "loading", "source": "supervisor"}
    try:
        res = inst.call(method, params, timeout=3)
    except TimeoutError:
        if inst.call("engine.heartbeat", timeout=5).get("loading"):
            return {"instance": inst.id, "available": False, "reason": "loading", "source": "supervisor"}
        raise TimeoutError(f"{method}: no reply in 3 s and no level is loading (main thread stalled; see game_status)")
    res["call_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return {"instance": inst.id, **res}


@tool
async def game_objects(instance: str | None = None, radius: float = 100, kinds: list[str] | None = None,
                       pattern: str | None = None, alive: bool | None = None, hostile_only: bool = False,
                       visible_only: bool = False, los: bool = False, limit: int = 30, summary: bool = False,
                       offline: bool = False, position: list[float] | None = None) -> dict:
    """Find things around the actor (or `position` [x,y,z]) without screenshots: nearest first, compact, read-only.

    radius: metres (3D distance from the origin). kinds: stalker (living NPC, incl.
    traders), monster, corpse (dead stalker/monster, `of`), item, weapon, ammo, artefact, anomaly, campfire,
    container, door, physic, vehicle, ladder, level_changer, smart_terrain, restrictor, cover (smart cover), squad
    (offline only), other (default: all but restrictor and cover); aliases
    npc=stalker, box=container, zone=anomaly, loot=item+weapon+ammo+artefact, creature=stalker+monster+corpse, all.
    pattern: case-insensitive glob on section / object name / NPC name. alive: true = living creatures only, false =
    corpses only. hostile_only: living creatures whose relation to the actor is enemy. visible_only: inside the
    current camera view (frustum, not occlusion). los: per object an unobstructed ray from the camera to its centre
    (level geometry and other objects block; at most 64 rays per call, later objects get no los field).
    limit: max objects (default 30, cap 100); summary=true returns only counts per kind (cheap first look).
    offline: also alife server entities on this level that have no client object (beyond the online radius, squads):
    listed separately in `offline` (id, kind, section, name, position, distance, bearing, alive, squad, smart;
    offline: true); hostile_only / visible_only disable it.
    Per object: id, kind, section, name (unique object name), display (NPC character name / item name), position,
    distance, bearing (deg to turn right to face its centre: game_input {"look": {"yaw": bearing, "pitch":
    elevation}}, or {"look_at": position}), elevation (deg up). Creatures: alive, health, community, rank, relation
    (friend/neutral/enemy/worst_enemy), wounded, in_combat, enemy_id, sees_actor, squad_id/squad/smart; corpses:
    items, has_loot. Items: condition, cost, ammo/mag_size/ammo_type (weapons), rounds (ammo). Anomalies: type (engine
    class), radius, enabled (campfires: lit). Containers: empty, items, closed. Level changers: destination. Also counts (per kind,
    all matches), total, omitted, target (crosshair: distance, point, id/kind/section or material for level geometry),
    cost_ms (engine time). Outside a level: {available: false, reason: no_level/loading/no_actor}.
    Detail for one object (inventory, money, active item, zone power): game_object(id)."""
    inst = get(instance)
    bad = [k for k in (kinds or []) if k not in WORLD_KINDS]
    if bad:
        raise ValueError(f"unknown kinds {bad}; valid: {list(WORLD_KINDS)}")
    p = {"radius": str(float(radius)), "limit": int(limit)}
    if kinds:
        p["kinds"] = ",".join(kinds)
    if pattern:
        p["pattern"] = pattern
    if alive is not None:
        p["alive"] = int(alive)
    for k, v in (("hostile_only", hostile_only), ("visible_only", visible_only), ("los", los), ("summary", summary),
                 ("offline", offline)):
        if v:
            p[k] = 1
    if position is not None:
        if len(position) != 3:
            raise ValueError("position must be [x, y, z]")
        p["position"] = ",".join(str(float(c)) for c in position)
    return await run(world_call, inst, "world.objects", p)


@tool
async def game_object(id: int, instance: str | None = None) -> dict:
    """Full detail for one object by id (from game_objects / game_state.target): the game_objects fields plus los
    (always), inventory (grouped by section: count, rounds; capped at 50) / items / money / active_item for NPCs and
    corpses, inventory of containers, effective_radius / max_power for anomalies, parent_id when the object is inside
    an inventory. Not online -> the alife server entity (offline: true). Unknown id -> object null."""
    inst = get(instance)
    return await run(world_call, inst, "world.object", {"id": int(id)})


# ---------------------------------------------------------------- game_input

def input_run(inst, steps, wait, timeout):
    """input.run, then poll input.status until done; on timeout everything is released so no key stays stuck."""
    if not steps:
        return {"instance": inst.id, **inst.call("input.status", timeout=10)}
    rid = inst.call("input.run", {"steps": steps}, timeout=10)["id"]
    if not wait:
        return {"instance": inst.id, "id": rid, "done": False}
    deadline = time.monotonic() + timeout
    while True:
        try:
            res = inst.call("input.status", {"id": rid}, timeout=max(1.0, deadline - time.monotonic()))
            if res.get("done"):
                return {"instance": inst.id, **res}
        except TimeoutError:
            pass  # main thread blocked (level load): keep waiting until the deadline
        if time.monotonic() > deadline:
            rel = inst.call("input.release_all", timeout=10)
            raise TimeoutError(f"input sequence {rid} not done after {timeout} s; cancelled and released "
                               f"{rel.get('released')}")
        time.sleep(0.02)


@tool
async def game_input(steps: list, instance: str | None = None, wait: bool = True, timeout: float = 120) -> dict:
    """Play keyboard/mouse input like a human, timed per frame by the engine (works silent/minimized, no focus).

    Input goes where real input goes (level, main menu, dialogs, inventory, PDA, console). steps is a timed script:
    a list runs in SEQUENCE; a list nested in it runs in PARALLEL (a list inside that is a sequence again, ...).
    Steps (ms = duration; one verb per step):
      {"action": "forward", "ms": 2000}  hold a game action (bind name) for 2 s; without ms = tap (1 frame);
                                        "state": "down" / "up" = press / release only (stays held until released).
      {"key": "ESCAPE"}                 same for a raw key (W, ESCAPE/ESC, RETURN/ENTER, UP, F1, SPACE, LSHIFT,
                                        MOUSE1..8 / LMB / RMB ...); with ms / state like actions.
      {"text": "hello"}                 type text (key taps with shift, current keyboard layout) into an edit box.
      {"look": {"yaw": 90, "pitch": -10}, "ms": 300}  turn the actor camera by degrees (+ = right / up), smoothed.
      {"look_at": [x, y, z], "ms": 300} / {"look_dir": [dx, dy, dz]}  aim the camera at a world point / direction.
      {"mouse": {"dx": 40, "dy": 0}, "ms": 200}  raw mouse move in pixels (UI cursor; in game scaled by sensitivity).
      {"wheel": 1}                      mouse wheel notches (+ = up).   {"wait": 500}  pause 500 ms.
    Example "walk 2 s while turning 90 deg, then use":
      [[{"action": "forward", "ms": 2000}, {"look": {"yaw": 90}, "ms": 2000}], {"action": "use"}]
    Actions: forward back lstrafe rstrafe jump crouch accel sprint_toggle llookout rlookout wpn_fire wpn_zoom
    wpn_reload use inventory active_jobs (PDA) wpn_1..6 quick_use_1..4 torch night_vision show_detector drop quit
    (ESC/menu) ... aliases: fire aim zoom reload pda sprint walk menu strafe_left/right lean_left/right. Unknown
    names fail with the full list; actions resolve to their bound key.
    wait=True returns when done: {id, done, elapsed_ms, frames, held (keys still down), warning?}; wait=False returns
    {id} at once. steps=[] returns the status: running sequences and held keys. Keys are released on
    game_input_release, when the supervisor disconnects, and on timeout (seconds)."""
    inst = get(instance)
    return await run(input_run, inst, steps, wait, timeout)


@tool
async def game_input_release(instance: str | None = None) -> dict:
    """Release every key/button held by game_input and cancel running input sequences: {released, cancelled}."""
    inst = get(instance)
    return {"instance": inst.id, **await run(lambda: inst.call("input.release_all", timeout=10))}


# ---------------------------------------------------------------- game_screenshot

def screenshot(inst, max_width, fmt, quality, save_path, timeout):
    """screenshot.begin, then poll screenshot.get (captured at the end of the next rendered frame, encoded off-thread)."""
    if inst.call("engine.heartbeat", timeout=5).get("loading"):
        raise RuntimeError("a level is loading: the loading screen cannot be captured (the main thread is blocked); "
                           "retry once game_status shows the level")
    params = {"max_width": max_width, "format": fmt, "quality": quality}
    if save_path:
        p = Path(save_path)
        p = (p if p.is_absolute() else RUN_DIR / inst.id / "screenshots" / p).resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        params["save_path"] = str(p)
    t0 = time.monotonic()
    deadline = t0 + timeout
    rid = inst.call("screenshot.begin", params, timeout=timeout)["id"]
    while True:
        res = inst.call("screenshot.get", {"id": rid}, timeout=max(1.0, deadline - time.monotonic()))
        if not res.get("pending"):
            break
        if time.monotonic() > deadline:
            raise TimeoutError(f"no screenshot after {timeout} s (stage {res.get('stage')}: no frame rendered, "
                               "or the main thread is stalled)")
        time.sleep(0.01)
    data = res.pop("data")
    res["total_ms"] = round((time.monotonic() - t0) * 1000, 1)
    res = {"instance": inst.id, **{k: round(v, 1) if isinstance(v, float) else v for k, v in res.items()}}
    return [TextContent(type="text", text=json.dumps(res)),
            ImageContent(type="image", data=data, mime_type="image/png" if fmt == "png" else "image/jpeg")]


@tool
async def game_screenshot(instance: str | None = None, max_width: int = 1024, format: str = "jpeg", quality: int = 70,
                          save_path: str | None = None, timeout: float = 10) -> list[TextContent | ImageContent]:
    """Capture what a player would see (3D view + HUD/UI/menus) and return it as an image plus JSON metadata.

    The frame is the presented back buffer of the next rendered frame (works minimized/silent and with -showwindow).
    max_width: downscale the returned image to this width (aspect kept; 0 = full size). format: "jpeg" (default,
    quality 1..100) or "png". Default (1024 px JPEG q70) is ~50-200 KB. save_path: also write the FULL-resolution
    frame to this file (.jpg/.jpeg = JPEG q95, anything else PNG); relative paths go to .mcp-run/<id>/screenshots/.
    Metadata: width/height (returned image), orig_width/orig_height, frame and level when captured (level null in the
    main menu), readback_frame, bytes (returned image), saved_path/saved_bytes, capture/encode/save/total ms.
    Fails cleanly while a level is loading (the loading screen is not capturable) and with HDR10 output on."""
    inst = get(instance)
    if format not in ("jpeg", "png"):
        raise ValueError('format must be "jpeg" or "png"')
    if not 1 <= quality <= 100 or max_width < 0:
        raise ValueError("quality must be 1..100 and max_width >= 0")
    return await run(screenshot, inst, max_width, format, quality, save_path, timeout)


# ---------------------------------------------------------------- game_perf

def perf_window(inst, seconds):
    """perf.start, wait, then poll perf.report until the engine closes the window (first frame after `seconds`)."""
    inst.call("perf.start", {"seconds": seconds}, timeout=15)
    deadline = time.monotonic() + seconds + 30
    time.sleep(seconds)
    while True:
        res = inst.call("perf.report", timeout=15)
        if not res.get("running"):
            return {"instance": inst.id, **res}
        if time.monotonic() > deadline:
            raise RuntimeError(f"perf window still open after {res.get('elapsed_seconds')} s with {res.get('frames')} "
                               "frames (main thread stalled or a level is loading)")
        time.sleep(0.2)


@tool
async def game_perf(instance: str | None = None, seconds: float = 5) -> dict:
    """Measure the game's main thread over a window of `seconds` (0.1..120) and return per-frame averages.

    fps, frame_ms avg/p50/p95/p99/max/min, hitches (frames > max(2*p50, 33.3 ms)). Loading frames are excluded.
    cap: capped/capped_by (r__framelimit, the menu/pause refresh cap, vsync), limiter_ms = busy-wait in the frame
    limiter. main.busy_ms = frame time minus limiter and inactive sleep (the load to compare; busy_fps = 1000/busy).
    main.phases (ms, pct of frame, lua_ms inside): before_frame, frame_move (seqFrame: level/actor/UI/scripts,
    includes scheduler_rt), pre_render (particles, matrices, task submit), limiter, render (seqRender submit),
    present (End/Present: GPU or vsync wait), wait_tasks (waiting for worker tasks: scheduler, GC), inactive_sleep,
    other (message pump, rest). main.lua_ms = Lua entered via luabind calls/resumes plus explicit lua_gc steps on the
    main thread; tasks.* = same for worker tasks (GameThread: deferred scheduler, seqFrameMT; parallel Lua GC).
    Not covered: Lua run by raw lua_pcall (script loading, console run_string without a level, lua_exec itself),
    and GC triggered implicitly by allocation (it counts as time of the Lua call that allocated).
    memory: working set, private bytes, Lua heap (MB) and its growth per frame (lower bound), engine alloc calls."""
    inst = get(instance)
    if not 0.1 <= seconds <= 120:
        raise ValueError("seconds must be 0.1..120")
    return await run(perf_window, inst, seconds)


def main():
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    w = wer_warning(DEFAULT_EXE.name)
    if w:
        log.warning(w)
    server.run("stdio")
    # stdin closed: quit launched games gracefully (the job object kills any that remain when we exit)
    ts = [threading.Thread(target=stop, args=(i,)) for i in instances.values() if i.slot and not i.dead]
    for t in ts:
        t.start()
    for t in ts:
        t.join()


if __name__ == "__main__":
    main()
