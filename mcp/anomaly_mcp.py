"""MCP supervisor for X-Ray Monolith (Stalker Anomaly): launches and drives game instances.

Agent <-> MCP stdio (this process) <-> TCP JSON lines <-> engine (-mcp_port). stdout belongs to MCP; logs go to stderr.
"""
import ctypes
import ctypes.wintypes as wt
import difflib
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
           full_dumps=False, hang_timeout=None, load_timeout=None, slot_index=None, load=None):
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
        if load:  # the save must already be in this slot's appdata (game_saves import_from=...)
            if " " in load or not (appdata / "savedgames" / (check_load_name(load) + SAVE_EXT)).is_file():
                raise FileNotFoundError(f"no save {load!r} (without spaces) in {appdata / 'savedgames'}")
        port_file = appdata / "mcp_port"
        port_file.unlink(missing_ok=True)
        args = [str(exe), "-fsltx", str(game_root / "fsgame.ltx"), "-appdata", str(appdata), "-mcp_port", "0"]
        if show_window:
            args.append("-showwindow")
        if new_game:
            args += ["-start", "server(all/single/alife/new)", "client(localhost)"]
        elif load:
            args += ["-start", f"server({load}/single/alife/load)", "client(localhost)"]
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
                      load_timeout: float | None = None, load: str | None = None) -> dict:
    """Launch a game instance (silent/minimized unless show_window) and connect to it.

    exe: default <repo>/_build/_game/bin_dbg/AnomalyDX11AVX.exe. game_root: default C:\\Anomaly (C:\\GAMMA counts
    against the GAMMA cap). new_game: start a new game straight into a level. extra_args: more engine args.
    timeout: seconds to wait for the first engine.info reply after connecting (a new_game level load blocks it).
    full_dumps: Windows Error Reporting writes full dumps (GBs) instead of mid dumps; the setting is global per exe
    name, so the last launch wins for all running instances. hang_timeout / load_timeout: seconds of stalled frames
    before the instance is marked hung (defaults 30 / 600 while a level loads).
    Refused when the machine-wide instance cap is reached. If the engine never opens its port, the game is dumped
    and killed, and the error names the dump. load: start straight into that save (it must be in the slot's
    savedgames, e.g. imported earlier by game_saves); returns once the actor is in control (result.load, as
    game_load)."""
    t0 = time.time()
    res = await run(launch, exe, game_root, show_window, new_game, extra_args, timeout, full_dumps, hang_timeout,
                    load_timeout, None, load)
    if load:
        res["load"] = await run(load_wait, instances[res["instance"]], load, 0, 0, t0 + (load_timeout or 600), t0)
    return res


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
elseif p.y < level.vertex_position(level.vertex_id(p)).y - 0.5 then -- below the ground: lift onto the node
  p.y = level.vertex_position(level.vertex_id(p)).y
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


# ---------------------------------------------------------------- game_save / game_load / game_saves
# Console commands like the stock UI: `save <name>` (the console path skips the UI-only on_before_save_input vetoes:
# campfire saves, agony/timer conditions), `load <name>` while ALife runs, else `start server(<name>/single/alife/load)`.
# The engine writes <name>.scoc (Lua marshal data) and <name>.scop, then emits game_saved; the .dds thumbnail follows.

SAVE_EXT = ".scop"
SAVE_PARTS = (".scop", ".scoc", ".dds")
SAVE_BAD_CHARS = set('/\\:*?"<>|^()[]%')  # engine valid_saved_game_name
ENTITY_RX = re.compile(r"! Can't create entity '([^']*)'")

SL_STATE = r'''
local a = db.actor
if not (level.present() and a) then return alife() and "noactor" or "menu" end
local p = a:position()
return a:alive() and "ok" or "dead", level.name(), string.format("%.2f,%.2f,%.2f", p.x, p.y, p.z), a:money(),
  string.format("%.3f", a.health)
'''

SL_RESCAN = 'getFS():rescan_path("$game_saves$") getFS():rescan_pathes()\n'  # the FS caches the file list

SL_INFO = SL_RESCAN + r'''
local out = {}
for _, n in ipairs(@NAMES@) do
  local ok, r = pcall(function()
    if not valid_saved_game(n) then return "||invalid" end
    local w = CSavedGameWrapper(n)
    local Y, M, D, h, m = w:game_time():get(0, 0, 0, 0, 0, 0, 0)
    return string.format("%s|%04d-%02d-%02d %02d:%02d|%.3f", w:level_name(), Y, M, D, h, m, w:actor_health())
  end)
  out[#out + 1] = ok and r or "||error"
end
return unpack(out)
'''

SL_LOAD = SL_RESCAN + r'''
local n = @NAME@
-- the death tutorial's stop callback runs `main_menu on`; inside a load that leaves a dead menu window (crash)
if game.has_active_tutorial() then game.stop_tutorial() return "tutorial" end
exec_console_cmd("keypress_on_start off")
if alife() then exec_console_cmd("load " .. n) return "load" end
device():pause(false)
exec_console_cmd("disconnect")
exec_console_cmd("start server(" .. n .. "/single/alife/load) client(localhost)")
exec_console_cmd("main_menu off")
return "start"
'''


def lua_str(s):
    return json.dumps(s, ensure_ascii=False)  # a valid Lua string literal for names without control characters


def sl_exec(inst, code, timeout=30, **kw):
    for k, v in kw.items():
        code = code.replace(f"@{k.upper()}@", v)
    res = inst.call("lua.exec", {"code": code}, timeout=timeout)
    return res["results"], res.get("lines", [])


def saves_dir(inst):
    if not inst.appdata:
        raise ValueError(f"{inst.id} has no known appdata (attach with appdata=...)")
    return inst.appdata / "savedgames"


def save_names(inst):
    d = saves_dir(inst)
    return sorted((p.stem for p in d.glob("*" + SAVE_EXT)), key=lambda n: -(d / (n + SAVE_EXT)).stat().st_mtime) \
        if d.is_dir() else []


def check_load_name(name):
    if not name or not name.strip() or any(c in SAVE_BAD_CHARS for c in name):
        raise ValueError(f"bad save name {name!r} (not allowed: {''.join(sorted(SAVE_BAD_CHARS))})")
    return name.strip()


def clean_save_name(name):
    """New save names: letters, digits, _ - . (others become _), max 64 chars; spaces would not survive -start."""
    n = re.sub(r"[^A-Za-z0-9_.\-]", "_", (name or "").strip()).strip(".")[:64]
    if not n:
        raise ValueError(f"bad save name {name!r}")
    return n


def sl_ready(inst, what):
    if inst.dead:
        raise RuntimeError(f"cannot {what}: instance {inst.id} is not running (exit code {inst.exit_code}); "
                           "use game_restart or game_launch(load=...)")
    if inst.hung:
        raise RuntimeError(f"cannot {what}: instance {inst.id} is hung (see game_status)")
    if inst.call("engine.heartbeat", timeout=5).get("loading"):
        raise RuntimeError(f"cannot {what}: a level is loading; retry when game_state is no longer 'loading'")


def same_save(a, b):
    a, b = (a or "").lower(), (b or "").lower()
    return a.removesuffix(SAVE_EXT) == b.removesuffix(SAVE_EXT)


def sl_event(inst, types, since, pred, deadline, what):
    while True:
        for e in inst.events.select(since, types):
            if pred(e.get("data") or {}):
                return e
        if inst.dead or inst.crash:
            raise RuntimeError(f"game crashed or exited while waiting for {what}")
        if time.time() > deadline:
            raise TimeoutError(f"no {what} in time")
        time.sleep(0.05)


def file_info(p):
    try:
        st = p.stat()
        return {"size": st.st_size, "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))}
    except OSError:
        return None


def game_save_run(inst, name, timeout):
    t0 = time.perf_counter()
    deadline = time.time() + timeout
    n = clean_save_name(name)
    sl_ready(inst, "save")
    st, _ = sl_exec(inst, SL_STATE)
    if st[0] == "menu":
        raise RuntimeError("cannot save: no game is running (main menu); start one with game_teleport or game_load")
    if st[0] == "noactor":
        raise RuntimeError("cannot save: the level has no actor yet (loading); retry in a moment")
    if st[0] == "dead":
        raise RuntimeError("cannot save: the actor is dead; use game_load to go back to a save")
    path = saves_dir(inst) / (n + SAVE_EXT)
    existed = path.is_file()
    since = inst.events.last_seq
    res = inst.call("console.exec", {"cmd": f"save {n}"}, timeout=30)
    bad = [l for l in res.get("lines", []) if "Save failed" in l or "cannot make saved game" in l]
    if bad:
        raise RuntimeError("save refused by the engine: " + "; ".join(bad))
    try:
        ev = sl_event(inst, ["game_saved"], since, lambda d: same_save(d.get("name"), n), deadline, "game_saved event")
    except TimeoutError:
        raise TimeoutError(f"save {n}: no game_saved event within {timeout} s; console: {res.get('lines', [])[-5:]}")
    save_s = time.perf_counter() - t0
    dds = path.with_suffix(".dds")
    for _ in range(40):  # the thumbnail is rendered a frame or two later
        if dds.is_file():
            break
        time.sleep(0.05)
    return {"instance": inst.id, "name": n, "file": str(path), "size": path.stat().st_size,
            "parts": {ext[1:]: file_info(path.with_suffix(ext)) for ext in SAVE_PARTS}, "overwritten": existed,
            "level": st[1], "position": [float(c) for c in st[2].split(",")], "money": int(float(st[3])),
            "health": float(st[4]), "event_seq": ev["seq"], "save_s": round(save_s, 2),
            "renamed": n != name or None}


def missing_entities(inst, log_since):
    secs = {}
    for _, line, _ in inst.lines(since=log_since):
        m = ENTITY_RX.search(line)
        if m:
            secs[m.group(1)] = secs.get(m.group(1), 0) + 1
    return secs


def load_crash(inst, name, log_since, t0):
    """The crash record for a load that killed the game, plus the save/data mismatch hints from the log."""
    t = time.time()
    while time.time() - t < WER_WAIT_S + 10 and not (inst.dead and inst.crash):
        time.sleep(0.25)
    secs = missing_entities(inst, log_since)
    out = {"instance": inst.id, "name": name, "loaded": False, "crashed": True, "alive": not inst.dead,
           "error": f"the game died while loading {name}: " + (inst.crash["reason"] if inst.crash else
                                                               f"exit code {inst.exit_code}"),
           "total_s": round(time.time() - t0, 1)}
    if secs:
        top = sorted(secs.items(), key=lambda kv: -kv[1])[:20]
        out["missing_sections"] = {"count": sum(secs.values()), "distinct": len(secs), "top": dict(top)}
        out["hint"] = ("the save references object sections that this game's configs do not have (made with mods "
                       "this instance does not run); load it in the game setup it was made with")
    out["crash"] = last_crash(inst, 30)
    return out


def load_wait(inst, name, ev_since, log_since, deadline, t0, want_level=None):
    """After a load command: game_loaded -> level_loaded -> actor_spawned events, then tp_wait's readiness probe."""
    try:
        gl = sl_event(inst, ["game_loaded"], ev_since, lambda d: same_save(d.get("name"), name), deadline,
                      f"game_loaded event for {name}")
        lv = sl_event(inst, ["level_loaded"], gl["seq"], lambda d: True, deadline, "level_loaded event")
        level = (lv.get("data") or {}).get("level")
        sl_event(inst, ["actor_spawned"], lv["seq"], lambda d: True, deadline, "actor_spawned event")
        tp_wait(inst, level, deadline)
    except Exception as e:
        if inst.dead or inst.crash:
            return load_crash(inst, name, log_since, t0)
        if isinstance(e, TimeoutError):
            hb = None
            try:
                hb = inst.call("engine.heartbeat", timeout=5)
            except Exception:
                pass
            raise TimeoutError(f"load of {name} not finished in time ({e}); the game is alive" +
                               (", still loading" if hb and hb.get("loading") else "") +
                               ": it may finish later (watch game_state), or use game_stop / game_restart") from None
        raise
    st, _ = sl_exec(inst, SL_STATE)
    out = {"instance": inst.id, "name": name, "loaded": True, "level": st[1],
           "position": [float(c) for c in st[2].split(",")], "money": int(float(st[3])), "health": float(st[4]),
           "load_s": round(time.time() - t0, 1)}
    if want_level and want_level != st[1]:
        out["note"] = f"save header says level {want_level}"
    secs = missing_entities(inst, log_since)
    if secs:
        out["missing_sections"] = {"count": sum(secs.values()), "distinct": len(secs),
                                   "top": dict(sorted(secs.items(), key=lambda kv: -kv[1])[:20])}
    return out


def game_load_run(inst, name, timeout):
    t0 = time.time()
    deadline = t0 + timeout
    name = check_load_name(name)
    sl_ready(inst, "load")
    if not (saves_dir(inst) / (name + SAVE_EXT)).is_file():
        names = save_names(inst)
        raise FileNotFoundError(f"no save {name!r} in {saves_dir(inst)}; saves: {names[:20] or 'none'}"
                                " (game_saves(import_from=...) copies one in)")
    info, _ = sl_exec(inst, SL_INFO, names="{" + lua_str(name) + "}")
    level, _, health = info[0].split("|")
    if health in ("invalid", "error"):
        raise ValueError(f"save {name!r} is not loadable here: version mismatch or corrupted ({health})")
    ev_since, log_since = inst.events.last_seq, inst.last_seq
    how, lines = sl_exec(inst, SL_LOAD, name=lua_str(name))
    for _ in range(10):  # stopped a tutorial (death screen): load from the menu it opened, a few frames later
        if how[0] != "tutorial":
            break
        time.sleep(0.3)
        how, lines = sl_exec(inst, SL_LOAD, name=lua_str(name))
    bad =[l for l in lines if l.startswith("! Cannot") or "ALife simulator has not been started" in l]
    if bad:
        raise RuntimeError(f"load of {name} refused: " + "; ".join(bad))
    out = load_wait(inst, name, ev_since, log_since, deadline, t0, level or None)
    out["via"] = "load (in game)" if how[0] == "load" else "start server (main menu)"
    return out


def game_saves_run(inst, delete, import_from, import_as, details):
    d = saves_dir(inst)
    done = {}
    if import_from:
        src = str(import_from)
        src = src[:-len(SAVE_EXT)] if src.lower().endswith(SAVE_EXT) else src
        if not Path(src + SAVE_EXT).is_file():
            raise FileNotFoundError(f"no {src + SAVE_EXT}")
        dst_name = clean_save_name(import_as) if import_as else check_load_name(Path(src).name)
        d.mkdir(parents=True, exist_ok=True)
        copied = []
        for ext in SAVE_PARTS:  # copy only: the source is never modified
            if Path(src + ext).is_file():
                shutil.copy2(src + ext, d / (dst_name + ext))
                copied.append(ext[1:])
        done["imported"] = {"name": dst_name, "from": src + SAVE_EXT, "parts": copied}
    if delete:
        n = check_load_name(delete)
        gone = []
        for ext in SAVE_PARTS:
            p = d / (n + ext)
            if p.is_file() and p.resolve().parent == d.resolve():
                p.unlink()
                gone.append(ext[1:])
        if not gone:
            raise FileNotFoundError(f"no save {n!r} in {d}")
        done["deleted"] = {"name": n, "parts": gone}
    names = save_names(inst)
    saves = []
    for n in names:
        p = d / (n + SAVE_EXT)
        e = {"name": n, **file_info(p)}
        e["size"] = sum((file_info(p.with_suffix(x)) or {"size": 0})["size"] for x in SAVE_PARTS)
        saves.append(e)
    note = None
    if details and saves:
        try:
            sl_ready(inst, "read save headers")
            info, _ = sl_exec(inst, SL_INFO, names="{" + ",".join(lua_str(n) for n in names) + "}", timeout=60)
            for e, r in zip(saves, info):
                lv, gt, hp = r.split("|")
                if hp in ("invalid", "error"):
                    e.update(valid=False, reason="version mismatch or corrupted" if hp == "invalid"
                             else "header read failed")
                else:
                    e.update(level=lv or None, game_time=gt, actor_health=float(hp))
        except Exception as ex:
            note = f"no levels/game times: {ex}"
    return {"instance": inst.id, "dir": str(d), **done, "count": len(saves), "saves": saves, "note": note}


@tool
async def game_save(name: str, instance: str | None = None, timeout: float = 60) -> dict:
    """Save the running game as `name` in the instance's own appdata (savedgames); returns once the save is on disk:
    file, size, parts (scop/scoc/dds), level, position, money, health, save_s. Name: letters, digits, _ - . (others
    become _; renamed is set then); an existing save of that name is overwritten. Uses the console `save` command, so
    the UI-only save vetoes (campfire-only saves, Agony/timer conditions, emission/combat checks) do not apply.
    Errors: main menu without a game, loading, actor dead."""
    inst = get(instance)
    return with_error_hint(inst, await run(game_save_run, inst, name, timeout))


@tool
async def game_load(name: str, instance: str | None = None, timeout: float = 600) -> dict:
    """Load save `name` (from game_saves) from the main menu or a running game, also while the actor is dead; returns
    once the actor is in control on the loaded level (same check as game_teleport): level, position, money, health,
    load_s, via. Missing save -> error listing the saves. If the game dies during the load the result has loaded=false,
    the crash record and, when the log shows skipped objects ("Can't create entity"), missing_sections: the save was
    made with mods whose sections this game does not have. Timeout -> error; the load may still finish later."""
    inst = get(instance)
    return with_error_hint(inst, await run(game_load_run, inst, name, timeout))


@tool
async def game_saves(instance: str | None = None, delete: str | None = None, import_from: str | None = None,
                     import_as: str | None = None, details: bool = True) -> dict:
    """List the instance's saves, newest first: name, size (all parts), mtime, and with details (game running, not
    loading) level, game_time, actor_health read from the save. import_from: a .scop path (e.g.
    C:\\Anomaly\\appdata\\savedgames\\1.scop) copied with its .scoc/.dds into the instance's savedgames, optionally as
    import_as (the source is never modified). delete: removes that save (all parts) from the instance's own
    savedgames only."""
    return await run(game_saves_run, get(instance), delete, import_from, import_as, details)


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

def perf_window(inst, seconds, **extra):
    """perf.start, wait, then poll perf.report until the engine closes the window (first frame after `seconds`)."""
    inst.call("perf.start", {"seconds": seconds, **extra}, timeout=15)
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


# ---------------------------------------------------------------- game_lua_profile

LUA_PROFILE_MODES = ("time", "calls", "alloc")


def lua_profile(inst, seconds, top, mode):
    rep = perf_window(inst, seconds, lua_profile=mode, top=top)
    m, t, mem = rep.get("main", {}), rep.get("tasks", {}), rep.get("memory", {})
    fm = rep.get("frame_ms", {})
    return {
        "instance": inst.id,
        "window": {"frames": rep.get("frames"), "seconds": rep.get("seconds"), "fps": rep.get("fps"),
                   "frame_ms": {k: fm.get(k) for k in ("avg", "p50", "p99", "max")},
                   "hitches": rep.get("hitches"), "capped": rep.get("cap", {}).get("capped")},
        "game_perf_lua": {"main_lua_ms": m.get("lua_ms"), "main_lua_pct": m.get("lua_pct"),
                          "main_entries": m.get("lua_entries"), "worker_lua_ms": t.get("lua_ms")},
        "gc": {"main_gc_ms": m.get("gc_ms"), "main_gc_steps": m.get("gc_steps"), "tasks_gc_ms": t.get("gc_ms"),
               "tasks_gc_steps": t.get("gc_steps"), "lua_heap_mb": mem.get("lua_heap_mb"),
               "lua_growth_kb_per_frame": mem.get("lua_growth_kb_per_frame")},
        "profile": rep.get("lua_profile"),
    }


@tool
async def game_lua_profile(instance: str | None = None, seconds: float = 5, top: int = 30,
                           mode: str = "time") -> dict:
    """Profile the game's Lua over `seconds` (0.1..120): WHICH function / script / callback / entry point costs time.

    All ms are per frame; pct = % of that thread's Lua time, frame_pct = % of the average frame. profile.threads.main
    and .worker (worker = GameThread tasks; only present if any ran) each have:
    - entries (EXACT): outermost engine->Lua calls (luabind functor/binder method/script thread resume) by the
      called function: ms, calls_per_frame, max_ms (worst single call). E.g. bind_stalker.script actor update.
    - functions (SAMPLED): self_ms (own code + the C/engine functions it calls), total_ms (inclusive), samples,
      gc_pct (share of self samples inside GC = allocation pressure). functions_by_total = call-tree view.
    - files: self time per script. callbacks: RegisterScriptCallback dispatch (axr_main make_callback) per callback
      name with its listener functions (inclusive ms), e.g. actor_on_update -> my_mod.script:12 (on_update).
    - vmstate_pct of samples (jit/interp/c/gc/compiler); sampler_ms = profiler's own time (excluded from lua_ms).
    Sampling: LuaJIT timer ~1-2 ms, only ticks inside a luabind entry count; scaled to the exact Lua time, so short
    windows / cheap functions are noisy (check samples). worst_frames: the 5 slowest frames with their top exact
    entries and sampled functions (hitch attribution). game_perf_lua is game_perf's Lua time for the same window
    (includes the sampler's overhead); gc = explicit GC steps and Lua heap. Fn ids are script:linedefined.
    mode: "time" (default, ~1-3 % overhead), "calls" (+ exact calls_per_frame per function via a call hook, JIT
    off for the window: slow, times inflated), "alloc" (jit.allocprof allocation KB/frame per function and file,
    byte-sampled, JIT off; no time sampling). The JIT is restored after the window. Survives level changes
    (lua_state_closes counts re-arms). Not covered: Lua run by raw lua_pcall (script loading, lua_exec)."""
    inst = get(instance)
    if not 0.1 <= seconds <= 120:
        raise ValueError("seconds must be 0.1..120")
    if mode not in LUA_PROFILE_MODES:
        raise ValueError(f"mode must be one of {LUA_PROFILE_MODES}")
    return await run(lua_profile, inst, seconds, max(1, min(int(top), 200)), mode)


# ---------------------------------------------------------------- scenario tools
# game_spawnable / game_spawn / game_give / game_actor / game_kill / game_release / game_weather / game_emission:
# guarded lua.exec chunks through Anomaly's own script paths (debug spawner lists and squad creation, alife_create(_item),
# alife_release, level_weathers, surge/psi-storm managers). Each chunk returns one JSON string ({error} on failure).

SC_PRE = r'''
local A = @ARGS@
local function J(v)
  local t = type(v)
  if t == "nil" then return "null" end
  if t == "boolean" then return tostring(v) end
  if t == "number" then
    if v ~= v or v == math.huge or v == -math.huge then return "null" end
    if v == math.floor(v) and math.abs(v) < 1e15 then return string.format("%d", v) end
    return string.format("%.3f", v)
  end
  if t == "table" then
    local o = {}
    if v[1] ~= nil or next(v) == nil then
      for i = 1, #v do o[i] = J(v[i]) end
      return "[" .. table.concat(o, ",") .. "]"
    end
    for k, x in pairs(v) do o[#o + 1] = J(tostring(k)) .. ":" .. J(x) end
    return "{" .. table.concat(o, ",") .. "}"
  end
  return '"' .. (tostring(v):gsub('[%c"\\]', function(c) return string.format("\\u%04x", c:byte()) end)) .. '"'
end
local function fail(m) return J({error = m}) end
local function in_game() return level.present() and db.actor ~= nil and alife() ~= nil end
local NOGAME = "not in game (main menu or no actor yet); start one with game_teleport"
local BAD = 4294967295
local function r2(x) return math.floor(x * 100 + 0.5) / 100 end
local function P(p) return {r2(p.x), r2(p.y), r2(p.z)} end
local function snap(p, maxr) -- the point's AI node, else the nearest on rings up to maxr m
  local v = level.vertex_id(p)
  if v < BAD then return v, 0 end
  for d = 1, maxr do
    local n = math.max(8, math.floor(2 * math.pi * d / 1.5))
    for k = 0, n - 1 do
      local a = 2 * math.pi * k / n
      v = level.vertex_id(vector():set(p.x + d * math.cos(a), p.y, p.z + d * math.sin(a)))
      if v < BAD then return v, d end
    end
  end
end
local function mod(name) -- a game script module (loaded on demand); nil if a mod removed it
  local ok, m = pcall(function() return _G[name] end)
  return ok and type(m) == "table" and m or nil
end
local function creature(o) return IsStalker(o) or IsMonster(o) end
'''

SC_CATALOG = r'''
local m = mod("ui_debug_main")
if not (m and type(m.get_spawn_table) == "function" and type(m.id_to_spawn_table) == "table") then
  return fail("ui_debug_main.get_spawn_table (the debug spawner's section lists) is not available")
end
local out = {}
for _, name in ipairs(m.id_to_spawn_table) do
  for _, sec in ipairs(m.get_spawn_table(name) or {}) do
    out[#out + 1] = name .. "|" .. sec .. "|" .. (name:find("^Squads") and ini_sys:r_string_ex(sec, "faction") or "")
  end
end
return J(table.concat(out, "\n"))
'''

SC_SPAWN = r'''
if not in_game() then return fail(NOGAME) end
local a, sec, kind, n, rel = db.actor, A.section, A.kind, A.count, A.relation
local base, clip, turned
if A.pos then
  base = vector():set(A.pos[1], A.pos[2], A.pos[3])
else -- dist m ahead of the camera, horizontally; blocked by level geometry -> the freest of 8 directions
  local d, p = device().cam_dir, a:position()
  local yaw = math.atan2(d.x, d.z)
  local best, bd, turn = nil, -1, 0
  for k = 0, 7 do
    local y = yaw + (k % 2 == 1 and 1 or -1) * math.ceil(k / 2) * math.pi / 4
    local h = vector():set(math.sin(y), 0, math.cos(y))
    local rq = ray_pick(vector():set(p.x, p.y + 1.5, p.z), h, A.dist, rq_target.rqtStatic, a)
    local free = (rq:query() and rq:get_distance() < A.dist) and math.max(0.5, rq:get_distance() - 0.7) or A.dist
    if free > bd + 0.5 then best, bd, turn = h, free, math.floor(math.deg(y - yaw) + 0.5) end
    if free >= A.dist or k == 0 and not A.turn then break end
  end
  if bd < A.dist then clip = r2(bd) end
  if turn ~= 0 then turned = (turn + 540) % 360 - 180 end
  base = vector():set(p.x + best.x * bd, p.y, p.z + best.z * bd)
end
local lv0, moved = snap(base, 30)
if not lv0 then return fail("position " .. J(P(base)) .. " is not within 30 m of the AI map of " .. level.name()) end
local center, gv = level.vertex_position(lv0), a:game_vertex_id()
local ring = n > 1 and math.max(A.spacing, A.spacing * n / (2 * math.pi)) or 0
local gw = rel == "enemy" and -1000 or rel == "friend" and 1000 or 0
local function info(se, k) return {id = se.id, section = se:section_name(), kind = k, position = P(se.position)} end
local out, notes = {}, {}
for i = 1, n do
  local p, lv = center, lv0
  if ring > 0 then
    local ang = 2 * math.pi * (i - 1) / n
    local l = snap(vector():set(center.x + ring * math.cos(ang), center.y, center.z + ring * math.sin(ang)), 3)
    if l then p, lv = level.vertex_position(l), l end
  end
  local se
  if kind == "squad" then -- as the debug spawner / create_squad_at_named_location: no smart, sim picks a target
    se = alife_create(sec, p, lv, gv)
    if se then
      se:create_npc(nil, p, lv, gv)
      local o = info(se, "squad")
      o.faction, o.members = se.player_id, {}
      for k in se:squad_members() do
        local m = k.object or alife():object(k.id)
        if m then
          SIMBOARD:setup_squad_and_group(m)
          SendScriptCallback("squad_on_npc_creation", se, m)
          o.members[#o.members + 1] = {id = m.id, section = m:section_name(), community = alife_character_community(m)}
        end
      end
      if rel then se:set_squad_relation(rel) end
      out[#out + 1] = o
    end
  elseif kind == "item" then
    se = alife_create_item(sec, {vector():set(p.x, p.y + 0.3, p.z), lv, gv})
    if se then out[#out + 1] = info(se, kind) end
  else
    se = alife_create(sec, p, lv, gv)
    if se then
      local stpk = kind == "anomaly" and mod("utils_stpk")
      local data = stpk and stpk.get_object_data(se)
      if data then -- the debug spawner's zone setup: a 3 m sphere
        data.object_flags, data.restrictor_type = 31, 0
        data.shapes = {{shtype = 0, offset = VEC_ZERO, radius = 3}}
        stpk.set_object_data(data, se)
      end
      if rel and IsStalker(nil, se:clsid()) then se:force_set_goodwill(gw, 0) end
      out[#out + 1] = info(se, kind)
    end
  end
  if not se then notes[#notes + 1] = "creating #" .. i .. " failed (see log_tail)" end
end
return J({objects = out, center = P(center), snapped_m = moved, clipped_m = clip, turned_deg = turned, level = level.name(), notes = notes})
'''

SC_ONLINE = r'''
if not in_game() then return fail(NOGAME) end
local out = {}
for _, id in ipairs(A.ids) do
  local o = level.object_by_id(id)
  local r = {id = id, exists = alife():object(id) ~= nil, online = o ~= nil}
  if o then
    r.position = P(o:position())
    if creature(o) then r.alive = o:alive() end
  end
  out[#out + 1] = r
end
return J(out)
'''

SC_GIVE = r'''
if not in_game() then return fail(NOGAME) end
local a, sec, ids = db.actor, A.section, {}
if A.remove then
  a:iterate_inventory(function(_, it)
    if it:section() == sec and (A.count == 0 or #ids < A.count) then ids[#ids + 1] = it:id() end
  end, a)
  for _, id in ipairs(ids) do alife_release_id(id) end
  return J({removed = ids})
end
if A.rounds then -- itms_manager splits the rounds into full boxes + a remainder
  local t = alife_create_item(sec, a, {ammo = A.rounds})
  for _, se in ipairs(type(t) == "table" and t or {t}) do ids[#ids + 1] = se.id end
else
  for _ = 1, A.count do
    local se = alife_create_item(sec, a)
    if se then ids[#ids + 1] = se.id end
  end
end
return J({ids = ids})
'''

SC_EQUIP = r'''
if not in_game() then return fail(NOGAME) end
local a, out = db.actor, {}
for _, id in ipairs(A.ids) do
  local o = level.object_by_id(id)
  local sec = o and o:section()
  local slot = sec and ini_sys:line_exist(sec, "slot") and ini_sys:r_s32(sec, "slot") or -1
  if not o then out[#out + 1] = {id = id, error = "not online yet"}
  elseif slot < 0 then out[#out + 1] = {id = id, error = sec .. " has no inventory slot"}
  else -- the move is an inventory event: the slot changes a frame later (A.check polls it)
    local w = a:item_in_slot(slot)
    local ok = w ~= nil and w:id() == id
    if not (ok or A.check) then a:move_to_slot(o, slot) end
    if ok and A.check and IsWeapon(o) and a:active_slot() ~= slot then a:activate_slot(slot) end
    out[#out + 1] = {id = id, slot = slot, equipped = ok}
  end
end
return J(out)
'''

SC_ACTOR = r'''
if not in_game() then return fail(NOGAME) end
local a, S = db.actor, A.set
if not a:alive() then return fail("the actor is dead") end
-- absolute setters (the change_* ones only queue a delta for the next condition update)
if S.health then a:set_health_ex(S.health) end
if S.stamina then a.power = S.stamina end
if S.radiation then a.radiation = S.radiation end
if S.satiety then a.satiety = S.satiety end
if S.psy_health then a.psy_health = S.psy_health end
if S.bleeding == 0 then a.bleeding = 1 end -- ChangeBleeding(1) heals every wound
if S.money then a:give_money(S.money - a:money()) end
return J({ok = true})
'''

SC_KILL = r'''
if not in_game() then return fail(NOGAME) end
local out = {}
local function kill(id, squad)
  local o, r = level.object_by_id(id), {id = id, squad = squad}
  if not o then r.result = alife():object(id) and "offline (not on this level's online radius); use game_release" or "gone"
  elseif not creature(o) then r.result = "not a creature (" .. o:section() .. ")"
  elseif not o:alive() then r.result = "already dead"
  else o:kill(A.by_actor and db.actor or o) r.result = "killed" r.section = o:section() end
  out[#out + 1] = r
end
for _, id in ipairs(A.ids) do
  local se = alife():object(id)
  if se and (se:clsid() == clsid.online_offline_group_s or se:clsid() == clsid.online_offline_group) then
    for k in se:squad_members() do kill(k.id, id) end
  elseif id == 0 then out[#out + 1] = {id = 0, result = "the actor; use game_actor or lua db.actor:kill(db.actor)"}
  else kill(id) end
end
return J(out)
'''

SC_RELEASE = r'''
if not in_game() then return fail(NOGAME) end
local out = {}
for _, e in ipairs(A.items) do
  local id, want = e[1], e[2]
  local se = alife():object(id)
  if not se then out[#out + 1] = {id = id, result = "gone"}
  elseif id == 0 then out[#out + 1] = {id = id, result = "the actor; not released"}
  elseif want and se:section_name() ~= want then
    out[#out + 1] = {id = id, result = "id now belongs to " .. se:section_name() .. "; skipped"}
  else
    local sec = se:section_name()
    if not alife_release(se) then -- alife_release skips creatures without a squad (debug-spawned ones)
      alife_record(se, false)
      alife():release(se, true)
    end
    out[#out + 1] = {id = id, section = sec, result = alife():object(id) and "release pending" or "released"}
  end
end
return J(out)
'''

SC_WEATHER = r'''
if not in_game() then return fail(NOGAME) end
local lw = mod("level_weathers")
if not (lw and type(lw.get_weather_manager) == "function") then
  return fail("level_weathers.get_weather_manager is not available")
end
local wm = lw.get_weather_manager()
local function cur()
  return {weather = level.get_weather(), cycle = wm.cycle, preset = wm.preset, weather_type = wm.weatherType,
    time = string.format("%02d:%02d", level.get_time_hours(), level.get_time_minutes()), day = level.get_time_days(),
    weather_fx = level.is_wfx_playing()}
end
local cycles, owner, files = {}, {}, {}
for c, ps in pairs(wm.presets or {}) do
  local l = {}
  for p in pairs(ps) do l[#l + 1] = p owner[p] = c end
  table.sort(l)
  cycles[c] = l
end
local fl = getFS():file_list_open("$game_weathers$", 1)
for i = 0, fl:Size() - 1 do files[#files + 1] = (fl:GetAt(i):gsub("%.ltx$", "")) end
fl:Free()
table.sort(files)
local other = {}
for _, f in ipairs(files) do if not owner[f] then other[#other + 1] = f end end
local before, notes, W = cur(), {}, A.weather
local cyc, pre, isfile
if W then
  if cycles[W] then cyc = W pre = owner[wm.preset or ""] == W and wm.preset or cycles[W][math.random(#cycles[W])]
  elseif owner[W] then cyc, pre = owner[W], W end
  for _, f in ipairs(files) do if f == W then isfile = true end end
  if not (cyc or isfile) then
    local sim, lw = {}, W:lower()
    for _, c in ipairs(files) do if lw:find(c:lower(), 1, true) or c:lower():find(lw, 1, true) then sim[#sim + 1] = c end end
    for c in pairs(cycles) do if lw:find(c, 1, true) or c:find(lw, 1, true) then sim[#sim + 1] = c end end
    return J({error = "unknown weather '" .. W .. "'" .. (#sim > 0 and "; similar: " .. table.concat(sim, ", ") or ""),
      cycles = cycles, other = other})
  end
end
local mins = 0
if A.minute_of_day then mins = (A.minute_of_day - (level.get_time_hours() * 60 + level.get_time_minutes())) % 1440 end
if A.advance then mins = mins + math.floor(A.advance * 60 + 0.5) end
if mins > 0 then
  level.change_game_time(math.floor(mins / 1440), math.floor(mins % 1440 / 60), mins % 60)
  wm:forced_weather_change()
end
if W then
  if wm.weatherType ~= "atmosfear" then
    notes[#notes + 1] = level.name() .. " has the fixed weather type " .. tostring(wm.weatherType) ..
      " (indoor/underground): level_weathers puts it back at the next game hour"
  end
  if level.is_wfx_playing() then notes[#notes + 1] = "a weather effect (surge/psi-storm) is playing and overrides the weather until it ends" end
  if cyc and wm.weatherType == "atmosfear" then -- through the manager, so it sticks for a full weather period
    wm.cycle, wm.preset = cyc, pre
    wm.transition_period, wm.pre_blowout_period, wm.weather_fx = false, false, nil
    wm:reset_change_date(true)
    wm:select_weather(true)
  else
    level.set_weather(W, true)
    if not cyc then notes[#notes + 1] = W .. " is not a cycle preset: level_weathers replaces it at the next game hour" end
  end
end
return J({before = before, after = cur(), advanced_minutes = mins, notes = notes,
  cycles = A.list and cycles or nil, other = A.list and other or nil})
'''

SC_EMISSION = r'''
if not in_game() then return fail(NOGAME) end
local K, act = A.kind, A.action
local name = K == "surge" and "surge_manager" or "psi_storm_manager"
local M = mod(name)
local get = M and (K == "surge" and M.get_surge_manager or M.get_psi_storm_manager)
local start = M and (K == "surge" and M.start_surge or M.start_psi_storm)
local stop = M and (K == "surge" and M.stop_surge or M.stop_psi_storm)
if type(get) ~= "function" or type(start) ~= "function" or type(stop) ~= "function" then
  return fail(name .. " (get/start/stop functions) is not available in this game's scripts")
end
local m, notes = get(), {}
local function state()
  local s = {started = m.started == true, level = level.name(), weather_fx = level.is_wfx_playing(),
    time_factor = level.get_time_factor()}
  if m.started and m.inited_time then
    s.elapsed_s = math.floor(game.get_game_time():diffSec(m.inited_time) / level.get_time_factor())
  end
  if K == "surge" then s.duration_s, s.actor_in_cover = m.surge_time, GetEvent("current_safe_cover") and true or false end
  return s
end
local before = state()
if act == "start" then
  if m.started then notes[#notes + 1] = K .. " already running"
  else
    start()
    if not m.started then
      local lv = mod("level_weathers")
      local opt = mod("ui_options")
      local key = K == "surge" and "alife/event/emission_state" or "alife/event/psi_storm_state"
      if lv and lv.valid_levels and lv.valid_levels[level.name()] ~= true then
        notes[#notes + 1] = "skipped: " .. level.name() .. " has no " .. K .. "s (underground/indoor level)"
      elseif opt and not opt.get(key) then notes[#notes + 1] = "skipped: disabled in the game options (" .. key .. ")"
      else notes[#notes + 1] = "skipped by " .. name .. " (level exception or an on_before_* callback)" end
    end
  end
elseif act == "stop" then
  if m.started then stop() else notes[#notes + 1] = K .. " was not running" end
end
return J({kind = K, action = act, before = before, after = state(), notes = notes})
'''

SC_CACHE = {}  # (instance id, pid) -> catalog
SC_SPAWNED = {}  # instance id -> {"pid": pid, "objects": [{id, section, kind, level}]}
SC_ALIASES = {  # kind alias -> category key patterns (keys come from the debug spawner's list names)
    "item": ["artefacts*", "items_*", "helmets", "outfits_*", "weapons_*"],
    "weapon": ["weapons_melee", "weapons_pistol", "weapons_shotgun", "weapons_smg", "weapons_rifle", "weapons_sniper",
               "weapons_explosive", "weapons_misc"],
    "ammo": ["weapons_ammo"], "outfit": ["outfits_*", "helmets"], "artefact": ["artefacts*"],
    "monster": ["npc_mutant"], "stalker": ["npc_stalker"], "npc": ["npc_*"],
    "squad": ["squads_*"], "monster_squad": ["squads_mutant"], "stalker_squad": ["squads_stalker"],
}
SC_SPAWN_KIND = {"npc_mutant": "monster", "npc_stalker": "stalker", "vehicles": "vehicle", "anomaly": "anomaly",
                 "physic_misc": "physic"}
SC_DEFAULTS = {"item": (2, 0.6), "monster": (15, 2), "stalker": (15, 2), "squad": (25, 8), "vehicle": (8, 6),
               "anomaly": (8, 6), "physic": (4, 1.5)}  # kind -> (distance ahead m, spacing m)
RELATIONS = ("enemy", "neutral", "friend")


def sc_lit(v):
    """Python value -> Lua literal (args of the SC_* chunks)."""
    if v is None:
        return "nil"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("numbers must be finite")
        return repr(v)
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r") + '"'
    if isinstance(v, (list, tuple)):
        return "{" + ",".join(sc_lit(x) for x in v) + "}"
    if isinstance(v, dict):
        return "{" + ",".join(f"[{sc_lit(str(k))}]={sc_lit(x)}" for k, x in v.items()) + "}"
    raise TypeError(f"cannot pass {type(v).__name__} to Lua")


def sc_exec(inst, code, args=None, timeout=30):
    if inst.dead:
        raise RuntimeError(f"instance {inst.id} is not running (exit code {inst.exit_code})")
    if inst.hung:
        raise RuntimeError("the game is hung (see game_status)")
    if inst.call("engine.heartbeat", timeout=5).get("loading"):
        raise RuntimeError("a level is loading; retry once game_state shows in_game")
    res = inst.call("lua.exec", {"code": SC_PRE.replace("@ARGS@", sc_lit(args or {})) + code}, timeout=timeout)
    out = json.loads(res["results"][0])
    if isinstance(out, dict) and out.get("error"):
        extra = "; ".join(f"{k}: {json.dumps(v)}" for k, v in out.items() if k != "error")
        raise ValueError(out["error"] + (f"; {extra}" if extra else ""))
    return out


def sc_key(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def sc_catalog(inst):
    """{section: (category key, faction)} from the debug spawner lists; cached per game process."""
    key = (inst.id, inst.pid)
    if key not in SC_CACHE:
        cat = {}
        for line in sc_exec(inst, SC_CATALOG, timeout=60).splitlines():
            name, sec, fac = line.split("|")
            cat.setdefault(sec, (sc_key(name), fac or None))
        SC_CACHE[key] = cat
    return SC_CACHE[key]


def sc_section(inst, section):
    cat = sc_catalog(inst)
    if section in cat:
        return cat[section]
    close = difflib.get_close_matches(section, list(cat), n=8, cutoff=0.6)
    sub = [s for s in sorted(cat) if section.lower() in s.lower()][:8]
    hint = list(dict.fromkeys(sub + close))[:10]
    raise ValueError(f"unknown or non-spawnable section {section!r}" + (f"; similar: {', '.join(hint)}" if hint else "")
                     + " (see game_spawnable)")


def sc_kind_match(kind, cat_key):
    pats = SC_ALIASES.get(kind, [kind])
    return any(fnmatch.fnmatchcase(cat_key, p) for p in pats)


def sc_spawned(inst):
    reg = SC_SPAWNED.get(inst.id)
    if not reg or reg["pid"] != inst.pid:
        reg = SC_SPAWNED[inst.id] = {"pid": inst.pid, "objects": []}
    return reg["objects"]


def sc_wait_online(inst, ids, wait):
    """SC_ONLINE until every id has a game object or `wait` s passed (online objects appear a frame or more later)."""
    deadline = time.time() + wait
    while True:
        st = sc_exec(inst, SC_ONLINE, {"ids": ids}) if ids else []
        if all(s["online"] or not s["exists"] for s in st) or time.time() >= deadline:
            return {s["id"]: s for s in st}
        time.sleep(0.1)


def spawnable(inst, kind, pattern, faction, limit):
    cat = sc_catalog(inst)
    if not (kind or pattern or faction):
        cats, facs = {}, {}
        for c, f in cat.values():
            cats[c] = cats.get(c, 0) + 1
            if f:
                facs[f] = facs.get(f, 0) + 1
        return {"instance": inst.id, "sections": len(cat), "categories": dict(sorted(cats.items())),
                "aliases": {k: v for k, v in SC_ALIASES.items()}, "squad_factions": dict(sorted(facs.items())),
                "hint": "filter with kind (alias or category key/glob), pattern (glob on the section name), faction"}
    if kind and kind not in SC_ALIASES and not any(sc_kind_match(kind, c) for c, _ in cat.values()):
        raise ValueError(f"unknown kind {kind!r}; aliases: {', '.join(SC_ALIASES)}; categories: "
                         + ", ".join(sorted({c for c, _ in cat.values()})))
    pat = pattern if pattern and any(ch in pattern for ch in "*?[") else f"*{pattern}*" if pattern else None
    hits = sorted((s, c, f) for s, (c, f) in cat.items()
                  if (not kind or sc_kind_match(kind, c)) and (not pat or fnmatch.fnmatch(s, pat))
                  and (not faction or f == faction))
    shown = hits[:limit]
    by_cat = {}
    for s, c, _ in shown:
        by_cat.setdefault(c, []).append(s)
    facs = {s: f for s, _, f in shown if f}
    return {"instance": inst.id, "matched": len(hits), "by_category": by_cat, "squad_factions": facs or None,
            "omitted": f"{len(hits) - len(shown)} more; narrow with pattern or raise limit" if len(hits) > len(shown)
            else None}


def spawn(inst, section, count, position, distance, relation, wait):
    cat_key, _ = sc_section(inst, section)
    if cat_key == "phantom":
        raise ValueError("phantoms are client-only effects and cannot be spawned as objects")
    kind = "squad" if cat_key.startswith("squads_") else SC_SPAWN_KIND.get(cat_key, "item")
    if not 1 <= count <= 50:
        raise ValueError("count must be 1..50")
    if position is not None and len(position) != 3:
        raise ValueError("position must be [x, y, z]")
    if relation is not None and relation not in RELATIONS:
        raise ValueError(f"relation must be one of {RELATIONS}")
    notes = []
    if relation and kind not in ("squad", "stalker"):
        notes.append(f"relation ignored for {kind}s")
        relation = None
    dist, spacing = SC_DEFAULTS[kind]
    res = sc_exec(inst, SC_SPAWN, {"section": section, "kind": kind, "count": count, "relation": relation,
                                   "pos": list(position) if position is not None else None,
                                   "dist": float(distance if distance is not None else dist), "spacing": spacing,
                                   "turn": kind != "item"})
    reg = sc_spawned(inst)
    ids = []
    for o in res["objects"]:
        reg.append({"id": o["id"], "section": o["section"], "kind": o["kind"], "level": res["level"]})
        ids += [m["id"] for m in o.get("members", [])] or [o["id"]]
    st = sc_wait_online(inst, ids, wait) if wait > 0 else {}
    for o in res["objects"]:
        for m in o.get("members", []) or [o]:
            s = st.get(m["id"])
            if s:
                m["online"] = s["online"]
                if s.get("position"):
                    m["position"] = s["position"]
    if res.get("turned_deg") is not None:
        notes.append(f"level geometry ahead: placed {res['turned_deg']} deg off the view direction (+ = right)")
    if res.get("clipped_m") is not None:
        notes.append(f"level geometry ahead: distance shortened to {res['clipped_m']} m")
    if res["snapped_m"] > 1:
        notes.append(f"point off the AI map; snapped {res['snapped_m']} m to the nearest node")
    if any(m.get("online") is False for o in res["objects"] for m in o.get("members", []) or [o]):
        notes.append("some objects are still offline (alife only): far from the actor or not online yet")
    return {"instance": inst.id, "level": res["level"], "section": section, "kind": kind, "category": cat_key,
            "center": res["center"], "objects": res["objects"], "notes": (notes + res["notes"]) or None}


def give(inst, section, count, rounds, equip, remove):
    cat_key, _ = sc_section(inst, section)
    if not sc_kind_match("item", cat_key):
        raise ValueError(f"{section} is a {cat_key} section, not an inventory item; use game_spawn")
    if remove:
        if count < 0:
            raise ValueError("count must be >= 0 (0 = all)")
        out = sc_exec(inst, SC_GIVE, {"section": section, "count": count, "remove": True})
        return {"instance": inst.id, "section": section, "removed_ids": out["removed"]}
    if rounds is not None and (cat_key != "weapons_ammo" or rounds < 1):
        raise ValueError("rounds (>= 1) is only for ammo sections")
    if not 1 <= count <= 100:
        raise ValueError("count must be 1..100")
    out = sc_exec(inst, SC_GIVE, {"section": section, "count": count, "rounds": rounds})
    res = {"instance": inst.id, "section": section, "ids": out["ids"]}
    if equip:
        ids = out["ids"][:1]
        sc_wait_online(inst, ids, 3)
        eq = sc_exec(inst, SC_EQUIP, {"ids": ids})
        deadline = time.time() + 2
        while not eq[0].get("error") and time.time() < deadline:
            time.sleep(0.1)
            eq = sc_exec(inst, SC_EQUIP, {"ids": ids, "check": True})
            if eq[0].get("equipped"):
                break
        res["equip"] = eq[0]
    return res


def actor_set(inst, **values):
    s = {k: v for k, v in values.items() if v is not None and k != "heal"}
    for k, v in s.items():
        if k != "money" and not 0 <= v <= 1:
            raise ValueError(f"{k} must be 0..1")
    if s.get("bleeding") not in (None, 0):
        raise ValueError("bleeding: only 0 (stop all bleeding) is supported")
    if values.get("heal"):
        s = {"health": 1, "stamina": 1, "radiation": 0, "psy_health": 1, "bleeding": 0, **s}
    if "money" in s:
        s["money"] = int(s["money"])
        if s["money"] < 0:
            raise ValueError("money must be >= 0")
    def stats():  # engine values (Lua's actor.satiety getter always reads 1)
        a = inst.call("state.get", {}, timeout=3).get("actor") or {}
        return {k: a.get(k) for k in ("health", "stamina", "radiation", "satiety", "psy_health", "bleeding", "money",
                                      "alive", "god_mode")}

    res = {"before": stats()}
    sc_exec(inst, SC_ACTOR, {"set": s})
    res["after"] = stats()
    off = [k for k, v in s.items() if res["after"].get(k) is not None and abs(res["after"][k] - v) > 0.01]
    if off:
        res["notes"] = f"not applied: {', '.join(off)}" + (
            " (god mode is on and keeps the stats; console_exec 'g_god off')" if res["after"].get("god_mode") else "")
    return {"instance": inst.id, **res}


def release(inst, ids, spawned):
    reg = sc_spawned(inst)
    known = {o["id"]: o["section"] for o in reg}
    items = [[int(i), known[int(i)]] if int(i) in known else [int(i)] for i in ids or []]
    if spawned:
        items += [[o["id"], o["section"]] for o in reg if o["id"] not in {i[0] for i in items}]
    if not items:
        return {"instance": inst.id, "results": [], "notes": "nothing to release"}
    out = sc_exec(inst, SC_RELEASE, {"items": items})
    pending = [r for r in out if r["result"] == "release pending"]
    deadline = time.time() + 2
    while pending and time.time() < deadline:  # squads/online objects go away a frame or more later
        time.sleep(0.1)
        st = sc_wait_online(inst, [r["id"] for r in pending], 0)
        for r in pending:
            if not st[r["id"]]["exists"]:
                r["result"] = "released"
        pending = [r for r in pending if r["result"] == "release pending"]
    done ={r["id"] for r in out if r["result"] in ("released", "release pending", "gone")
            or r["result"].startswith("id now")}
    reg[:] = [o for o in reg if o["id"] not in done]
    return {"instance": inst.id, "results": out, "still_tracked": len(reg)}


def weather_set(inst, name, time_of_day, advance_hours):
    args = {"weather": name, "advance": advance_hours, "list": not (name or time_of_day is not None or advance_hours)}
    if time_of_day is not None:
        m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?", str(time_of_day).strip())
        if not m or int(m[1]) > 23 or int(m[2] or 0) > 59:
            raise ValueError("time must be 'HH:MM' (00:00..23:59) or an hour 0..23")
        args["minute_of_day"] = int(m[1]) * 60 + int(m[2] or 0)
    if advance_hours is not None and not 0 <= advance_hours <= 24 * 30:
        raise ValueError("advance_hours must be 0..720")
    res = sc_exec(inst, SC_WEATHER, args)
    if res.get("other"):
        res["other"], extra = res["other"][:100], len(res["other"]) - 100
        if extra > 0:
            res["other_omitted"] = extra
    return {"instance": inst.id, **res}


@tool
async def game_spawnable(instance: str | None = None, kind: str | None = None, pattern: str | None = None,
                         faction: str | None = None, limit: int = 200) -> dict:
    """Spawnable sections from the game's data (Anomaly's debug spawner lists, so mods' items are included).

    No filter -> categories with counts, kind aliases and squad factions. kind: alias (item, weapon, ammo, outfit,
    artefact, monster, stalker, npc, squad, monster_squad, stalker_squad) or a category key / glob (e.g. items_food,
    weapons_rifle, squads_stalker). pattern: glob on the section name (no wildcard = substring). faction: squads of
    that faction (e.g. bandit, duty, monster_predatory_day). limit: max sections listed (default 200)."""
    inst = get(instance)
    return await run(spawnable, inst, kind, pattern, faction, max(1, min(int(limit), 2000)))


@tool
async def game_spawn(section: str, count: int = 1, position: list[float] | None = None,
                     distance: float | None = None, relation: str | None = None, instance: str | None = None,
                     wait: float = 3) -> dict:
    """Spawn count (1..50) objects of a spawnable section (see game_spawnable): items on the ground, monsters,
    stalkers, squads (alife squad sections: members are created and the squad joins the simulation like a natural
    one), anomalies, vehicles, physics objects. Saves/loads like natural objects.

    Placement: position [x, y, z], else distance m ahead of the camera (default item 2, monster/stalker 15,
    squad 25, others 4-8), snapped to the nearest AI-map node (within 30 m; never inside geometry); count > 1 spreads
    them on a ring. relation (squads/stalkers): enemy | neutral | friend towards the actor (default: faction's).
    Returns alife ids at once; waits up to `wait` s for the game objects (online true/false per object; squads far
    from the actor stay offline). Every spawn is tracked for game_release(spawned=True). Kill with game_kill."""
    inst = get(instance)
    return with_error_hint(inst, await run(spawn, inst, section, int(count), position, distance, relation,
                                           max(0.0, min(float(wait), 30.0))))


@tool
async def game_give(section: str, count: int = 1, rounds: int | None = None, equip: bool = False,
                    remove: bool = False, instance: str | None = None) -> dict:
    """Give the actor count (1..100) inventory items of section (see game_spawnable kind=item). Ammo: count = full
    boxes, or rounds = exact number of rounds (split into full boxes + a remainder). equip: move the (first) item to
    its slot (weapons become active; outfits/helmets are worn). remove: take up to count items of section from the
    actor instead (count 0 = all). Returns the item ids. Money / health: game_actor."""
    inst = get(instance)
    return with_error_hint(inst, await run(give, inst, section, int(count), rounds, equip, remove))


@tool
async def game_actor(heal: bool = False, health: float | None = None, stamina: float | None = None,
                     radiation: float | None = None, satiety: float | None = None, psy_health: float | None = None,
                     bleeding: float | None = None, money: int | None = None,
                     instance: str | None = None) -> dict:
    """Set actor stats (0..1): health, stamina, radiation, satiety, psy_health; bleeding 0 stops bleeding; money =
    exact amount (RU). heal = health/stamina/psy 1, radiation 0, bleeding stopped (explicit values win). Returns before
    and after. Other toggles are console cvars (console_exec): "g_god on" (god mode), "g_unlimitedammo on";
    faction: game_teleport(faction=...)."""
    inst = get(instance)
    return with_error_hint(inst, await run(lambda: actor_set(inst, heal=heal, health=health, stamina=stamina,
                                                             radiation=radiation, satiety=satiety,
                                                             psy_health=psy_health, bleeding=bleeding, money=money)))


@tool
async def game_kill(ids: list[int], by_actor: bool = False, instance: str | None = None) -> dict:
    """Kill creatures by id through the engine's kill path (death callbacks, corpses, squad bookkeeping run as usual).
    A squad id kills its online members. by_actor: the actor is the killer (counts as the actor's kill: goodwill,
    stats); default the victim itself. Offline (alife-only) creatures cannot be killed; release them instead."""
    inst = get(instance)
    return with_error_hint(inst, {"instance": inst.id, "results": await run(
        sc_exec, inst, SC_KILL, {"ids": [int(i) for i in ids], "by_actor": by_actor})})


@tool
async def game_release(ids: list[int] | None = None, spawned: bool = False, instance: str | None = None) -> dict:
    """Remove objects from the game (alife_release: squads with their members, NPCs out of their squads, items also
    from inventories). ids: any object ids; spawned: everything game_spawn created in this game process that still
    exists (ids are checked against the spawned section, so reused ids are skipped)."""
    inst = get(instance)
    return with_error_hint(inst, await run(release, inst, ids, spawned))


@tool
async def game_weather(weather: str | None = None, time: str | None = None, advance_hours: float | None = None,
                       instance: str | None = None) -> dict:
    """Weather and time of day. No arguments -> current state plus the valid weathers: cycles {cycle: [presets]}
    (e.g. clear, cloudy, rainy, stormy, foggy) and other weather files. weather: a cycle (random preset of it) or a
    preset, applied at once through level_weathers (holds for a full weather period); another weather file is set
    directly (level_weathers replaces it at the next game hour). time: 'HH:MM' or hour, reached by advancing the game
    clock (never backwards); advance_hours: advance further. Returns before/after (weather, cycle, preset, time, day).
    Surges/psi-storms play weather effects that override the weather while they run. Time factor: not here."""
    inst = get(instance)
    return with_error_hint(inst, await run(weather_set, inst, weather, time, advance_hours))


@tool
async def game_emission(kind: str = "surge", action: str = "status", instance: str | None = None) -> dict:
    """Emission (kind=surge) or psi-storm (kind=psi_storm) through Anomaly's surge_manager / psi_storm_manager (the
    debug tools' calls, so the scripted effects, sounds, hits and their Lua load are the real thing). action: start,
    stop (ends it at once), status. Start may be skipped by the manager (underground level, disabled in options,
    level exceptions); notes say why. Returns before/after: started, elapsed_s, duration_s (surge), weather_fx,
    time_factor. game_state.world.surge / psi_storm report the same started flag."""
    inst = get(instance)
    if kind not in ("surge", "psi_storm"):
        raise ValueError("kind must be surge or psi_storm")
    if action not in ("start", "stop", "status"):
        raise ValueError("action must be start, stop or status")
    return with_error_hint(inst, {"instance": inst.id,
                                  **await run(sc_exec, inst, SC_EMISSION, {"kind": kind, "action": action})})


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
