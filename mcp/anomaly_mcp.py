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
import struct
import time
import winreg
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
            "warnings": [w for w in [wer_warning(inst.exe_name)] if w] or None,
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
    return {"instance": inst.id, "crashed": True, **c, "stack_omitted": stack_note,
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
    return await run(teleport, get(instance), faction, level, position, cvars, timeout)


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
