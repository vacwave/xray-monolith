"""MCP supervisor for X-Ray Monolith (Stalker Anomaly): launches and drives game instances.

Agent <-> MCP stdio (this process) <-> TCP JSON lines <-> engine (-mcp_port). stdout belongs to MCP; logs go to stderr.
"""
import ctypes
import ctypes.wintypes as wt
import difflib
import fnmatch
import functools
import inspect
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
DEFAULT_GAME_ROOT = Path(r"C:\AnomalyMO2")  # testers' MO2: game C:\Anomaly + their mods; C:\Anomaly = plain vanilla
GAMMA_ROOT = Path(r"C:\GAMMA")
STATE_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "anomaly-mcp"
SLOT_DIR = STATE_DIR / "slots"

RING_SIZE = 20000  # log lines kept per instance
MAX_OUTPUT_CHARS = 20000  # per tool result, lines part
MAX_LINE_CHARS = 2000
QUICK_EXIT_S = 15  # an exit within this time after launch is reported as a failed/blocked launch
PORT_WAIT_S = 120
MO2_CHILD_WAIT_S = 60  # MO2 `run` -> game child process (~3 s seen)
STOP_WAIT_S = 30
HEARTBEAT_S = 2  # hang monitor poll interval
WER_WAIT_S = 15  # after a crash-like exit, how long to wait for the WER dump to appear
WER_KEY = r"SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps"
# MiniDumpWithThreadInfo|IndirectlyReferencedMemory|DataSegs|HandleData|UnloadedModules|ProcessThreadData|FullMemoryInfo
MID_DUMP_FLAGS = 0x1000 | 0x40 | 0x1 | 0x4 | 0x20 | 0x100 | 0x800

DEFAULTS = {"max_vanilla": 3, "max_gamma": 1, "hang_timeout_s": 30, "load_hang_timeout_s": 600,
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
k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]


class _ProcEntry(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                ("szExeFile", wt.WCHAR * 260)]


k32.Process32FirstW.argtypes = k32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(_ProcEntry)]


class _StartupInfoEx(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR), ("lpTitle", wt.LPWSTR),
                ("dwX", wt.DWORD), ("dwY", wt.DWORD), ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD),
                ("dwXCountChars", wt.DWORD), ("dwYCountChars", wt.DWORD), ("dwFillAttribute", wt.DWORD),
                ("dwFlags", wt.DWORD), ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD), ("lpReserved2", wt.LPVOID),
                ("hStdInput", wt.HANDLE), ("hStdOutput", wt.HANDLE), ("hStdError", wt.HANDLE),
                ("lpAttributeList", wt.LPVOID)]


class _ProcInfo(ctypes.Structure):
    _fields_ = [("hProcess", wt.HANDLE), ("hThread", wt.HANDLE), ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]


k32.InitializeProcThreadAttributeList.argtypes = [wt.LPVOID, wt.DWORD, wt.DWORD, ctypes.POINTER(ctypes.c_size_t)]
k32.UpdateProcThreadAttribute.argtypes = [wt.LPVOID, wt.DWORD, ctypes.c_size_t, wt.LPVOID, ctypes.c_size_t, wt.LPVOID,
                                          wt.LPVOID]
k32.DeleteProcThreadAttributeList.argtypes = [wt.LPVOID]
k32.CreateProcessW.argtypes = [wt.LPCWSTR, wt.LPWSTR, wt.LPVOID, wt.LPVOID, wt.BOOL, wt.DWORD, wt.LPVOID, wt.LPCWSTR,
                               ctypes.POINTER(_StartupInfoEx), ctypes.POINTER(_ProcInfo)]


def spawn_outside_job(cmdline, cwd):
    """Start a process as a child of the user's explorer.exe, so it takes explorer's job (none), not ours: MO2 starts
    the game with CREATE_BREAKAWAY_FROM_JOB, which fails with error 5 inside a job that forbids breakaway (e.g. the
    MCP Python SDK's stdio client puts its server in one). Returns a ProcHandle."""
    hp = None
    for pid, _, name in processes():
        if name.lower() == "explorer.exe":
            hp = k32.OpenProcess(0x0080, False, pid)  # PROCESS_CREATE_PROCESS (fails for other users' sessions)
            if hp:
                break
    if not hp:
        raise OSError("no explorer.exe of this user to start MO2 under")
    try:
        size = ctypes.c_size_t()
        k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        attrs = ctypes.create_string_buffer(size.value)
        parent = wt.HANDLE(hp)
        if not (k32.InitializeProcThreadAttributeList(attrs, 1, 0, ctypes.byref(size)) and
                k32.UpdateProcThreadAttribute(attrs, 0, 0x00020000, ctypes.byref(parent), ctypes.sizeof(parent),
                                              None, None)):  # PROC_THREAD_ATTRIBUTE_PARENT_PROCESS
            raise OSError(f"proc thread attributes failed (error {ctypes.get_last_error()})")
        si = _StartupInfoEx()
        si.cb = ctypes.sizeof(si)
        si.lpAttributeList = ctypes.cast(attrs, wt.LPVOID)
        pi = _ProcInfo()
        ok = k32.CreateProcessW(None, ctypes.create_unicode_buffer(cmdline), None, None, False,
                                0x00080000, None, cwd, ctypes.byref(si), ctypes.byref(pi))  # EXTENDED_STARTUPINFO_PRESENT
        err = ctypes.get_last_error()
        k32.DeleteProcThreadAttributeList(attrs)
        if not ok:
            raise OSError(f"CreateProcess failed (error {err}): {cmdline}")
        k32.CloseHandle(pi.hThread)
        return ProcHandle(pi.dwProcessId, pi.hProcess)
    finally:
        k32.CloseHandle(hp)


def processes():
    """[(pid, parent pid, exe name)] of all running processes."""
    snap = k32.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    e = _ProcEntry()
    e.dwSize = ctypes.sizeof(e)
    out = []
    ok = k32.Process32FirstW(snap, ctypes.byref(e))
    while ok:
        out.append((e.th32ProcessID, e.th32ParentProcessID, e.szExeFile))
        ok = k32.Process32NextW(snap, ctypes.byref(e))
    k32.CloseHandle(snap)
    return out


class ProcHandle:
    """Process handle by pid (works for launched and attached games)."""

    def __init__(self, pid, h=None):
        self.pid = pid
        self.h = h or k32.OpenProcess(SYNCHRONIZE | PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
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


_jobs = []  # handles kept open until this process exits


def kill_with_supervisor(pid):
    """Games we launch die with this process, so a dead supervisor never leaks a running game past its slot.
    One job per game: an MO2-started game is already in MO2's job, and a job nested under one job cannot take another's."""
    job = k32.CreateJobObjectW(None, None)
    _jobs.append(job)
    info = _JobExtLimits()
    info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))  # ExtendedLimitInformation
    h = k32.OpenProcess(0x0100 | 0x0001, False, pid)  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
    if not h or not k32.AssignProcessToJobObject(job, h):
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
        self.wake = threading.Condition()  # notified on every log line / game event / disconnect (game_wait)
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
                    continue
                with self.wake:
                    self.wake.notify_all()
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
            with self.wake:
                self.wake.notify_all()
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


def seed_appdata(appdata, game_root, keep_shader_cache=False):
    appdata.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(TEMPLATE_USER_LTX, appdata / "user.ltx")
    (appdata / "axr_options.ltx").unlink(missing_ok=True)  # each run starts from the game's own options
    src = game_root / "appdata" / "shaders_cache"
    dst = appdata / "shaders_cache"
    if not keep_shader_cache:  # default: the game rebuilds it, so stale shaders never survive a launch
        shutil.rmtree(dst, ignore_errors=True)
    elif src.is_dir() and not dst.exists():
        shutil.copytree(src, dst)


def copy_missing_dlls(game_root, exe_dir):
    copied = []
    for dll in (game_root / "bin").glob("*.dll"):
        if not (exe_dir / dll.name).exists():
            shutil.copy2(dll, exe_dir / dll.name)
            copied.append(dll.name)
    return copied


def mo2_setup(root):
    """MO2 folder (ModOrganizer.exe, no fsgame.ltx) -> (ModOrganizer.exe, profile, game path, MO2's own DX11-AVX
    executable, (active mods, total mods)) from its ini and the profile's modlist.txt, else None."""
    if (root / "fsgame.ltx").is_file() or not (root / "ModOrganizer.exe").is_file():
        return None
    ini = (root / "ModOrganizer.ini").read_text(encoding="utf-8", errors="replace")

    def val(key):
        m = re.search(rf"^{key}=(?:@ByteArray\()?(.*?)\)?\s*$", ini, re.M)
        if not m:
            raise ValueError(f"no {key} in {root / 'ModOrganizer.ini'}")
        return m.group(1).replace("\\\\", "\\")
    profile, game_path = val("selected_profile"), Path(val("gamePath"))
    bins = [Path(b.replace("\\\\", "\\")) for b in re.findall(r"^\d+\\binary=(.*?)\s*$", ini, re.M)]
    exe = next((b for b in bins if b.name.lower() == DEFAULT_EXE.name.lower()), game_path / "bin" / DEFAULT_EXE.name)
    mods = [l for l in (root / "profiles" / profile / "modlist.txt").read_text(encoding="utf-8", errors="replace")
            .splitlines() if l[:1] in "+-" and l[1:] and not l.endswith("_separator")]
    return root / "ModOrganizer.exe", profile, game_path, exe, (sum(l[0] == "+" for l in mods), len(mods))


def mo2_run(mo2, args):
    """Start the game through our own MO2 (usvfs overlay), return the game's pid, then kill that MO2."""
    mo2_exe, profile = mo2[:2]
    others = [pid for pid, _, name in processes() if name.lower() == mo2_exe.name.lower()]
    if others:
        raise RuntimeError(f"ModOrganizer.exe is already running (pid {', '.join(map(str, others))}); close MO2 first "
                           "(the supervisor starts and stops its own MO2 per launch)")
    mo_log = mo2_exe.parent / "logs" / "mo_interface.log"  # rewritten by every MO2 start
    t0 = time.time()
    p = spawn_outside_job(subprocess.list2cmdline([str(mo2_exe), "-p", profile, "run", args[0], "-a",
                                                   subprocess.list2cmdline(args[1:])]), str(mo2_exe.parent))
    exe_name = Path(args[0]).name.lower()
    try:
        while True:
            pid = next((pid for pid, ppid, name in processes() if ppid == p.pid and name.lower() == exe_name), None)
            if pid:
                return pid
            errors = []
            try:
                if mo_log.stat().st_mtime > t0:
                    errors = [l for l in mo_log.read_text(encoding="utf-8", errors="replace").splitlines() if " E] " in l]
            except OSError:
                pass
            if any(re.search(r" E\] (Error \d+|Failed to run)", l) for l in errors):  # MO2 shows a modal error box
                raise RuntimeError(f"MO2 could not start {args[0]}:\n" + "\n".join(errors))
            if p.wait(0):
                raise RuntimeError(f"ModOrganizer.exe exited with code {p.exit_code()} without starting {exe_name}")
            if time.time() - t0 > MO2_CHILD_WAIT_S:
                raise TimeoutError(f"MO2 started no {exe_name} within {MO2_CHILD_WAIT_S} s; check the exe path and "
                                   f"permissions (MO2 log: {mo_log})")
            time.sleep(0.25)
    finally:  # also closes an MO2 error dialog; the game keeps running without MO2 (usvfs lives in the game process)
        if not p.wait(0):
            p.kill()
            p.wait(10)
        k32.CloseHandle(p.h)


# MCP runs keep their own options (engine: axr_options.ltx saved into -appdata); GAMMA's zzz_player_injuries
# applies master_volume to snd_volume_eff on every level load, so the option itself is set to 0
MUTE_LUA = r'''
local c = axr_main.config
c:w_value("options", "sound/general/master_volume", 0)
c:w_value("options", "sound/general/music_volume", 0)
c.ini:save_as(getFS():update_path("$app_data_root$", "axr_options.ltx"))
'''


def info_or_error(inst, timeout=5):
    try:
        return inst.call("engine.info", timeout=timeout)
    except Exception as e:
        return {"error": str(e)}


def launch(exe=None, game_root=None, show_window=False, new_game=False, extra_args=None, timeout=180,
           full_dumps=False, hang_timeout=None, load_timeout=None, slot_index=None, load=None,
           keep_shader_cache=False, debug=True, cheats=True, god=True, infinite_mag=True, norecoil=True):
    params ={k: v for k, v in locals().items() if k != "slot_index"}
    game_root = Path(game_root) if game_root else DEFAULT_GAME_ROOT
    mo2 = mo2_setup(game_root)
    exe = Path(exe) if exe else mo2[3] if mo2 else DEFAULT_EXE  # MO2: the DX11-AVX exe from its executables list
    if not exe.is_file():
        raise FileNotFoundError(f"exe not found: {exe}")
    fs_root = mo2[2] if mo2 else game_root
    if not (fs_root / "fsgame.ltx").is_file():
        raise FileNotFoundError(f"no fsgame.ltx in {fs_root}")
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
        copied = copy_missing_dlls(fs_root, exe.parent)
        seed_appdata(appdata, fs_root, keep_shader_cache)
        if load:  # the save must already be in this slot's appdata (game_saves import_from=...)
            if " " in load or not (appdata / "savedgames" / (check_load_name(load) + SAVE_EXT)).is_file():
                raise FileNotFoundError(f"no save {load!r} (without spaces) in {appdata / 'savedgames'}")
        port_file = appdata / "mcp_port"
        port_file.unlink(missing_ok=True)
        args = [str(exe), "-fsltx", str(fs_root / "fsgame.ltx"), "-appdata", str(appdata), "-mcp_port", "0"]
        if show_window:
            args.append("-showwindow")
        if debug:
            args.append("-dbg")
        if new_game:
            args += ["-start", "server(all/single/alife/new)", "client(localhost)"]
        elif load:
            args += ["-start", f"server({load}/single/alife/load)", "client(localhost)"]
        if extra_args:
            args += extra_args.split() if isinstance(extra_args, str) else list(extra_args)
        if mo2:
            pid = mo2_run(mo2, args)
        else:
            pid = subprocess.Popen(args, cwd=str(game_root), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, close_fds=True).pid
        kill_with_supervisor(pid)
        inst = Instance(iid, appdata, slot, profile)
        inst.launch_params, inst.exe_name = params, exe.name
        if hang_timeout:
            inst.hang_timeout = hang_timeout
        if load_timeout:
            inst.load_timeout = load_timeout
        inst.watch(ProcHandle(pid))
    except Exception:
        slot.release()
        raise
    with reg_lock:
        instances[iid] = inst  # replaces a dead instance that held this slot before
        current[0] = iid
    log.info("[%s] launched pid %d%s: %s", iid, pid, " via MO2" if mo2 else "", " ".join(args))

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
            raise TimeoutError(f"no mcp_port file after {PORT_WAIT_S} s; killed pid {pid}; {dump}" +
                               (f"\nlog tail ({tail[0]}):\n" + "\n".join(tail[1]) if tail else " (no log)"))
        time.sleep(0.25)
    inst.connect(port)
    info = info_or_error(inst, timeout=timeout)
    try:
        inst.call("lua.exec", {"code": MUTE_LUA, "quiet": 1}, timeout=timeout)
    except Exception as e:
        warnings.append(f"mute via options failed: {e}")
    # process-wide actor flags: set once, they hold across loads, new games and level changes
    cheat_state = {n: bool(cheats and on) for n, on in
                   (("g_god", god), ("g_infinite_mag", infinite_mag), ("g_norecoil", norecoil))}
    for n, on in cheat_state.items():
        try:
            inst.call("console.exec", {"cmd": f"{n} {'on' if on else 'off'}"}, timeout=timeout)
        except Exception as e:
            warnings.append(f"{n} failed: {e}")
    return {"instance": iid, "pid": pid, "port": port, "profile": profile, "appdata": str(appdata),
            "game_root": str(game_root), "exe": str(exe),
            "mo2": {"profile": mo2[1], "game_path": str(fs_root), "mods_active": mo2[4][0],
                    "mods_total": mo2[4][1]} if mo2 else None,
            "args": args[1:], "copied_dlls": copied, "full_dumps": full_dumps, "cheats": cheat_state, "engine_info": info,
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
    "Drive a real Stalker Anomaly / GAMMA game (X-Ray Monolith engine) and test it like a player. The game runs "
    "hidden and muted; you see it only through these tools. Every tool takes an optional `instance` (default: the "
    "last launched or attached). Tool descriptions are short on purpose: full arguments and result fields per "
    "tool are in mcp/TOOLS.md, the how-to guide in mcp/TESTER_GUIDE.md (both in the repo).\n"
    "Flow: game_launch -> game_teleport (new game on a map) or game_load (a save) -> look -> act -> verify with "
    "game_wait and state -> game_stop. Always stop the games you launched.\n"
    "- Start / stop: game_launch (default C:\\AnomalyMO2 = Anomaly with the tester's mods; game_root C:\\Anomaly = "
    "plain vanilla, C:\\GAMMA = GAMMA; god mode, infinite magazine and no recoil are ON unless you turn them off), "
    "game_attach (a game already running with -mcp_port), game_list, game_status, game_restart, game_stop.\n"
    "- Look: game_state first (cheap snapshot: mode, actor, hands, crosshair target, world), game_screenshot (see "
    "the frame), game_screenshot_burst (cutscenes / animations to files), game_objects (what is around), "
    "game_object (one object in detail), game_ui (widgets on screen), game_nav / game_nav_map (where can I walk).\n"
    "- Move: game_move_to (walk to a point or object; opens doors, shoots hostiles on the way), game_changelevel "
    "(go through a level changer), game_teleport (new game, another level, or an exact position).\n"
    "- Interact: game_use (talk to / take / open an object by id), game_talk (read and choose dialogue options), "
    "game_ui_click (click a widget by its text), game_input (keys, mouse, looking, drag: anything else a player "
    "does), game_input_release (let go of held keys).\n"
    "- Fight: game_shoot (shoot one target), game_hunt (go and kill a creature or a quest's target, also on "
    "another level), game_clear_area (kill every hostile in an area), game_guard (idle self-defence, ON by "
    "default: turn it off when the test must not shoot).\n"
    "- Quests: game_tasks (tasks, stages, targets), game_infos (story flags: read, give, remove), game_events "
    "(what happened: tasks, deaths, hits, combat, relations, saves, level loads, script errors).\n"
    "- Wait: game_wait for an event, state, object, log line or Lua condition. Never sleep or poll in a loop.\n"
    "- Set up a scenario: game_spawnable (what can be spawned), game_spawn, game_give (items to the actor), "
    "game_actor (health, money, stats), game_kill, game_release (remove objects), game_weather, game_emission, "
    "game_time (pause, speed, jump the clock), game_step (advance a paused game frame by frame), game_save / "
    "game_load / game_saves.\n"
    "- Diagnose: log_tail (engine log, regex filter), game_events(types=['script_error']), game_perf (frame "
    "times), game_lua_profile (which script costs time). Crash: game_last_crash, then game_restart. Hang "
    "(game_status hung): game_dump, then game_stop.\n"
    "- Last resort: lua_exec / console_exec when no tool does the job; say so in your report (a tool gap). A Lua "
    "error inside a game callback crashes the game."))


def run(fn, *args):
    return anyio.to_thread.run_sync(lambda: fn(*args))


def slim_schema(node):
    """Shrink a tool's JSON schema in place (the agent loads every schema): no titles, no null variants."""
    if isinstance(node, list):
        for v in node:
            slim_schema(v)
    if not isinstance(node, dict):
        return
    if isinstance(node.get("title"), str):
        del node["title"]
    if "default" in node and node["default"] is None:
        del node["default"]
    kinds = [v for v in node.get("anyOf", []) if v != {"type": "null"}]
    if len(kinds) == 1 and len(node["anyOf"]) == 2:
        del node["anyOf"]
        node.update(kinds[0])
    for v in node.values():
        slim_schema(v)


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
    fn = server.tool()(wrapper)
    t = server._tool_manager.get_tool(f.__name__)
    t.description = inspect.cleandoc(t.description or "")
    slim_schema(t.parameters)
    return fn


@tool
async def game_launch(exe: str | None = None, game_root: str | None = None, show_window: bool = False,
                      new_game: bool = False, extra_args: str | None = None, timeout: float = 180,
                      full_dumps: bool = False, hang_timeout: float | None = None,
                      load_timeout: float | None = None, load: str | None = None,
                      keep_shader_cache: bool = False, debug: bool = True, cheats: bool = True, god: bool = True,
                      infinite_mag: bool = True, norecoil: bool = True) -> dict:
    """Start a game (hidden, muted) and connect to it.
    game_root: default C:/AnomalyMO2 (Anomaly + the tester's MO2 mods); C:/Anomaly = plain vanilla; C:/GAMMA =
    GAMMA. An MO2 root is refused while any MO2 is open. new_game: straight into a new game; load: straight into a
    save of this slot. Cheats are on by default: god, infinite_mag, norecoil (cheats=False = all off). exe: another
    engine exe. show_window: visible window. Refused at the instance cap."""
    t0 = time.time()
    res = await run(launch, exe, game_root, show_window, new_game, extra_args, timeout, full_dumps, hang_timeout,
                    load_timeout, None, load, keep_shader_cache, debug, cheats, god, infinite_mag, norecoil)
    if load:
        res["load"] = await run(load_wait, instances[res["instance"]], load, 0, 0, t0 + (load_timeout or 600), t0)
    return res


@tool
async def game_restart(instance: str | None = None, new_game: bool | None = None) -> dict:
    """Stop the instance (kill it if hung) and relaunch it with the same exe, game root and args. new_game overrides
    the original choice."""
    return await run(restart, get(instance), new_game)


@tool
async def game_last_crash(instance: str | None = None, lines: int = 30) -> dict:
    """Why it crashed: reason, engine stack, dump paths, log tail. crashed=false if it has not crashed."""
    return await run(last_crash, get(instance), max(1, min(lines, 200)))


@tool
async def game_dump(instance: str | None = None) -> dict:
    """Write a minidump of the running game (all thread stacks). Use it on a hung instance before game_stop."""
    inst = get(instance)
    path = await run(inst.write_dump)
    with inst.crash_lock:
        deleted, inst.dumps_deleted = inst.dumps_deleted, []
    return {"instance": inst.id, "dump": str(path), "size_mb": round(path.stat().st_size / 2 ** 20, 1),
            "dumps_deleted_by_retention": deleted or None}


@tool
async def game_attach(port: int | None = None, appdata: str | None = None) -> dict:
    """Attach to a game already running with -mcp_port, by TCP port or by its -appdata dir. Attached games are not
    killed when the supervisor exits."""
    return await run(attach, port, appdata)


@tool
async def game_status(instance: str | None = None, lines: int = 20) -> dict:
    """Is the game alive: pid, crashed, hung, engine info, log rate and the last log lines."""
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
    """Quit the game (killed after 30 s) and free its slot. The log buffer stays readable."""
    return await run(stop, get(instance))


@tool
async def console_exec(cmd: str, instance: str | None = None, timeout: float = 30) -> dict:
    """Run an engine console command; returns the log lines it printed."""
    inst = get(instance)
    return with_error_hint(inst, exec_result(await run(inst.call, "console.exec", {"cmd": cmd}, timeout)))


@tool
async def lua_exec(code: str, instance: str | None = None, timeout: float = 30) -> dict:
    """Run Lua on the game's main thread (last resort when no tool fits). An expression list returns its values as
    strings (e.g. 'level.name()'); statements run as a chunk; print() output comes back in lines. Errors return the
    traceback."""
    inst = get(instance)
    return with_error_hint(inst, exec_result(await run(inst.call, "lua.exec", {"code": code}, timeout)))


@tool
async def log_tail(instance: str | None = None, n: int = 100, pattern: str | None = None, since: int | None = None,
                   ignore_case: bool = False, grep: str | None = None) -> dict:
    """Engine log lines. Default: the newest n (max 1000). pattern: regex filter (ignore_case) -> matches. since: only
    lines with seq > since, oldest first (continue with next_since). To see what an action printed: take last_seq
    before it, pass it as since after."""
    inst = get(instance)
    n = max(1, min(n, 1000))
    pattern = pattern or grep
    rx = re.compile(pattern, re.I if ignore_case else 0) if pattern else None
    items = inst.lines(since=since)
    lost = 0
    if since is not None:
        first = items[0][0] if items else inst.last_seq + 1
        if first > since + 1 and inst.sock and not inst.dead:
            try:  # older than the buffer holds: the engine ring may still have them
                res = await run(inst.call, "log.tail", {"since": since}, 10)
                have = {e[0] for e in items}
                items = sorted(items + [(l["seq"], l["line"], 0) for l in res["lines"] if l["seq"] not in have])
            except Exception as e:
                log.info("[%s] log_tail engine fill failed: %s", inst.id, e)
            first = items[0][0] if items else inst.last_seq + 1
        lost = max(0, first - since - 1)
    if rx:
        items = [e for e in items if rx.search(e[1])]
    matched = len(items)
    items = items[:n] if since is not None else items[-n:]
    out = {"instance": inst.id, "alive": not inst.dead, "last_seq": inst.last_seq, "matched": matched,
           "truncated": matched - len(items)}
    if since is not None:
        out["next_since"] = items[-1][0] if out["truncated"] and items else max(inst.last_seq, since)
        if lost:
            out["lost"] = lost
    if not rx:
        lines, note = bound([l for _, l, _ in items], keep="head" if since is not None else "tail")
        out.update(first_seq=items[0][0] if items else None, lines=lines, omitted=note)
        return out
    kept, size = [], 0
    order = items if since is not None else reversed(items)  # keep the oldest after since, else the newest
    for seq, line, _ in order:
        if len(line) > MAX_LINE_CHARS:
            line = line[:MAX_LINE_CHARS] + f" …[+{len(line) - MAX_LINE_CHARS} chars]"
        if size + len(line) + 20 > MAX_OUTPUT_CHARS:
            break
        kept.append({"seq": seq, "line": line})
        size += len(line) + 20
    if since is None:
        kept.reverse()
    out["matches"] = kept
    if len(kept) < len(items):
        out["truncated"] += len(items) - len(kept)
        out["omitted"] = f"{len(items) - len(kept)} matches over the output cap ({MAX_OUTPUT_CHARS} chars)"
        if since is not None:
            out["next_since"] = kept[-1]["seq"] if kept else since
    return out


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
    """What happened in the game, as structured events, oldest first.
    types (names or globs like 'task_*'): script_error, level_loaded, level_unloaded, actor_spawned, actor_died,
    game_saved, game_loaded, menu_opened, menu_closed, task_given, task_completed, task_failed, task_in_progress,
    task_stage, info_given, info_removed, death, hit, combat, relation, guard_fight. since: only seq > since (page
    with the returned last_seq); without it the newest `limit` (max 500). Repeating script errors are merged
    (count)."""
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
if NOSNAP then -- exact point; on the AI map = a node below/above it within 2 m
  local v = level.vertex_id(p)
  local dy = v < bad and p.y - level.vertex_position(v).y
  db.actor:set_actor_position(p)
  return "exact", fmt(p), tostring(dy and math.abs(dy) <= 2 or false), dy and string.format("%.2f", dy) or ""
end
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


def teleport(inst, faction, level, position, cvars, timeout, snap=True):
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
    notes, load_s, fac_set, exact = [], 0.0, None, None
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
        placed = tp_exec(inst, ("" if snap else "local NOSNAP = true ") + TP_PLACE, pos=position)
        if placed[0] == "error":
            raise RuntimeError(placed[1])
        if placed[0] == "exact":
            exact = {"on_ai_map": placed[2] == "true", "node_dy": float(placed[3]) if placed[3] else None}
            if not exact["on_ai_map"]:
                notes.append("snap=False: the point is off the AI map (may be inside geometry or fall)")
        elif placed[2] != "0":
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
            "total_s": round(time.time() - t0, 1), "cvars": applied or None, "notes": notes or None,
            **({"exact": exact} if exact else {})}


@tool
async def game_teleport(instance: str | None = None, faction: str | None = None, level: str | None = None,
                        position: list[float] | None = None, cvars: list[str] | None = None,
                        snap: bool = True, timeout: float = 600) -> dict:
    """Get a playable session on a map; returns once the actor is in control.
    From the main menu: new game as faction (default stalker) on level (default: the faction's start). In a game:
    level = change to that level, position [x,y,z] = move the actor there (snapped to the walkable map; snap=False =
    the exact point), faction = only relabels the actor. cvars: console commands applied after arrival. Unknown
    level / faction -> error listing the valid ones."""
    inst = get(instance)
    return with_error_hint(inst, await run(paused_around, inst, teleport, inst, faction, level, position, cvars, timeout,
                                                   snap))


# ---------------------------------------------------------------- game_changelevel
# Same packet as walking into the changer and confirming its dialog (CChangeLevelWnd::OnOk -> M_CHANGE_LEVEL with the
# changer's dest gvid/lvid/position/angles), sent via _g ChangeLevel. Anomaly has no script-side changer locks (the
# xr_effects enable/disable_level_changer are commented out), so "locked" = the engine flag is_level_changer_enabled().

CL_INFO = r"""
local sim, gg, out, apos = alife(), game_graph(), {}, db.actor:position()
for _, id in ipairs(@IDS@) do
  local o, se = level.object_by_id(id), sim:object(id)
  if o and se and se.dest_game_vertex_id then
    local gv, p = se.dest_game_vertex_id, se.dest_position
    local lvl = gg:valid_vertex_id(gv) and sim:level_name(gg:vertex(gv):level_id()) or ""
    local ok, d = pcall(utils_stpk.get_level_changer_data, se) -- the only reader of the dest angles
    local dir = ok and d and d.dest_direction or VEC_ZERO
    out[#out + 1] = table.concat({id, o:name(), lvl, gv, se.dest_level_vertex_id,
      string.format("%.2f,%.2f,%.2f", p.x, p.y, p.z), string.format("%.4f,%.4f,%.4f", dir.x, dir.y, dir.z),
      tostring(o:is_level_changer_enabled()), string.format("%.1f", o:position():distance_to(apos)),
      tostring(se.hint or "")}, "|")
  end
end
return unpack(out)
"""

CL_GO = r"""
exec_console_cmd("main_menu off")
local d = @DEST@
ChangeLevel(vector():set(d[1], d[2], d[3]), d[7], d[8], vector():set(d[4], d[5], d[6]))
return "ok"
"""

CL_WHERE = r"""
local p = db.actor:position()
return level.name(), string.format("%.2f,%.2f,%.2f", p.x, p.y, p.z)
"""


def cl_changers(inst):
    ids = [o["id"] for o in nav_level_changers(inst) if o.get("id") is not None]
    code = CL_INFO.replace("@IDS@", "{%s}" % ",".join(str(int(i)) for i in ids))
    out = []
    for line in inst.call("lua.exec", {"code": code}, timeout=30)["results"]:
        id, name, lvl, gv, lv, pos, dir, en, dist, hint = line.split("|")
        out.append({"id": int(id), "name": name, "dest_level": lvl, "dest_game_vertex": int(float(gv)),
                    "dest_level_vertex": int(float(lv)), "dest_position": [float(c) for c in pos.split(",")],
                    "dest_direction": [float(c) for c in dir.split(",")], "enabled": en == "true",
                    "distance": float(dist), "hint": hint or None})
    return out


def changelevel(inst, changer, override, timeout):
    t0 = time.time()
    if inst.call("lua.exec", {"code": TP_PROBE}, timeout=10)["results"][:1] in ([], [""]):
        raise RuntimeError("not in a game (main menu or loading); use game_teleport / game_load first")
    lcs = cl_changers(inst)
    key = str(changer).strip()
    hit = ([c for c in lcs if str(c["id"]) == key] or [c for c in lcs if c["name"] == key] or
           sorted([c for c in lcs if c["dest_level"] == key], key=lambda c: (not c["enabled"], c["distance"])))
    brief = [{k: c[k] for k in ("id", "name", "dest_level", "enabled", "distance")} for c in lcs]
    if not hit:
        return {"instance": inst.id, "error": "NoChanger", "reason": f"no level changer matches {key!r}",
                "changers": brief}
    c, notes = hit[0], []
    if len(hit) > 1:
        notes.append(f"{len(hit)} changers lead to {key}; used the nearest enabled one ({c['name']}, {c['distance']} m)")
    used = {k: c[k] for k in ("id", "name", "dest_level", "dest_game_vertex", "dest_position", "enabled", "hint")}
    if not c["dest_level"]:
        raise RuntimeError(f"changer {c['name']} has an invalid destination game vertex {c['dest_game_vertex']}")
    if not c["enabled"]:
        if not override:
            return {"instance": inst.id, "error": "LevelLocked", "changer": used,
                    "reason": "level changer disabled (engine flag, enable_level_changer(false))" +
                              (f"; hint {c['hint']}" if c["hint"] not in (None, "level_changer_invitation") else "")}
        notes.append("changer is locked; override=True changed level anyway")
    from_level = inst.call("lua.exec", {"code": CL_WHERE}, timeout=10)["results"][0]
    dest = "{%s}" % ",".join(repr(float(v)) for v in
                             (*c["dest_position"], *c["dest_direction"], c["dest_level_vertex"], c["dest_game_vertex"]))
    inst.call("lua.exec", {"code": CL_GO.replace("@DEST@", dest)}, timeout=30)
    t = time.time()
    tp_wait(inst, c["dest_level"], t0 + timeout)
    load_s = time.time() - t
    lvl, pos = inst.call("lua.exec", {"code": CL_WHERE}, timeout=10)["results"][:2]
    return {"instance": inst.id, "level": lvl, "position": [float(v) for v in pos.split(",")], "from_level": from_level,
            "changer": used, "load_s": round(load_s, 1), "total_s": round(time.time() - t0, 1),
            "notes": notes or None}


@tool
async def game_changelevel(instance: str | None = None, changer: str | int = "", override: bool = False,
                           timeout: float = 600) -> dict:
    """Go to an adjacent level through its level changer, as if the actor walked into it (no need to be near it).
    changer: object id, name, or the destination level name. No match -> error NoChanger with the list of changers;
    a disabled changer -> error LevelLocked (override=True goes anyway). Returns once the actor is in control on the
    new level."""
    inst = get(instance)
    return with_error_hint(inst, await run(paused_around, inst, changelevel, inst, changer, override, timeout))


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
if game.has_active_tutorial() and db.actor and not db.actor:alive() then game.stop_tutorial() return "tutorial" end
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
    if how[0] == "tutorial":
        raise RuntimeError(f"load of {name} not started: the death screen tutorial did not stop")
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
    """Save the game as `name` in the instance's own savedgames (overwrites); returns once it is on disk. The UI save
    restrictions (campfire-only saves etc.) do not apply."""
    inst = get(instance)
    return with_error_hint(inst, await run(game_save_run, inst, name, timeout))


@tool
async def game_load(name: str, instance: str | None = None, timeout: float = 600) -> dict:
    """Load save `name` (see game_saves) from the main menu or a running game; returns once the actor is in control. If
    the game dies during the load: loaded=false with the crash record (and missing_sections when the save needs mods
    this game lacks)."""
    inst = get(instance)
    return with_error_hint(inst, await run(paused_around, inst, game_load_run, inst, name, timeout))


@tool
async def game_saves(instance: str | None = None, delete: str | None = None, import_from: str | None = None,
                     import_as: str | None = None, details: bool = True) -> dict:
    """List the instance's saves, newest first (with details: level, game time, health). import_from: copy a .scop save
    file with its parts into the instance, optionally renamed with import_as (the source is not modified). delete:
    remove one of the instance's saves."""
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
    """Where am I and what is the game doing: one cheap snapshot. Call it first.
    mode: main_menu, loading, in_game, in_game_menu, dialog, dead, paused, tutorial, console, hung. ui: the open
    dialog (inventory / trade / pda / talk / ...), cursor. actor: position, yaw / pitch, health and other stats,
    money, community, cheats. hands: weapon, ammo, state. target: what is under the crosshair. world: level, game
    time, weather, surge. sections: add 'inventory', 'tasks', 'stats' or 'all'."""
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
    """Find things around the actor (or `position`), nearest first.
    radius: metres. kinds: stalker, monster, corpse, item, weapon, ammo, artefact, anomaly, campfire, container,
    door, physic, vehicle, ladder, level_changer, smart_terrain, restrictor, cover, squad; aliases npc, loot,
    creature, all. pattern: glob on section / object name / NPC name. alive, hostile_only, visible_only (in the
    camera view), los (add a line-of-sight check), offline (also far alife entities and squads), summary=True (only
    counts per kind), limit (30, max 100).
    Per object: id, kind, section, name, position, distance, bearing; creatures add health, community, relation,
    in_combat, story_id; doors add their state; containers and corpses their loot. target = what the crosshair hits."""
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
    """Everything about one object id: the game_objects fields plus line of sight, inventory, money, active item,
    current enemy, mental state, script logic (scheme, active section), anomaly power. An offline object returns its
    alife entity; unknown id -> object null."""
    inst = get(instance)
    return await run(world_call, inst, "world.object", {"id": int(id)})


TASK_LONG = ("description", "def")  # dropped from the compact (default) game_tasks output


def tasks_call(inst, ids, active_only, history, full):
    exact = len(ids) == 1 and not any(c in ids[0] for c in "*?[")
    p = {"history": int(bool(history) and not active_only)}
    if exact:
        p["id"] = ids[0]
    res = world_call(inst, "tasks.get", p)
    if not res.get("available"):
        return res

    def keep(t):
        return not ids or any(fnmatch.fnmatchcase(t.get("id", ""), i) for i in ids)
    for k in ("tasks", "history"):
        if k in res:
            res[k] = [t for t in res[k] if keep(t)]
            if not full:
                for t in res[k]:
                    for f in TASK_LONG:
                        t.pop(f, None)
    if ids and "history" in res and not exact:
        res.pop("history_omitted", None)  # counts the unfiltered registry
    if not full:
        res["compact"] = "description/def omitted: full=True or id to include them"
    return res


@tool
async def game_tasks(instance: str | None = None, id: str | list[str] | None = None, active_only: bool = False,
                     history: bool = True, full: bool | None = None) -> dict:
    """Quest state as the PDA knows it: tasks in progress (id, title, stage, status, target with position / distance,
    giver) and history (completed / failed, newest first).
    id: a task id, a list, or a glob (e.g. 'esc_*'). active_only: no history. full: add the description and the task
    definition (default when id is given). To wait for a change use game_wait on task_stage / task_completed events."""
    inst = get(instance)
    ids = [i.strip() for i in (id.split(",") if isinstance(id, str) else id or []) if i and i.strip()]
    return await run(tasks_call, inst, ids, active_only, history, bool(ids) if full is None else full)


INFOS_MAX = 500


def infos_call(inst, match, has, give, remove):
    p = {}
    if give:
        p["give"] = ",".join(give)
    if remove:
        p["remove"] = ",".join(remove)
    res = world_call(inst, "infos.get", p)
    if not res.get("available"):
        return res
    names = res.pop("infos")
    known = set(names)
    if has:
        res["has"] = {n: n in known for n in has}
    if match or not has:
        sel = sorted(n for n in names if not match or fnmatch.fnmatch(n.lower(), match.lower()))
        res["matched"] = len(sel)
        res["infos"] = sel[:INFOS_MAX]
        if len(sel) > INFOS_MAX:
            res["omitted"] = f"{len(sel) - INFOS_MAX} more; narrow with match"
    return res


@tool
async def game_infos(instance: str | None = None, match: str | None = None, has: list[str] | None = None,
                     give: list[str] | None = None, remove: list[str] | None = None) -> dict:
    """The actor's info portions (story flags). No arguments: the full list. has: names -> {name: bool}. match: glob
    over the known names. give / remove: add / drop names first (each change fires an info_given / info_removed
    event)."""
    inst = get(instance)
    return await run(infos_call, inst, match, has, give, remove)


# ---------------------------------------------------------------- game_nav / game_nav_map (AI map queries, overlay, top-down image)

import base64  # noqa: E402
import math  # noqa: E402
import zlib  # noqa: E402

NAV_QUERIES = ("info", "at", "around", "path", "nearest", "overlay")


def nav_vec(v, name):
    if v is None:
        return None
    if len(v) != 3:
        raise ValueError(f"{name} must be [x, y, z]")
    return ",".join(str(float(c)) for c in v)


def nav_level_changers(inst):
    """Level changers on this level (world.objects), for naming the exits of a route."""
    res = world_call(inst, "world.objects", {"radius": "100000", "kinds": "level_changer", "limit": 100})
    return res.get("objects", []) if res.get("available") else []


def nav_query(inst, query, p):
    method = {"info": "nav.info", "at": "nav.at", "around": "nav.around", "path": "nav.path",
              "nearest": "nav.nearest", "overlay": "nav.overlay"}[query]
    res = world_call(inst, method, p)
    if not res.get("available", True):
        return res
    if query == "info" and "exits" in res:
        ch = nav_level_changers(inst)
        res["level_changers"] = [{k: o.get(k) for k in ("id", "name", "destination", "position", "distance", "bearing")}
                                 for o in ch]
    return res


@tool
async def game_nav(query: str = "at", instance: str | None = None, position: list[float] | None = None,
                   to: list[float] | None = None, to_level: str | None = None, radius: float | None = None,
                   rays: int = 16, grid: bool = True, grid_cell: float = 2, grid_radius: float | None = None,
                   orient: str = "view", max_nodes: int | None = None, max_waypoints: int = 64,
                   show: bool = False, random_radius: float | None = None, count: int = 1, on: bool | None = None,
                   path: list[list[float]] | None = None, markers: list[list[float]] | None = None,
                   clear: bool = False) -> dict:
    """Where can the actor walk: queries on the level's walkable map (AI map) and the level-to-level graph. Origin =
    `position` or the actor.
    query: 'at' (is this point walkable; nearest walkable node), 'around' (walkable space within radius: ray
    distances and an ASCII grid), 'path' (route to `to`: found, length, waypoints, reason when not found; with
    to_level: the route across levels and the level changer to take), 'nearest' (nearest walkable point;
    random_radius + count = random reachable points), 'info' (map size, exits to other levels, level changers),
    'overlay' (draw nodes / path / markers in the game view for screenshots: on, radius, path, markers, clear).
    show=True draws a found path."""
    inst = get(instance)
    if query not in NAV_QUERIES:
        raise ValueError(f"query must be one of {NAV_QUERIES}")
    if orient not in ("view", "north"):
        raise ValueError('orient must be "view" or "north"')
    p = {}
    if query == "path":
        if position is not None:
            p["from"] = nav_vec(position, "position")
        if to is None and not to_level:
            raise ValueError("path needs to [x, y, z] and/or to_level")
        if to is not None:
            p["to"] = nav_vec(to, "to")
        if to_level:
            p["to_level"] = to_level
        if max_nodes:
            p["max_nodes"] = int(max_nodes)
        p["max_waypoints"] = int(max_waypoints)
        if show:
            p["overlay"] = 1
    elif position is not None:
        p["position"] = nav_vec(position, "position")
    if radius is not None:
        p["radius"] = str(float(radius))
    if query == "around":
        p.update({"rays": int(rays), "grid": int(grid), "grid_cell": str(float(grid_cell)), "orient": orient})
        if grid_radius is not None:
            p["grid_radius"] = str(float(grid_radius))
    if query == "nearest" and random_radius:
        p.update({"random_radius": str(float(random_radius)), "count": int(count)})
    if query == "overlay":
        if on is not None:
            p["on"] = int(on)
        if clear:
            p["clear"] = 1
        for key, pts in (("path", path), ("markers", markers)):
            if pts is not None:
                p[key] = ";".join(nav_vec(v, key) for v in pts)
    res = await run(nav_query, inst, query, p)
    if query == "path" and show and res.get("found"):
        await run(world_call, inst, "nav.overlay", {"on": 1})
        res["overlay"] = True
    return res


NAV_KIND_COLORS = {"stalker": (60, 200, 255), "monster": (255, 50, 50), "corpse": (150, 150, 150),
                   "level_changer": (255, 0, 255), "container": (170, 110, 60), "anomaly": (255, 140, 0),
                   "campfire": (255, 200, 80), "weapon": (255, 255, 120), "ammo": (255, 255, 120),
                   "artefact": (255, 255, 120), "item": (255, 255, 120)}


def nav_png(w, h, rgb):
    """Minimal 8-bit RGB PNG (stdlib zlib)."""
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    stride = w * 3
    raw = b"".join(b"\x00" + bytes(rgb[y * stride:(y + 1) * stride]) for y in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def nav_map(inst, position, radius, cell, orient, path_to, objects, max_px):
    p = {"radius": str(float(radius)), "orient": orient, "max_px": int(max_px)}
    if position is not None:
        p["position"] = nav_vec(position, "position")
    if cell:
        p["cell"] = str(float(cell))
    g = world_call(inst, "nav.grid", p)
    if not g.get("available"):
        return [TextContent(type="text", text=json.dumps(g))]
    n, c, yaw = g["width"], g["cell"], math.radians(g["yaw"])
    ox, oy, oz = g["origin"]
    data = base64.b64decode(g.pop("data"))
    s = max(1, 640 // n)
    W = n * s
    img = bytearray(W * W * 3)
    for r in range(n):
        for col in range(n):
            v = data[r * n + col]
            if not v:
                rgb = (28, 28, 32)
            else:
                f = min(1.6, max(0.45, 1 + ((v & 0x7f) - 64) * 0.04))
                base = (50, 165, 70) if v & 0x80 else (190, 105, 45)
                rgb = tuple(min(255, int(x * f)) for x in base)
            px = bytes(rgb) * s
            for dy in range(s):
                o = ((r * s + dy) * W + col * s) * 3
                img[o:o + 3 * s] = px

    def to_px(pt):
        dx, dz = pt[0] - ox, pt[2] - oz
        right = dx * math.cos(yaw) - dz * math.sin(yaw)
        fwd = dx * math.sin(yaw) + dz * math.cos(yaw)
        return ((right / c + (n - 1) / 2 + 0.5) * s, ((n - 1) / 2 - fwd / c + 0.5) * s)

    def dot(x, y, rgb, rad):
        for yy in range(int(y - rad), int(y + rad) + 1):
            for xx in range(int(x - rad), int(x + rad) + 1):
                if 0 <= xx < W and 0 <= yy < W and (xx - x) ** 2 + (yy - y) ** 2 <= rad * rad:
                    o = (yy * W + xx) * 3
                    img[o:o + 3] = bytes(rgb)

    def line(a, b, rgb, rad=1.0):
        steps = int(max(abs(b[0] - a[0]), abs(b[1] - a[1]))) + 1
        for i in range(steps + 1):
            t = i / steps
            dot(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, rgb, rad)

    meta = {k: v for k, v in g.items() if k != "encoding"}
    meta["scale_px_per_cell"] = s
    meta["image"] = {"width": W, "height": W, "metres_per_px": round(c / s, 3),
                     "up": "north (+z)" if orient == "north" else "camera forward",
                     "legend": "green = walkable and reachable from the origin (brighter = higher), orange = walkable "
                               "but not linked, dark = no AI node; white arrow = actor/camera, yellow = path, "
                               "dots: cyan stalker, red monster, grey corpse, magenta level changer, yellow loot, "
                               "orange anomaly, brown container"}
    if objects:
        ob = world_call(inst, "world.objects", {"radius": str(float(radius) * 1.42), "limit": 100,
                                                "position": ",".join(str(v) for v in g["origin"])})
        shown = 0
        for o in ob.get("objects", []):
            x, y = to_px(o["position"])
            if 0 <= x < W and 0 <= y < W:
                dot(x, y, NAV_KIND_COLORS.get(o["kind"], (230, 230, 230)), max(2.0, s * 0.6))
                shown += 1
        meta["objects_drawn"] = shown
    if path_to is not None:
        pp = {"to": nav_vec(path_to, "path_to"), "max_waypoints": 500}
        if position is not None:
            pp["from"] = nav_vec(position, "position")
        pr = world_call(inst, "nav.path", pp)
        meta["path"] = {k: pr.get(k) for k in ("found", "length", "straight", "nodes", "reason", "search_ms")}
        if pr.get("found"):
            pts = [pr["from"]] + [w["position"] for w in pr["waypoints"]]
            for a, b in zip(pts, pts[1:]):
                line(to_px(a), to_px(b), (255, 230, 0), max(1.0, s * 0.35))
        dot(*to_px(path_to), (255, 230, 0), max(3.0, s * 0.9))
    # actor / camera arrow at the origin (or the actor's projected position)
    cx, cy = to_px(g["origin"])
    head = math.radians(g["camera_yaw"]) - yaw  # 0 = up in the image
    L = max(8.0, s * 3)
    tip = (cx + math.sin(head) * L, cy - math.cos(head) * L)
    left = (cx + math.sin(head - 2.5) * L * 0.6, cy - math.cos(head - 2.5) * L * 0.6)
    right = (cx + math.sin(head + 2.5) * L * 0.6, cy - math.cos(head + 2.5) * L * 0.6)
    for a, b in ((tip, left), (tip, right), (left, right), ((cx, cy), tip)):
        line(a, b, (255, 255, 255), 1.0)
    png = nav_png(W, W, img)
    meta["bytes"] = len(png)
    return [TextContent(type="text", text=json.dumps({"instance": inst.id, **meta})),
            ImageContent(type="image", data=base64.b64encode(png).decode(), mime_type="image/png")]


@tool
async def game_nav_map(instance: str | None = None, position: list[float] | None = None, radius: float = 40,
                       cell: float | None = None, orient: str = "view", path_to: list[float] | None = None,
                       objects: bool = True, max_px: int = 400) -> list[TextContent | ImageContent]:
    """Top-down PNG of the walkable map around the actor (or `position`): green = reachable, orange = walkable but not
    linked, dark = not walkable; the actor as an arrow, nearby objects as dots, and with path_to [x,y,z] the route
    in yellow. radius: metres (40). orient: 'view' (camera forward is up) or 'north'."""
    inst = get(instance)
    if orient not in ("view", "north"):
        raise ValueError('orient must be "view" or "north"')
    return await run(nav_map, inst, position, radius, cell, orient, path_to, objects, max_px)


def nav_move(inst, p, wait, wait_timeout, method="nav.move_to", status_method="nav.move_status"):
    res = world_call(inst, method, p)
    if res.get("available") is False or res.get("done") or not wait:
        return res
    deadline = time.monotonic() + wait_timeout
    while time.monotonic() < deadline:
        time.sleep(0.1)
        try:
            res = inst.call(status_method, {}, timeout=5)
        except TimeoutError:
            continue  # level load: the mover aborts itself (level_change)
        if res.get("done"):
            return {"instance": inst.id, **res}
    tool = "game_shoot" if status_method == "nav.shoot_status" else "game_move_to"
    return {"instance": inst.id, **res, "note": f"still running after {wait_timeout} s of wall time (paused or "
                                                 f"stepping?); poll {tool}(status=True) or stop=True"}


@tool
async def game_move_to(instance: str | None = None, position: list[float] | None = None, id: int | None = None,
                       stop_distance: float | None = None, timeout: float = 120, sprint: bool = True,
                       engage: bool | str = True, wait: bool = True, wait_timeout: float = 300, status: bool = False,
                       stop: bool = False) -> dict:
    """Walk the actor to `position` [x,y,z] or to object `id` (followed while it moves) like a player: no teleport,
    sprints by default, opens doors on the way, goes around obstacles.
    engage (default True): stops to shoot visible hostiles within 50 m, then walks on; False = never shoots;
    'required' = fail when it cannot fight. stop_distance: metres (1; objects 1.5). timeout: seconds of walking.
    Result state: reached / stuck (blocker) / timeout / no_path / unreachable (reason) / blocked_door (reason, e.g.
    locked) / aborted (dialog, actor_dead, level_change, ...); plus doors, fights, distance_left, position.
    wait=False returns at once; status=True polls; stop=True aborts."""
    inst = get(instance)
    if status or stop:
        return {"instance": inst.id, **await run(world_call, inst, "nav.move_status", {"stop": int(stop)})}
    if (position is None) == (id is None):
        raise ValueError("give exactly one of position [x, y, z] or id")
    if engage not in (True, False, "required"):
        raise ValueError('engage must be True, False or "required"')
    p = {"timeout_ms": int(timeout * 1000), "sprint": int(sprint), "engage": 2 if engage == "required" else int(engage)}
    if position is not None:
        p["to"] = nav_vec(position, "position")
    else:
        p["id"] = int(id)
    if stop_distance is not None:
        p["stop_distance"] = str(float(stop_distance))
    return await run(nav_move, inst, p, wait, wait_timeout)


# ---------------------------------------------------------------- game_hunt
# The engine hunts an online creature (nav.move_to hunt=1); the supervisor resolves the target (alife), changes level
# through the route's level changer (game_changelevel), walks toward an offline target until it comes online, and
# ends area targets (squad / smart terrain) with arrived_area.

HUNT_OBJ = r"""
local sim, id = alife(), @ID@
if type(id) == "string" then id = get_story_object_id(id) end
local se = id and sim:object(id)
if not se then return "" end
local c, k, p, o = se:clsid(), "other", se.position, level.object_by_id(id)
if IsStalker(nil, c) then k = "stalker" elseif IsMonster(nil, c) then k = "monster"
elseif c == clsid.online_offline_group_s or c == clsid.online_offline_group then
  k = "squad"
  local cm = se.commander_id and sim:object(se:commander_id())
  local co = cm and level.object_by_id(cm.id)
  if co then p = co:position() elseif cm then p = cm.position end
elseif c == clsid.smart_terrain then k = "smart_terrain"
elseif c == clsid.space_restrictor or c == clsid.script_restr then k = "restrictor" end
if o and k ~= "squad" then p = o:position() end
local alive = true
if k == "stalker" or k == "monster" then
  if o then alive = o:alive() else alive = se:alive() end
end
local lvl = sim:level_name(game_graph():vertex(se.m_game_vertex_id):level_id())
return table.concat({id, k, lvl, string.format("%.2f,%.2f,%.2f", p.x, p.y, p.z), tostring(alive), tostring(o ~= nil),
  se:name()}, "|")
"""

HUNT_AREA_KINDS = ("squad", "smart_terrain", "restrictor")


def hunt_obj(inst, ref):
    code = HUNT_OBJ.replace("@ID@", str(int(ref)) if isinstance(ref, int) else json.dumps(str(ref)))
    out = inst.call("lua.exec", {"code": code}, timeout=10)["results"]
    if not out or not out[0]:
        return None
    id, kind, lvl, pos, alive, online, name = out[0].split("|", 6)
    return {"id": int(float(id)), "kind": kind, "level": lvl, "position": [float(v) for v in pos.split(",")],
            "alive": alive == "true", "online": online == "true", "name": name}


def hunt_level(inst):
    return inst.call("lua.exec", {"code": TP_PROBE}, timeout=10)["results"][:1] or [""]


def hunt_run(inst, id, task, story_id, timeout, sprint, engage_range, area_radius, clear=False, clear_radius=50.0):
    t0 = time.monotonic()
    end = t0 + timeout
    out = {"instance": inst.id, "fights": [], "levels": [], "steps": []}

    def left():
        return max(1.0, end - time.monotonic())

    def finish(state, **kw):
        out.update(state=state, **kw)
        out["elapsed_s"] = round(time.monotonic() - t0, 1)
        if not out["fights"]:
            del out["fights"]
        if not out["levels"]:
            del out["levels"]
        return out

    def walk(p):
        p = {"timeout_ms": int(left() * 1000), "sprint": int(sprint), "engage": 1,
             "engage_range": str(float(engage_range)), **p}
        res = nav_move(inst, p, True, left() + 30)
        out["fights"] += res.get("fights", [])
        out["steps"].append({k: res.get(k) for k in ("state", "reason", "travelled", "elapsed_ms", "hunt", "ladder",
                                                     "drew") if res.get(k) is not None})
        return res

    ref = id
    if task is not None:
        tk = world_call(inst, "tasks.get", {"id": task, "history": 0})
        if tk.get("available") is False:
            return finish("aborted", reason=tk.get("reason"))
        cur = next((t for t in tk.get("tasks", []) if t.get("id") == task), None)
        if cur is None:
            return finish("no_target", reason=f"task {task!r} is not in progress")
        out["task"] = {k: cur.get(k) for k in ("id", "title", "stage")}
        if not cur.get("target") or cur["target"].get("exists") is False:
            return finish("no_target", reason="the task has no target marker (target null)")
        ref = int(cur["target"]["id"])
    elif story_id is not None:
        ref = story_id
    last_offline = None
    for _ in range(40):
        if time.monotonic() >= end:
            return finish("timeout")
        tgt = hunt_obj(inst, ref)
        if tgt is None:
            return finish("target_gone" if "target" in out else "no_target", reason="no such alife object")
        out["target"] = tgt
        ref = tgt["id"]
        creature = tgt["kind"] in ("stalker", "monster")
        if creature and not tgt["alive"]:
            return finish("target_dead")
        level = hunt_level(inst)[0]
        if not level:
            return finish("aborted", reason="not in a level")
        if tgt["level"] != level:
            # cross-level: the game-graph route's changer on this level, entered like game_changelevel
            np = world_call(inst, "nav.path", {"to_level": tgt["level"], "to": nav_vec(tgt["position"], "position")})
            lc = np.get("level_changer")
            if not np.get("found") or not lc:
                return finish("unreachable", reason=f"no level route / changer from {level} to {tgt['level']}")
            cl = paused_around(inst, changelevel, inst, lc["id"], False, left())
            if cl.get("error"):
                return finish("unreachable", reason=f"{cl['error']}: {cl.get('reason')}", changer=cl.get("changer"))
            out["levels"].append({"from": level, "to": cl.get("level"), "changer": lc.get("name"),
                                  "load_s": cl.get("load_s")})
            continue
        if not creature:
            res = walk({"to": nav_vec(tgt["position"], "position"),
                        "stop_distance": str(float(area_radius if tgt["kind"] in HUNT_AREA_KINDS else 1.5))})
            st, why = res.get("state"), res.get("reason")
            near = res.get("distance_left") is not None and res["distance_left"] <= area_radius
            if st == "reached" or (st == "unreachable" and why == "target_off_mesh" and near):
                if tgt["kind"] not in HUNT_AREA_KINDS:
                    return finish("arrived", position=res.get("position"))
                if clear:
                    ids = squad_members(inst, tgt["id"]) if tgt["kind"] == "squad" else None
                    st, why = clear_loop(inst, out, tgt["position"], clear_radius, ids, end, sprint, engage_range)
                    return finish(st, reason=why)
                ho = world_call(inst, "world.objects", {"position": nav_vec(tgt["position"], "position"),
                                                        "radius": str(float(max(area_radius * 3, 50))),
                                                        "kinds": "stalker,monster", "hostile_only": 1, "los": 1,
                                                        "limit": 30})
                hs = [{k: o.get(k) for k in ("id", "name", "section", "kind", "distance", "los", "position")}
                      for o in ho.get("objects", [])]
                return finish("arrived_area", position=res.get("position"), hostiles=hs,
                              hostiles_visible=[h for h in hs if h.get("los")])
            if st == "aborted" and why == "level_change":
                continue
            return finish(st, reason=why, ladder=res.get("ladder"))
        if tgt["online"]:
            res = walk({"id": tgt["id"], "hunt": 1})
            st = res.get("state")
            if st == "target_gone" or (st == "aborted" and res.get("reason") == "level_change"):
                continue  # went offline / released / we changed level: resolve again
            return finish(st, reason=res.get("reason"), ladder=res.get("ladder"), hunt=res.get("hunt"),
                          health=res.get("health"))
        # offline (beyond the online radius): walk toward its server position until it comes online
        if last_offline and math.dist(last_offline, tgt["position"]) < 1 and out["steps"] and \
                out["steps"][-1].get("state") == "reached":
            return finish("target_offline", reason="reached its last known position; it is still offline")
        last_offline = tgt["position"]
        r = world_call(inst, "nav.move_to", {"to": nav_vec(tgt["position"], "position"), "stop_distance": "3",
                                             "timeout_ms": int(left() * 1000), "sprint": int(sprint), "engage": 1,
                                             "engage_range": str(float(engage_range))})
        while not r.get("done") and time.monotonic() < end:
            time.sleep(0.5)
            try:
                r = inst.call("nav.move_status", {}, timeout=5)
            except TimeoutError:
                continue
            now = hunt_obj(inst, ref)
            if now is None or now["online"] or math.dist(now["position"], tgt["position"]) > 15:
                r = inst.call("nav.move_status", {"stop": 1}, timeout=5)
                break
        out["fights"] += r.get("fights", [])
        out["steps"].append({k: r.get(k) for k in ("state", "reason", "travelled", "elapsed_ms") if r.get(k) is not None})
        if r.get("state") in ("no_path", "unreachable", "stuck", "engage_failed", "blocked_door") or \
                (r.get("state") == "aborted" and r.get("reason") not in ("stopped", "level_change")):
            return finish("unreachable" if r.get("state") == "no_path" else r.get("state"), reason=r.get("reason"),
                          ladder=r.get("ladder"))
    return finish("timeout", reason="too many retargets")


@tool
async def game_hunt(instance: str | None = None, id: int | None = None, task: str | None = None,
                    story_id: str | None = None, timeout: float = 600, sprint: bool = True,
                    engage_range: float = 100, area_radius: float = 10, clear: bool | None = None,
                    clear_radius: float = 50) -> dict:
    """Go and kill creature `id`, or the target of quest `task` (task id) or of a `story_id`, on foot: walks to it
    (through level changers when it is on another level), shoots visible hostiles within engage_range on the way,
    ends when it dies.
    Area targets (squad, smart terrain): with clear (default for task) the hostiles there are killed like
    game_clear_area, else it stops on arrival. Items / stashes: stops on arrival. A firearm is drawn if the hands
    are empty. Ladders cannot be walked (unreachable).
    Result state: target_dead / cleared / arrived_area / arrived / unreachable / target_offline / target_gone /
    timeout / engage_failed (no weapon or ammo) / aborted / stuck / no_target; plus fights, levels, steps. timeout:
    wall seconds."""
    if sum(x is not None for x in (id, task, story_id)) != 1:
        raise ValueError("give exactly one of id, task or story_id")
    inst = get(instance)
    return await run(hunt_run, inst, None if id is None else int(id), task, story_id, float(timeout), sprint,
                     float(engage_range), max(1.0, float(area_radius)), task is not None if clear is None else clear,
                     max(5.0, float(clear_radius)))


# ---------------------------------------------------------------- game_clear_area
# Kill the hostiles of an area (or listed creatures) one at a time: next = visible (los) first, then nearest; each one
# is hunted with hunt_run (engine hunt mode fights any visible hostile meanwhile, the hunted one preferred).

SQUAD_MEMBERS = r"""
local se, t = alife():object(@ID@), {}
if se and se.squad_members then for k in se:squad_members() do t[#t + 1] = k.id end end
return table.concat(t, ",")
"""

ACTOR_POS = r"""
local a = db.actor
if not (level.present() and a) then return "" end
local p = a:position()
return string.format("%.2f,%.2f,%.2f", p.x, p.y, p.z)
"""


def squad_members(inst, id):
    out = inst.call("lua.exec", {"code": SQUAD_MEMBERS.replace("@ID@", str(int(id)))}, timeout=10)["results"]
    return [int(float(v)) for v in (out[0] if out else "").split(",") if v]


def actor_pos(inst):
    out = inst.call("lua.exec", {"code": ACTOR_POS}, timeout=10)["results"]
    return [float(v) for v in out[0].split(",")] if out and out[0] else None


def clear_targets(inst, center, radius, ids):
    """Living targets left, next first (los, then nearest; other levels last); None outside a level."""
    me, level = actor_pos(inst), hunt_level(inst)[0]
    if me is None or not level:
        return None
    if ids is None:
        r = world_call(inst, "world.objects", {"position": nav_vec(center, "position"), "radius": str(float(radius)),
                                               "kinds": "stalker,monster", "hostile_only": 1, "los": 1, "limit": 100})
        if r.get("available") is False:
            return None
        ts = [{k: o.get(k) for k in ("id", "name", "section", "position", "los")} for o in r.get("objects", [])]
    else:
        r = world_call(inst, "world.objects", {"radius": "150", "kinds": "stalker,monster", "alive": 1, "los": 1,
                                               "limit": 100})
        los = {o["id"]: o.get("los") for o in r.get("objects", [])}
        ts = []
        for i in ids:
            t = hunt_obj(inst, i)
            if t and t["kind"] in ("stalker", "monster") and t["alive"]:
                ts.append({"id": i, "name": t["name"], "position": t["position"], "los": los.get(i),
                           "level": t["level"], "online": t["online"]})
    ts.sort(key=lambda t: (t.get("level", level) != level, not t.get("los"), math.dist(me, t["position"])))
    return ts


def clear_loop(inst, out, center, radius, ids, end, sprint, engage_range):
    """Hunt targets until none is left; fills out kills / remaining / hunts (+ fights, levels); returns (state, reason)."""
    out.setdefault("fights", [])
    out.setdefault("levels", [])
    hunts, bad, dead = [], {}, set()
    out["clear"] = {"position": center, "radius": radius} if ids is None else {"ids": ids}

    def done(state, reason=None, ts=()):
        out["hunts"] = hunts
        out["kills"] = len(dead | {f["id"] for f in out["fights"] if f.get("killed")})
        out["remaining"] = [{**t, "reason": bad[t["id"]]} if t["id"] in bad else t for t in ts]
        return state, reason

    ts = []
    for _ in range(100):
        ts = clear_targets(inst, center, radius, ids)
        if ts is None:
            return done("aborted", "not in a level")
        if not ts:
            return done("cleared")
        todo = [t for t in ts if t["id"] not in bad]
        if not todo:
            return done("unreachable", "no remaining target can be reached", ts)
        if time.monotonic() >= end:
            return done("timeout", None, ts)
        t = todo[0]
        res = hunt_run(inst, t["id"], None, None, max(1.0, end - time.monotonic()), sprint, engage_range, 10)
        out["fights"] += res.get("fights", [])
        out["levels"] += res.get("levels", [])
        st, why = res.get("state"), res.get("reason")
        hunts.append({k: v for k, v in (("id", t["id"]), ("name", t.get("name")), ("state", st), ("reason", why),
                                        ("drew", next((s["drew"] for s in res.get("steps", []) if s.get("drew")), None)),
                                        ("elapsed_s", res.get("elapsed_s"))) if v is not None})
        if st == "target_dead":
            dead.add(t["id"])
        elif st in ("unreachable", "stuck", "blocked_door", "target_offline", "no_target"):
            bad[t["id"]] = f"{st}: {why}" if why else st
        elif st != "target_gone":  # timeout, engage_failed, aborted
            return done(st, why, clear_targets(inst, center, radius, ids) or ts)
    return done("timeout", "too many targets", ts)


def clear_run(inst, position, radius, ref, ids, timeout, sprint, engage_range):
    if ref is not None:
        tgt = hunt_obj(inst, ref)
        if tgt is None:
            return {"instance": inst.id, "state": "no_target", "reason": "no such alife object"}
        if tgt["kind"] not in ("stalker", "monster"):
            # area object (smart terrain / squad / restrictor), maybe on another level: game_hunt travels there first
            return hunt_run(inst, tgt["id"], None, None, timeout, sprint, engage_range, 10, True, radius)
        ids = [tgt["id"]]
    t0 = time.monotonic()
    end = t0 + timeout
    out = {"instance": inst.id, "fights": [], "levels": [], "steps": []}
    me = actor_pos(inst)
    if me is None:
        return {"instance": inst.id, "state": "aborted", "reason": "not in a level"}
    if position is not None and math.dist(me, position) + radius > 120:
        # beyond the online radius the area's creatures have no client objects: walk up to it first
        res = nav_move(inst, {"to": nav_vec(position, "position"), "stop_distance": str(float(radius)),
                              "timeout_ms": int(timeout * 1000), "sprint": int(sprint), "engage": 1,
                              "engage_range": str(float(engage_range))}, True, timeout + 30)
        out["fights"] += res.get("fights", [])
        out["steps"].append({k: res.get(k) for k in ("state", "reason", "travelled", "elapsed_ms", "drew")
                             if res.get(k) is not None})
        near = res.get("distance_left") is not None and res["distance_left"] <= radius
        if res.get("state") != "reached" and not near:
            out.update(state=res.get("state"), reason=res.get("reason"), elapsed_s=round(time.monotonic() - t0, 1))
            return out
    st, why = clear_loop(inst, out, position, radius, ids, end, sprint, engage_range)
    out.update(state=st, elapsed_s=round(time.monotonic() - t0, 1))
    if why is not None:
        out["reason"] = why
    for k in ("fights", "levels", "steps"):
        if not out[k]:
            del out[k]
    return out


@tool
async def game_clear_area(instance: str | None = None, position: list[float] | None = None, radius: float = 50,
                          id: int | None = None, story_id: str | None = None, ids: list[int] | None = None,
                          engage_range: float = 100, timeout: float = 600, sprint: bool = True) -> dict:
    """Kill every hostile within `radius` of `position`, or around a smart terrain / restrictor `id` / `story_id`, or
    the members of a squad id, or exactly the creatures in `ids` (neutral ones too). Walks and fights like
    game_hunt, also on another level.
    Result state: cleared / unreachable / timeout / engage_failed / aborted / no_target; plus kills, remaining (with
    the reason), hunts, fights."""
    if sum(x is not None for x in (position, id, story_id, ids)) != 1:
        raise ValueError("give exactly one of position, id, story_id or ids")
    if position is not None:
        nav_vec(position, "position")
    inst = get(instance)
    return await run(clear_run, inst, position, max(5.0, float(radius)), story_id if id is None else int(id),
                     [int(i) for i in ids] if ids is not None else None, float(timeout), sprint, float(engage_range))


def game_use_run(inst, id, approach, stand, wait_timeout):
    res = nav_move(inst, {"id": id}, True, wait_timeout, "nav.use")
    why = res.get("reason")
    door = "before" in res.get("use", {})
    # a script may act only closer than the engine's use range (e.g. 1.5 m): walk up and press once more
    if not approach or res.get("available") is False or "center" not in res or not (
            (res.get("state") == "failed" and why in ("out_of_range", "pick_missed"))
            or (res.get("state") == "used" and why == "no_effect" and not door)):
        return {**res, "approached": False}
    c, a = res["center"], res["actor"]
    d = 1.5 if door else stand
    if res["distance_xz"] <= d + 0.3 and why != "pick_missed":
        return {**res, "approached": False}
    dx, dz = a[0] - c[0], a[2] - c[2]
    k = d / max(math.hypot(dx, dz), 1e-3)
    y = a[1] if abs(a[1] - c[1]) < 2 else c[1]
    mv = nav_move(inst, {"to": nav_vec([c[0] + dx * k, y, c[2] + dz * k], "stand"), "stop_distance": "0.3",
                         "sprint": 0, "engage": 0, "timeout_ms": 20000}, True, 30)
    second = nav_move(inst, {"id": id}, True, wait_timeout, "nav.use")
    return {**second, "approached": True,
            "approach": {"state": mv.get("state"), "reason": mv.get("reason"), "travelled": mv.get("travelled"),
                         "first": {k: res.get(k) for k in ("state", "reason", "distance", "distance_xz", "pick_range")}}}


@tool
async def game_use(instance: str | None = None, id: int | None = None, approach: bool = True, stand: float = 1.1,
                   wait_timeout: float = 15) -> dict:
    """Use an object like a player: talk to an NPC, take an item, open / close a door, open a box. Aims at it and
    presses use; approach (default True) walks next to it first when needed.
    Result state: used (reason dialog_opened, taken, door_toggled, no_effect, gone) / failed (out_of_range,
    pick_missed + what the crosshair hit instead) / timeout."""
    if id is None:
        raise ValueError("id required (an online object, e.g. from game_objects)")
    inst = get(instance)
    return await run(game_use_run, inst, int(id), approach, max(0.5, float(stand)), wait_timeout)


@tool
async def game_shoot(instance: str | None = None, id: int | None = None, part: str = "chest", shots: int = 1,
                     until_dead: bool = False, burst: int = 3, max_seconds: float = 30, fire: bool = True,
                     ads: bool = False, wait: bool = True, wait_timeout: float | None = None, status: bool = False,
                     stop: bool = False) -> dict:
    """Shoot creature `id` with the weapon in hands: keeps the aim on `part` ('chest', 'head' or a bone name) and fires
    only while the line of fire is clear. Spread, ammo, jams and reloads are the game's, so misses at range are
    normal. It does not walk (use game_move_to or game_hunt).
    shots=N or until_dead=True; burst: rounds per burst on automatic; ads: aim down sights; fire=False: only track
    the target; max_seconds.
    Result state: done / target_dead / target_gone / not_visible / los_blocked (blocker) / out_of_range /
    out_of_ammo / jammed / no_weapon / timeout / aborted; plus shots, hits, target health, ammo. wait=False returns
    at once; status=True polls; stop=True aborts."""
    inst = get(instance)
    if status or stop:
        return {"instance": inst.id, **await run(world_call, inst, "nav.shoot_status", {"stop": int(stop)})}
    if id is None:
        raise ValueError("id required (an online creature, e.g. from game_objects)")
    p = {"id": int(id), "part": str(part), "shots": int(shots), "until_dead": int(until_dead), "burst": int(burst),
         "timeout_ms": int(max_seconds * 1000), "fire": int(fire), "ads": int(ads)}
    return await run(nav_move, inst, p, wait, wait_timeout if wait_timeout is not None else max_seconds + 30,
                     "nav.shoot", "nav.shoot_status")


@tool
async def game_guard(instance: str | None = None, on: bool | None = None) -> dict:
    """Idle guard, ON by default: while no tool drives the actor, no input is sent and no UI is open, the actor shoots
    visible hostiles within 50 m with the weapon in hands. on=False turns it off (do this when the test must not
    shoot), on=True on, no argument = status. Its fights are guard_fight events."""
    inst = get(instance)
    p = {} if on is None else {"on": int(bool(on))}
    return {"instance": inst.id, **await run(world_call, inst, "nav.guard", p)}


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
async def game_input(steps: list, instance: str | None = None, wait: bool = True, timeout: float = 120,
                     image_size: list[int] | None = None) -> dict:
    """Play keyboard / mouse input like a human (works while the game is hidden).
    steps: a list runs in sequence; a list nested in it runs in parallel. One verb per step, ms = duration:
      {"action": "forward", "ms": 2000}  hold a game action; no ms = tap; "state": "down" / "up" = press / release
          only.
      {"key": "ESCAPE"}  a raw key (W, RETURN, F1, SPACE, LSHIFT, LMB, RMB ...), same options.
      {"text": "hello"}  type into an edit box.   {"wait": 500}   {"wheel": 1, "at": [x, y]}
      {"look": {"yaw": 90, "pitch": -10}, "ms": 300}  turn the camera by degrees (+ = right / up).
      {"look_at": [x, y, z]} / {"look_dir": [dx, dy, dz]}  aim the camera at a world point / direction.
      {"mouse": {"dx": 40, "dy": 0}, "ms": 200}  raw mouse move.
      {"cursor": [x, y]} / {"click": [x, y], "button": "right", "double": true, "hover": 400} / {"drag": {"from":
          [x, y], "to": [x, y]}, "ms": 300}  UI cursor; points are pixels of the last game_screenshot (main menu
          clicks need hover ~400).
    Actions: forward back lstrafe rstrafe jump crouch sprint_toggle wpn_fire wpn_zoom wpn_reload use inventory
    active_jobs (PDA) wpn_1..6 quick_use_1..4 torch night_vision show_detector drop quit; aliases fire aim reload
    pda sprint menu. Unknown names fail with the full list.
    wait=False returns at once; steps=[] returns the status (running sequences, held keys)."""
    inst = get(instance)
    return await run(lambda: input_run(inst, ui_expand_steps(inst, steps, image_size), wait, timeout))


@tool
async def game_input_release(instance: str | None = None) -> dict:
    """Release every key held by game_input and cancel running input sequences."""
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
    ui_note_screenshot(inst, res)
    res = {"instance": inst.id, **{k: round(v, 1) if isinstance(v, float) else v for k, v in res.items()}}
    return [TextContent(type="text", text=json.dumps(res)),
            ImageContent(type="image", data=data, mime_type="image/png" if fmt == "png" else "image/jpeg")]


@tool
async def game_screenshot(instance: str | None = None, max_width: int = 1024, format: str = "jpeg", quality: int = 70,
                          save_path: str | None = None, timeout: float = 10) -> list[TextContent | ImageContent]:
    """See what a player sees (3D view + HUD / menus): returns the image. max_width (1024; 0 = full size), format jpeg
    / png, quality. save_path: also write the full-resolution frame to a file. Pixel coordinates of this image are
    the ones game_input click / drag and game_ui use. Fails while a level is loading."""
    inst = get(instance)
    if format not in ("jpeg", "png"):
        raise ValueError('format must be "jpeg" or "png"')
    if not 1 <= quality <= 100 or max_width < 0:
        raise ValueError("quality must be 1..100 and max_width >= 0")
    return await run(screenshot, inst, max_width, format, quality, save_path, timeout)


def burst_wait_log(inst, rx, since, timeout):
    """First buffered log line with seq > since matching rx, waiting on inst.wake; (seq, line, arrival) or None."""
    deadline = time.monotonic() + timeout
    while True:
        with inst.wake:  # the reader notifies under this lock after updating the ring: no missed line
            for e in inst.lines(since=since):
                if rx.search(e[1]):
                    return e
                since = e[0]
            if inst.dead or time.monotonic() >= deadline:
                return None
            inst.wake.wait(min(0.25, max(0.001, deadline - time.monotonic())))


def screenshot_burst(inst, every, duration, max_width, fmt, quality, folder, start_on_log, log_since, ignore_case,
                     start_timeout, inline_every):
    if inst.call("engine.heartbeat", timeout=5).get("loading"):
        raise RuntimeError("a level is loading: the loading screen cannot be captured; retry once game_status shows "
                           "the level")
    p = Path(folder or time.strftime("burst_%Y%m%d_%H%M%S"))
    p = (p if p.is_absolute() else RUN_DIR / inst.id / "screenshots" / p).resolve()
    p.mkdir(parents=True, exist_ok=True)
    out = {"instance": inst.id, "folder": str(p)}
    if start_on_log:
        rx = re.compile(start_on_log, re.I if ignore_case else 0)
        hit = burst_wait_log(inst, rx, inst.last_seq if log_since is None else log_since, start_timeout)
        if hit is None:
            raise TimeoutError(f"no log line matched {start_on_log!r} within {start_timeout} s"
                               + (" (the game exited)" if inst.dead else "") + "; nothing captured")
        out["trigger"] = {"seq": hit[0], "line": hit[1][:MAX_LINE_CHARS],
                          "latency_ms": round((time.time() - hit[2]) * 1000, 1) if hit[2] else None}
    ext = "png" if fmt == "png" else "jpg"
    frames, inline = [], []
    t0 = time.perf_counter()
    i = 0
    while True:
        due = t0 + i * every
        now = time.perf_counter()
        if (now if every == 0 else due) - t0 > duration + 1e-6 or len(frames) >= 1000:
            break
        if due > now:
            time.sleep(due - now)
        tb = time.perf_counter()
        f = {"i": i, "t": round(tb - t0, 3)}
        try:  # one capture at a time: a newer screenshot.begin supersedes a pending one in the engine
            rid = inst.call("screenshot.begin", {"max_width": max_width, "format": fmt, "quality": quality},
                            timeout=5)["id"]
            while True:
                res = inst.call("screenshot.get", {"id": rid}, timeout=5)
                if not res.get("pending"):
                    break
                if time.perf_counter() - tb > 5:
                    raise TimeoutError("no frame within 5 s (loading or stalled)")
                time.sleep(0.005)
            data = base64.b64decode(res["data"])
            path = p / f"{i:03d}_{f['t']:07.3f}s.{ext}"
            path.write_bytes(data)
            f.update(frame=res.get("frame"), path=str(path), ms=round((time.perf_counter() - tb) * 1000, 1))
            if inline_every and i % inline_every == 0:
                inline.append((i, res["data"]))
            out.setdefault("size", [res.get("width"), res.get("height")])
            out.setdefault("level", res.get("level"))
        except Exception as e:
            f["error"] = f"{type(e).__name__}: {e}"
            if inst.dead:
                frames.append(f)
                break
        frames.append(f)
        # next slot on the grid; when a capture overran it, take the next one at once instead of bursting to catch up
        i = max(i + 1, math.floor((time.perf_counter() - t0) / every + 1e-9)) if every > 0 else i + 1
    ok = [f for f in frames if "path" in f]
    gaps = [b["t"] - a["t"] for a, b in zip(ok, ok[1:])]
    out.update(requested_every=every, duration=duration, count=len(ok), errors=len(frames) - len(ok),
               achieved_every=round(sum(gaps) / len(gaps), 3) if gaps else None,
               max_gap=round(max(gaps), 3) if gaps else None,
               capture_ms_avg=round(sum(f["ms"] for f in ok) / len(ok), 1) if ok else None,
               skipped_slots=sum(1 for a, b in zip(frames, frames[1:]) if b["i"] > a["i"] + 1), frames=frames)
    mime = "image/png" if fmt == "png" else "image/jpeg"
    return ([TextContent(type="text", text=json.dumps(out))]
            + [c for i, d in inline for c in (TextContent(type="text", text=f"frame {i}"),
                                               ImageContent(type="image", data=d, mime_type=mime))])


@tool
async def game_screenshot_burst(instance: str | None = None, every: float = 0.5, duration: float = 10,
                                max_width: int = 1024, format: str = "jpeg", quality: int = 70,
                                folder: str | None = None, start_on_log: str | None = None,
                                log_since: int | None = None, ignore_case: bool = False, start_timeout: float = 60,
                                inline_every: int = 0) -> list[TextContent | ImageContent]:
    """Record a series of frames to files (cutscenes, animations): one every `every` s (minimum about 0.1) for
    `duration` s into `folder`. Returns paths and timestamps, not images (inline_every=k also returns every k-th
    image). start_on_log: regex; the capture starts when a matching log line appears (log_since: a log_tail last_seq
    taken earlier; start_timeout s)."""
    inst = get(instance)
    if format not in ("jpeg", "png"):
        raise ValueError('format must be "jpeg" or "png"')
    if not 1 <= quality <= 100 or max_width < 0:
        raise ValueError("quality must be 1..100 and max_width >= 0")
    if every < 0 or duration < 0 or inline_every < 0 or (every == 0 and duration == 0):
        raise ValueError("every, duration and inline_every must be >= 0 (every=0: back to back)")
    if every > 0 and duration / every > 1000 or duration > 600:
        raise ValueError("at most 1000 frames and 600 s per burst")
    return await run(screenshot_burst, inst, every, duration, max_width, format, quality, folder, start_on_log,
                     log_since, ignore_case, start_timeout, inline_every)


# ---------------------------------------------------------------- game_time / game_step
# Engine time.* (xrEngine/mcp_time.cpp): an MCP pause on top of the device pause, fixed-dt step frames, the simulation
# time factor. The game clock rate (in-game time per simulation second) is the level's game time factor (Lua).
# All engine-side time state resets when the supervisor disconnects (the game resumes, time_factor is restored).

# level.get_time_factor / game.time crash (C++ AV, not a Lua error) without a level, so check level.present() first
TIME_CLOCK_LUA = "if not level.present() then return -1, -1 end return game.time(), level.get_time_factor()"
# surge_manager's main_loop resets the level time factor to its local normal_time_factor (alife.ltx) every second
# outside surges/psi storms, so that upvalue is set too (reset by the next Lua state: level change / load).
TIME_RATE_LUA = r'''
if not level.present() then return -1 end
local function up(f, name)
    for i = 1, 255 do
        local n, v = debug.getupvalue(f, i)
        if not n then return end
        if n == name then return i, v end
    end
end
local b, x, sticky = level.get_time_factor(), %f, false
local sm = rawget(_G, "surge_manager")
local _, ml = up(sm and sm.on_game_start or function() end, "main_loop")
local i = ml and up(ml, "normal_time_factor")
if i then debug.setupvalue(ml, i, x) sticky = true end
level.set_time_factor(x)
return b, sticky
'''

# Clock jump like the game's own skips (fast travel, sleep): alife + environment time, surge/psi-storm managers told
# (time_forwarded: no surge right after the jump), weather re-rolled for the new hour.
TIME_ADVANCE_LUA = r'''
if not level.present() then return -1 end
local function clock() return string.format("%%04d-%%02d-%%02d %%02d:%%02d:%%02d", game.get_game_time():get(0, 0, 0, 0, 0,
  0, 0)) end
local mins = %d
local t0, c0 = game.time(), clock()
level.change_game_time(math.floor(mins / 1440), math.floor(mins %% 1440 / 60), mins %% 60)
local notes = {}
for _, m in ipairs({{"surge_manager", "get_surge_manager"}, {"psi_storm_manager", "get_psi_storm_manager"}}) do
  local M = rawget(_G, m[1])
  local ok, o = pcall(function() return M[m[2]]() end)
  if ok and o then o.time_forwarded = true else notes[#notes + 1] = m[1] .. " not told (time_forwarded)" end
end
local ok, e = pcall(function() level_weathers.get_weather_manager():forced_weather_change() end)
if not ok then notes[#notes + 1] = "weather not updated: " .. tostring(e) end
return t0, c0, game.time(), clock(), table.concat(notes, "; ")
'''


def time_clock(inst):
    """Engine time state + game clock: time.get, then game time ms / game time factor via Lua (both main thread)."""
    st = inst.call("time.get", timeout=10)
    gt, f = (float(x) for x in inst.call("lua.exec", {"code": TIME_CLOCK_LUA}, timeout=10)["results"][:2])
    st.update(game_time_ms=None if gt < 0 else int(gt), clock_rate=None if f < 0 else f)
    return st


def time_wait(inst, pred, timeout, what):
    deadline = time.monotonic() + timeout
    while True:
        st = inst.call("time.get", timeout=max(1.0, deadline - time.monotonic()))
        if pred(st):
            return st
        if time.monotonic() > deadline:
            raise TimeoutError(f"{what}: not done after {timeout} s (state {st})")
        time.sleep(0.005)


def time_pause(inst, on, timeout=10):
    """time.pause, then wait until the engine applied it (the next frame)."""
    inst.call("time.pause", {"on": bool(on)}, timeout=10)
    return time_wait(inst, lambda s: s["applied"] == bool(on), timeout, "pause" if on else "resume")


def advance_minutes(v):
    """Minutes from a number (minutes) or a string such as "90", "2h", "2h30m", "1d", "1.5h"."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        n = float(v)
    else:
        t = str(v).strip().lower().replace(" ", "")
        if re.fullmatch(r"\d+(\.\d+)?", t):
            n = float(t)
        else:
            parts = re.findall(r"(\d+(?:\.\d+)?)([dhm])", t)
            if not parts or "".join(a + u for a, u in parts) != t:
                raise ValueError(f"advance: cannot read {v!r} (minutes, or e.g. '2h30m', '1d', '45m')")
            n = sum(float(a) * {"d": 1440, "h": 60, "m": 1}[u] for a, u in parts)
    n = round(n)
    if not 1 <= n <= 30 * 1440:
        raise ValueError("advance must be 1 minute .. 30 days")
    return n


def time_run(inst, pause, sim_speed, clock_rate, advance=None):
    if inst.call("engine.heartbeat", timeout=5).get("loading"):
        raise RuntimeError("a level is loading; retry when it is done")
    out = {}
    if advance is not None:
        n = advance_minutes(advance)
        res = inst.call("lua.exec", {"code": TIME_ADVANCE_LUA % n}, timeout=10)["results"]
        if float(res[0]) < 0:
            raise RuntimeError("advance needs a loaded level")
        out["advance"] = {"minutes": n, "old_game_time": res[1], "new_game_time": res[3],
                          "old_game_time_ms": int(float(res[0])), "new_game_time_ms": int(float(res[2]))}
        if len(res) > 4 and res[4]:
            out["advance"]["notes"] = res[4]
    if sim_speed is not None:
        if not 0.01 <= sim_speed <= 100:
            raise ValueError("sim_speed must be 0.01..100")
        inst.call("time.factor", {"value": float(sim_speed)}, timeout=10)
    if clock_rate is not None:
        if not 0 <= clock_rate <= 10000:
            raise ValueError("clock_rate must be 0..10000")
        res = inst.call("lua.exec", {"code": TIME_RATE_LUA % float(clock_rate)}, timeout=10)["results"]
        if float(res[0]) < 0:
            raise RuntimeError("clock_rate needs a loaded level")
        out["clock_rate_before"] = float(res[0])
        if res[1] != "true":
            out["clock_rate_note"] = "surge_manager's normal_time_factor not found: the game may reset the rate"
    if pause is not None:
        time_pause(inst, pause)
    return {"instance": inst.id, **time_clock(inst), **out}


@tool
async def game_time(instance: str | None = None, pause: bool | None = None, sim_speed: float | None = None,
                    clock_rate: float | None = None, advance: str | float | None = None) -> dict:
    """Pause, speed and clock. No arguments = report.
    pause=True / False: freeze the world (screenshots, state and Lua still work; game_input only advances during
    game_step). sim_speed: simulation speed (0.25 = slow motion, 4 = fast). clock_rate: in-game seconds per real
    second (default 6). advance: jump the game clock forward like sleeping (minutes, or '2h30m' / '1d')."""
    inst = get(instance)
    return await run(time_run, inst, pause, sim_speed, clock_rate, advance)


def paused_around(inst, fn, *args):
    """Run a level load/change with the MCP pause lifted (loads need a running simulation), then pause again."""
    was = inst.call("time.get", timeout=10).get("mcp_paused")
    if was:
        time_pause(inst, False)
    out = fn(*args)
    if was:
        time_pause(inst, True, timeout=30)
        if isinstance(out, dict):
            out["mcp_paused"] = True
    return out


def step_run(inst, frames, ms, dt_ms, steps, screenshot_opts, state, timeout):
    if (frames is None) == (ms is None):
        raise ValueError("give frames or ms")
    if not 1 <= dt_ms <= 100:
        raise ValueError("dt_ms must be 1..100")
    n = int(frames) if frames is not None else max(1, round(ms / dt_ms))
    if not 1 <= n <= 100000:
        raise ValueError("1..100000 frames")
    t0 = time.perf_counter()
    if inst.call("engine.heartbeat", timeout=5).get("loading"):
        raise RuntimeError("a level is loading; retry when it is done")
    if not inst.call("time.get", timeout=10)["applied"]:
        time_pause(inst, True)
    before = time_clock(inst)
    rid = inst.call("input.run", {"steps": steps}, timeout=10)["id"] if steps else None
    sid = inst.call("time.step", {"frames": n, "dt_ms": int(dt_ms)}, timeout=10)["step_id"]
    time_wait(inst, lambda s: s["step_done"] >= sid, timeout or n * 0.5 + 30, f"step of {n} frames")
    after = time_clock(inst)
    out = {"instance": inst.id, "frames": n, "dt_ms": int(dt_ms), "sim_ms": n * int(dt_ms),
           "cancelled": after.get("step_cancelled"),
           "before": {k: before[k] for k in ("frame", "time_global", "game_time_ms")},
           "after": {k: after[k] for k in ("frame", "time_global", "game_time_ms")},
           "time_global_delta": after["time_global"] - before["time_global"],
           "game_time_delta_ms": None if before["game_time_ms"] is None or after["game_time_ms"] is None
           else (after["game_time_ms"] - before["game_time_ms"]) % 2 ** 32}
    if rid is not None:
        out["input"] = inst.call("input.status", {"id": rid}, timeout=10)
    if state:
        out["state"] = game_state_get(inst, [])
    out["wall_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    if not screenshot_opts:
        return [TextContent(type="text", text=json.dumps(out))]
    shot = screenshot(inst, *screenshot_opts)
    out["screenshot"] = json.loads(shot[0].text)
    out["wall_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return [TextContent(type="text", text=json.dumps(out)), shot[1]]


@tool
async def game_step(instance: str | None = None, frames: int | None = None, ms: float | None = None,
                    dt_ms: int = 20, input: list | None = None, screenshot: bool = False, state: bool = True,
                    max_width: int = 1024, format: str = "jpeg", quality: int = 70, save_path: str | None = None,
                    timeout: float | None = None) -> list[TextContent | ImageContent]:
    """Advance a paused game by an exact amount, then stay paused (pauses first if needed); for reproducible,
    frame-exact tests. frames=N or ms of simulation time; each frame is dt_ms (20). input: game_input steps played
    during the step (their ms are simulation ms). screenshot=True adds the frame after the step; state=True adds
    game_state."""
    inst = get(instance)
    if format not in ("jpeg", "png") or not 1 <= quality <= 100 or max_width < 0:
        raise ValueError('format "jpeg"/"png", quality 1..100, max_width >= 0')
    shot = (max_width, format, quality, save_path, 10) if screenshot else None
    return await run(step_run, inst, frames, ms, dt_ms, input, shot, state, timeout)


# ---------------------------------------------------------------- UI tools (game_ui, game_ui_click, cursor steps)
# Coordinates: the engine works in UI space (1024x768 stretched over the whole back buffer); agents use pixels of
# the last game_screenshot image (inst.ui_image), so px = ui * image / (1024, 768).

UI_W, UI_H = 1024.0, 768.0
UI_INTERACTIVE = ("button", "check", "tab", "edit", "track", "combo", "list_item", "cell", "question", "menu_item")
UI_COLOR_RE = re.compile(r"%c\[[^\]]*\]")
UI_MENU_HOVER_MS = 400


def ui_note_screenshot(inst, res):
    """Remember the returned image size: it is the default pixel space of cursor steps and game_ui px rects."""
    w, h = res.get("width"), res.get("height")
    if w and h:
        inst.ui_image = (int(w), int(h))
        res["coords"] = (f"pixels of this {w}x{h} image are the default coordinates of game_input cursor/click/drag "
                         f"steps and game_ui px rects (UI space = x*1024/{w}, y*768/{h})")


def ui_image_size(inst, image_size=None):
    if image_size:
        if len(image_size) != 2 or min(image_size) <= 0:
            raise ValueError("image_size must be [width, height] > 0")
        return float(image_size[0]), float(image_size[1])
    if getattr(inst, "ui_image", None):
        return tuple(float(v) for v in inst.ui_image)
    sw, sh = getattr(inst, "ui_screen", None) or inst.call("ui.tree", {"max_nodes": 1}, timeout=5)["screen"]
    inst.ui_screen = (sw, sh)
    return 1024.0, float(round(1024 * sh / sw)) if sw else 576.0


def ui_point(inst, p, space, image, image_size):
    """[x, y] in px (screenshot pixels) / ui / norm -> UI-space [x, y]."""
    if not isinstance(p, (list, tuple)) or len(p) != 2 or not all(isinstance(v, (int, float)) for v in p):
        raise ValueError(f"expected a point [x, y], got {p!r}")
    space = space or "px"
    if space == "ui":
        return [float(p[0]), float(p[1])]
    if space == "norm":
        return [p[0] * UI_W, p[1] * UI_H]
    if space != "px":
        raise ValueError('"space" must be "px", "ui" or "norm"')
    w, h = ui_image_size(inst, image or image_size)
    return [round(p[0] * UI_W / w, 2), round(p[1] * UI_H / h, 2)]


def ui_px(inst, v, image_size=None):
    """UI-space rect/point list -> pixel space of the current image."""
    w, h = ui_image_size(inst, image_size)
    return [round(c * (w / UI_W if i % 2 == 0 else h / UI_H)) for i, c in enumerate(v)]


UI_BUTTONS = {"left": "LMB", "right": "RMB", "middle": "MMB"}


def ui_click_steps(at, button="left", double=False, ms=None, hover=None):
    key = UI_BUTTONS.get(button or "left")
    if not key:
        raise ValueError('"button" must be left, right or middle')
    out = [{"cursor": at, **({"ms": ms} if ms else {})}] + ([{"wait": hover}] if hover else []) + [{"key": key}]
    if double:
        out += [{"wait": 40}, {"key": key}]
    return out


def ui_expand_steps(inst, steps, image_size, parallel=False):
    """Supervisor-side cursor verbs -> engine steps ({"cursor": [ux, uy]} + mouse keys). A verb that becomes several
    steps is spliced into a sequence, or wrapped in a list (= sequence) inside a parallel list."""
    if not isinstance(steps, list):
        return steps
    out = []
    for s in steps:
        if isinstance(s, list):
            out.append(ui_expand_steps(inst, s, image_size, not parallel))
            continue
        if not isinstance(s, dict) or not any(k in s for k in ("cursor", "click", "drag", "at")):
            out.append(s)
            continue
        s = dict(s)
        space, image = s.pop("space", None), s.pop("image", None)
        pt = lambda p: ui_point(inst, p, space, image, image_size)  # noqa: E731
        if s.get("cursor") == "release":
            seq = [s]
        elif "cursor" in s:
            seq = [{**s, "cursor": pt(s["cursor"])}]
        elif "click" in s:
            extra = set(s) - {"click", "button", "double", "ms", "hover"}
            if extra:
                raise ValueError(f"click: unknown fields {sorted(extra)}")
            seq = ui_click_steps(pt(s["click"]), s.get("button"), s.get("double"), s.get("ms"), s.get("hover"))
        elif "drag" in s:
            d = s["drag"]
            if not isinstance(d, dict) or "from" not in d or "to" not in d:
                raise ValueError('drag: expected {"from": [x, y], "to": [x, y]}')
            key = UI_BUTTONS.get(s.get("button") or "left")
            if not key:
                raise ValueError('"button" must be left, right or middle')
            seq = [{"cursor": pt(d["from"])}, {"key": key, "state": "down"}, {"wait": 60},
                   {"cursor": pt(d["to"]), "ms": s.get("ms", 300)}, {"wait": 60}, {"key": key, "state": "up"}]
        else:  # wheel at a point
            if "wheel" not in s:
                raise ValueError('"at" only goes with "wheel"')
            seq = [{"cursor": pt(s.pop("at"))}, s]
        if len(seq) == 1:
            out.append(seq[0])
        elif parallel:
            out.append(seq)
        else:
            out.extend(seq)
    return out


def ui_text(n):
    t = n.get("text")
    return UI_COLOR_RE.sub("", t).replace("\\n", "\n").strip() if isinstance(t, str) else None


def ui_names(n):
    """What a widget can be found by: shown text, label, inventory item name / section."""
    it = n.get("item") or {}
    return [v for v in (ui_text(n), n.get("label"), it.get("name"), it.get("section")) if v]


def ui_labels(nodes):
    """An interactive widget without text (check box, radio) is labelled by the text window right after it."""
    by_parent = {}
    for n in nodes:
        by_parent.setdefault(n["p"], []).append(n)
    for sib in by_parent.values():
        for a, b in zip(sib, sib[1:]):
            if a["kind"] in UI_INTERACTIVE and not ui_text(a) and b["kind"] in ("text", "static") and ui_text(b):
                a["label"] = ui_text(b)
                b["label_of"] = a["i"]


def ui_tree(inst, root="all"):
    if inst.dead:
        raise RuntimeError(f"instance {inst.id} is not running (exit code {inst.exit_code})")
    if inst.hung:
        return {"loading": True, "reason": "hung", "nodes": [], "dialogs": []}
    if inst.call("engine.heartbeat", timeout=5).get("loading"):
        return {"loading": True, "nodes": [], "dialogs": [], "source": "supervisor"}
    res = inst.call("ui.tree", {"root": root}, timeout=5)
    inst.ui_screen = tuple(res.get("screen") or (0, 0)) or None
    ui_labels(res.get("nodes", []))
    return res


def ui_node_out(inst, n, image_size):
    o = {"path": n["path"], "kind": n["kind"]}
    for k in ("name", "script_class"):
        if k in n:
            o[k] = n[k]
    if n["kind"] in ("window", "static", "text") or "script_class" in n:
        o["class"] = n["class"]
    t = ui_text(n)
    if t:
        o["text"] = t if len(t) <= 300 else t[:300] + "..."
    for k in ("label", "num", "speaker", "checked", "active", "pushed", "selected", "focus", "value", "tab_id", "active_tab",
              "scroll", "item", "enabled", "hover", "clip"):
        if k in n:
            o[k] = n[k]
    r = n["rect"]
    o["rect"] = ui_px(inst, r, image_size)
    o["center"] = ui_px(inst, [(r[0] + r[2]) / 2, (r[1] + r[3]) / 2], image_size)
    return o


def ui_talk(inst, nodes, image_size):
    qs = [n for n in nodes if n["kind"] == "question"]
    ans = [n for n in nodes if n["kind"] == "answer"]
    if not qs and not ans:
        return None
    return {"phrases": [{"speaker": a.get("speaker"), "text": ui_text(a)} for a in ans[-6:]],
            "options": [{"index": i + 1, "id": q.get("id"), "text": ui_text(q), "path": q["path"],
                         "center": ui_node_out(inst, q, image_size)["center"],
                         **({"clip": "out"} if q.get("clip") == "out" else {})} for i, q in enumerate(qs)]}


def ui_match(pattern, value):
    if value is None:
        return False
    p, v = pattern.casefold(), value.casefold()
    return fnmatch.fnmatchcase(v, p) if any(c in p for c in "*?[") else v == p


def game_ui_run(inst, view, root, text, name, kind, limit, image_size, include_clipped):
    t0 = time.perf_counter()
    res = ui_tree(inst, root)
    nodes = res.get("nodes", [])
    out = {"instance": inst.id, "dialogs": res.get("dialogs", []), "image_size": list(ui_image_size(inst, image_size))}
    if res.get("loading"):
        out["loading"] = True
        return out
    cur = res.get("cursor")
    if cur:
        out["cursor"] = {**cur, "px": ui_px(inst, [cur["x"], cur["y"]], image_size)}
    talk = ui_talk(inst, nodes, image_size)
    if talk:
        out["talk"] = talk
    sel = nodes
    if not include_clipped:
        sel = [n for n in sel if n.get("clip") != "out"]
    if view == "interactive":
        sel = [n for n in sel if n["kind"] in UI_INTERACTIVE or "item" in n]
    elif view == "text":
        sel = [n for n in sel if ui_text(n)]
    if text:
        sel = [n for n in sel if any(ui_match(p, v) for p in (text, f"*{text}*") for v in ui_names(n))]
    if name:
        sel = [n for n in sel if ui_match(name, n.get("name"))]
    if kind:
        sel = [n for n in sel if n["kind"] in kind]
    out["count"] = len(sel)
    if len(sel) > limit:
        out["omitted"] = len(sel) - limit
    if view == "tree":
        out["nodes"] = [{"d": n["d"], **ui_node_out(inst, n, image_size)} for n in sel[:limit]]
    else:
        out["nodes"] = [ui_node_out(inst, n, image_size) for n in sel[:limit]]
    clipped = sum(1 for n in nodes if n.get("clip") == "out")
    if clipped and not include_clipped:
        out["clipped_out"] = clipped
    if res.get("truncated"):
        out["truncated"] = res["truncated"]
    out["engine_ms"] = res.get("ms")
    out["call_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return out


def ui_find(inst, nodes, text, name, path, option, kind, nth):
    by_i = {n["i"]: n for n in nodes}

    def interactive_ancestor(n):
        if "label_of" in n:
            return by_i.get(n["label_of"])
        while n is not None:
            if n["kind"] in UI_INTERACTIVE:
                return n
            n = by_i.get(n["p"])
        return None

    if option is not None:
        qs = [n for n in nodes if n["kind"] == "question"]
        if not 1 <= option <= len(qs):
            raise ValueError(f"option {option} not available: {len(qs)} answer option(s) "
                             f"{[ui_text(q) for q in qs]}")
        if qs[option - 1].get("clip") == "out":
            raise ValueError(f"option {option} is scrolled out of the list; use game_talk(n={option})")
        return qs[option - 1]
    cands = nodes
    if path is not None:
        cands = [n for n in cands if n["path"] == path]
    if text is not None:
        cands = [n for n in cands if any(ui_match(text, v) for v in ui_names(n))]
    if name is not None:
        cands = [n for n in cands if ui_match(name, n.get("name"))]
    if kind:
        cands = [n for n in cands if n["kind"] in kind]
    # a matching text inside a button/list item/option clicks that widget
    mapped = []
    for n in cands:
        a = interactive_ancestor(n) or n
        if a not in mapped:
            mapped.append(a)

    def has_ancestor_in(n, ids):
        p = by_i.get(n["p"])
        while p is not None:
            if p["i"] in ids:
                return True
            p = by_i.get(p["p"])
        return False

    ids = {n["i"] for n in mapped}
    mapped = [n for n in mapped if not has_ancestor_in(n, ids)]  # e.g. a talk option and its caption button
    # script inventories handle cells by cursor position, inside windows flagged disabled
    usable = [n for n in mapped if n.get("clip") != "out" and (n.get("enabled", True) or n["kind"] == "cell")
              and n["rect"][2] > n["rect"][0] and n["rect"][3] > n["rect"][1]]
    inter = [n for n in usable if n["kind"] in UI_INTERACTIVE]
    usable = inter or usable
    if not usable:
        why = "" if not mapped else f" ({len(mapped)} match(es) are disabled, hidden by scrolling or empty)"
        texts = sorted({ui_text(n) for n in nodes if ui_text(n) and n.get("clip") != "out"})
        hint = difflib.get_close_matches(text, texts, 8, 0.4) if text else texts[:40]
        raise ValueError(f"no visible enabled widget matches{why}; visible texts: {hint}")
    if len(usable) > 1 and nth is None:
        raise ValueError("ambiguous, pass nth (1-based) or path: " + json.dumps(
            [{"path": n["path"], "kind": n["kind"], "text": ui_text(n), "name": n.get("name"),
              "rect_ui": n["rect"]} for n in usable[:20]], ensure_ascii=False))
    if nth is not None and not 1 <= nth <= len(usable):
        raise ValueError(f"nth {nth} out of range 1..{len(usable)}")
    return usable[(nth or 1) - 1]


def game_ui_click_run(inst, text, name, path, option, kind, button, double, root, nth, settle_ms, image_size):
    res = ui_tree(inst, root)
    if res.get("loading"):
        raise RuntimeError("the game is loading (or hung): no UI to click")
    if not res.get("dialogs"):
        raise RuntimeError(f"no menu or dialog is open (root {root!r}): nothing to click")
    n = ui_find(inst, res.get("nodes", []), text, name, path, option, kind, nth)
    r = n["rect"]
    at = [round((r[0] + r[2]) / 2, 2), round((r[1] + r[3]) / 2, 2)]
    # main menu entries (CUIMMShniaga) take a click only after the hover highlight slid onto them
    hover = UI_MENU_HOVER_MS if n["kind"] == "menu_item" and not n.get("hover") else None
    inp = input_run(inst, ui_click_steps(at, button, double, hover=hover), True, 10)
    time.sleep(max(0, settle_ms) / 1000)
    st = game_state_get(inst, [])
    ui = st.get("ui", {})
    out = {"instance": inst.id, "clicked": ui_node_out(inst, n, image_size), "at_ui": at,
           "at_px": ui_px(inst, at, image_size), "mode": st.get("mode")}
    for k in ("dialog", "menu_dialog"):
        if k in ui:
            out[k] = ui[k]
    if inp.get("warning"):
        out["warning"] = inp["warning"]
    return out


@tool
async def game_ui(instance: str | None = None, view: str = "interactive", root: str = "top", text: str | None = None,
                  name: str | None = None, kind: list[str] | None = None, limit: int = 80,
                  image_size: list[int] | None = None, include_clipped: bool = False) -> dict:
    """What is on screen as widgets, to click by meaning.
    view: 'interactive' (buttons, tabs, edits, list items, inventory cells, talk options, menu entries), 'text' (all
    visible text), 'tree', 'all'. root: 'top' (the dialog that owns input), 'menu', 'game', 'hud', 'all'. Filters:
    text (glob / substring), name, kind, limit.
    Per node: path (for game_ui_click), kind, text, rect and center in pixels of the last game_screenshot, state
    (checked, selected, enabled ...), item {section, name, id, count} for inventory / trade cells. The 3D PDA needs
    one click first (game_input with key LMB) before its widgets are enabled."""
    inst = get(instance)
    if view not in ("interactive", "text", "tree", "all"):
        raise ValueError('view must be "interactive", "text", "tree" or "all"')
    return await run(game_ui_run, inst, view, root, text, name, kind, max(1, int(limit)), image_size, include_clipped)


@tool
async def game_ui_click(instance: str | None = None, text: str | None = None, name: str | None = None,
                        path: str | None = None, option: int | None = None, kind: list[str] | None = None,
                        button: str = "left", double: bool = False, root: str = "top", nth: int | None = None,
                        settle_ms: int = 300, image_size: list[int] | None = None) -> dict:
    """Click a visible widget the way the mouse would. Pick it by text (shown text, exact or glob), name, path (from
    game_ui) or option (talk answer number). button: left / right / middle; double=True (equip / use an inventory
    item). Ambiguous -> error listing the candidates (use nth or path). Returns the clicked node and the resulting
    mode / dialog."""
    inst = get(instance)
    if text is None and name is None and path is None and option is None:
        raise ValueError("pass text, name, path or option")
    return await run(game_ui_click_run, inst, text, name, path, option, kind, button, double, root, nth, settle_ms,
                     image_size)


# game_talk: engine ui.talk = the talk window's full option list and log (scrolled out or not) with phrase/dialog ids;
# choosing presses the option's number key (options 1..10) or scrolls the list and clicks it, like a player.

def talk_get(inst, since=0):
    if inst.dead:
        raise RuntimeError(f"instance {inst.id} is not running (exit code {inst.exit_code})")
    if inst.hung or inst.call("engine.heartbeat", timeout=5).get("loading"):
        return {"open": False, "loading": True}
    v = inst.call("ui.talk", {"since": since}, timeout=5)
    if v.get("open") and not v.get("top"):
        d = game_state_get(inst, []).get("ui", {}).get("dialog") or {}
        v["mode"] = d.get("script_mode") or d.get("menu_mode") or d.get("kind") or "other"
    elif v.get("open"):
        v["mode"] = "talk"
    return v


def talk_line(e, line_chars):
    o = {k: e[k] for k in ("i", "speaker", "actor", "info") if e.get(k) is not None}
    t = ui_text(e) or ""
    if line_chars and len(t) > line_chars:
        o["text"], o["truncated"] = t[:line_chars] + "…", len(t)
    else:
        o["text"] = t
    return o


def talk_out(inst, v, line_chars, log_last):
    out = {"instance": inst.id, "open": bool(v.get("open"))}
    if v.get("loading"):
        out["loading"] = True
    elif not v.get("open"):
        out["game_mode"] = game_state_get(inst, []).get("mode")
    if not v.get("open"):
        return out
    out["mode"] = v["mode"]
    for k in ("partner", "topic", "dialog", "pending"):
        if k in v:
            out[k] = v[k]
    out["options"] = [{**{k: o[k] for k in ("n", "key", "id", "dialog") if o.get(k) is not None}, "text": ui_text(o),
                       **{k: o[k] for k in ("final", "ends", "actions", "give_info", "disable_info", "replies", "clip") if k in o}}
                      for o in v.get("options", [])]
    log = v.get("log", [])
    if log_last is not None:
        log = log[-log_last:] if log_last > 0 else []
    out["log"] = [talk_line(e, line_chars) for e in log]
    out["log_count"] = v.get("log_count", 0)
    out["engine_ms"] = v.get("ms")
    return out


def talk_pick(v, id, n, text):
    opts = v.get("options", [])
    if id is not None:
        m = [o for o in opts if o.get("id") == str(id)]
    elif n is not None:
        m = [o for o in opts if o["n"] == n]
    else:
        m = [o for o in opts if ui_match(text, ui_text(o))] or [o for o in opts if ui_match(f"*{text}*", ui_text(o))]
    if len(m) == 1:
        return m[0]
    listed = [{"n": o["n"], "id": o.get("id"), "text": ui_text(o)} for o in (m or opts)]
    raise ValueError(("ambiguous, pass id or n: " if m else "no such option; options: ") +
                     json.dumps(listed, ensure_ascii=False))


def talk_scroll_click(inst, o_id, n):
    """Option beyond the number keys: wheel the option list until the option is fully inside it, then click it."""
    for _ in range(60):
        v = inst.call("ui.talk", {"since": 1 << 30}, timeout=5)
        o = next((x for x in v.get("options", []) if x["n"] == n and x.get("id") == o_id), None)
        if not o:
            raise RuntimeError("the option list changed while scrolling to the option")
        r, lr = o["rect"], v["options_rect"]
        mid = [round((lr[0] + lr[2]) / 2, 2), round((lr[1] + lr[3]) / 2, 2)]
        if r[1] >= lr[1] and r[3] <= lr[3]:
            input_run(inst, ui_click_steps([round((r[0] + r[2]) / 2, 2), round((r[1] + r[3]) / 2, 2)]), True, 10)
            return "click"
        input_run(inst, [{"cursor": mid}, {"wheel": 1 if r[1] < lr[1] else -1}, {"wait": 30}], True, 10)
    raise RuntimeError("could not scroll the option into view")


def game_talk_run(inst, id, n, text, line_chars, log_last, timeout):
    t0 = time.perf_counter()
    v = talk_get(inst)
    if id is None and n is None and text is None:
        out = talk_out(inst, v, line_chars, log_last)
        out["call_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return out
    if not v.get("open"):
        raise RuntimeError("no talk window is open (walk up to an NPC, look at the body, game_input use)")
    if v["mode"] != "talk":
        raise RuntimeError(f"the talk window is under a {v['mode']} window; close it first (game_input ESC)")
    # CUITalkWnd::AskQuestion drops a choice while the option list waits for its rebuild (pending: just opened, an
    # info portion given): pick from the rebuilt list
    deadline = time.monotonic() + 2
    while v.get("pending") and v.get("open") and time.monotonic() < deadline:
        time.sleep(0.03)
        v = talk_get(inst)
    o = talk_pick(v, id, n, text)
    before = v.get("log_count", 0)
    ids = [x.get("id") for x in v["options"]]
    retried = False
    while True:
        via = f"key {o['key']}" if o.get("key") else talk_scroll_click(inst, o.get("id"), o["n"])
        if o.get("key"):
            input_run(inst, [{"key": o["key"]}], True, 10)
        # the partner answers in the same frame; the option list is rebuilt on the next one
        t_press = time.monotonic()
        deadline = t_press + timeout
        while True:
            a = talk_get(inst, before)
            if not a.get("open") or a.get("loading") or a["mode"] != "talk" or (
                    not a.get("pending") and (a.get("log_count", 0) != before or a.get("dialog") != v.get("dialog")
                                              or [x.get("id") for x in a["options"]] != ids)):
                break
            # nothing at all after 1 s: the press was dropped (pending rebuild between our read and the key) -> once more
            if not retried and not a.get("pending") and time.monotonic() - t_press > 1 and (
                    [x.get("id") for x in a["options"]] == ids):
                break
            if time.monotonic() > deadline:
                break
            time.sleep(0.03)
        if retried or not a.get("open") or a.get("loading") or a["mode"] != "talk" or a.get("log_count", 0) != before \
                or a.get("dialog") != v.get("dialog") or [x.get("id") for x in a["options"]] != ids \
                or time.monotonic() > deadline:
            break
        retried = True
        o = talk_pick(a, o.get("id"), None, None) if o.get("id") is not None else o
    out = {"instance": inst.id, "chose": {"n": o["n"], "id": o.get("id"), "dialog": o.get("dialog"),
                                          "text": ui_text(o), "via": via}}
    if retried:
        out["chose"]["retried"] = True
    if a.get("open") and not a.get("loading"):
        out["new_lines"] = [talk_line(e, line_chars) for e in a.get("log", [])]
        if a["mode"] == "talk" and a.get("log_count", 0) == before and a.get("dialog") == v.get("dialog"):
            out["warning"] = "nothing changed after the choice (a script may have refused it)"
    full = talk_out(inst, a, line_chars, log_last if log_last is not None else 0)
    if not full.get("log"):
        full.pop("log", None)
    out.update({k: x for k, x in full.items() if k != "instance"})
    if not a.get("open"):
        st = game_state_get(inst, [])
        out["game_mode"] = st.get("mode")
        if st.get("ui", {}).get("dialog"):
            out["dialog_window"] = st["ui"]["dialog"]
    out["call_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return out


@tool
async def game_talk(instance: str | None = None, id: str | None = None, n: int | None = None,
                    text: str | None = None, line_chars: int = 2000, log_last: int | None = None,
                    timeout: float = 3) -> dict:
    """Read the open NPC conversation in full (all options and lines, also scrolled out) and choose an option. Open the
    talk first: game_use(id) on the NPC.
    No choice: partner, options [{n, id, text, ...}], log (log_last: only the last N lines). Choose by id (phrase
    id, the same in vanilla and mods: prefer it), n (on-screen number) or text (glob / substring): returns what was
    chosen, new_lines said since, and the new options. open=false = no talk window; mode trade / upgrade when it
    switched there."""
    inst = get(instance)
    if sum(x is not None for x in (id, n, text)) > 1:
        raise ValueError("pass only one of id, n, text")
    return await run(game_talk_run, inst, None if id is None else str(id), n, text, max(0, int(line_chars)),
                     log_last, timeout)


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
    """Measure frame performance over `seconds` (0.1..120): fps, frame_ms percentiles, hitches, the frame cap in
    effect, main-thread phases (ms, %, Lua time inside), worker tasks, memory. Uncap first for real numbers:
    console_exec('r__framelimit 0')."""
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
    """Profile the game's Lua over `seconds`: which function, script, callback or engine-to-Lua entry costs time (ms
    per frame), plus the 5 worst frames. mode: 'time' (default, low overhead), 'calls' (exact call counts, slow),
    'alloc' (allocation per function). top: rows per list."""
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

SC_ITEMSEC = r'''
local sec, ini = A.section, ini_sys
local function no(m) return J({ok = false, reason = m}) end
if not ini:section_exist(sec) then return no("no such section in system_ini") end
if not ini:line_exist(sec, "class") then return no("no class (a base/template section)") end
local cname = ini:r_string_ex(sec, "class")
local okc, cls = pcall(ini.r_clsid, ini, sec, "class")
if not okc then return no("unknown class " .. cname) end
-- _g's IsItem is not a clsid test; inv_weight is mandatory for CInventoryItem::Load and absent on non-items
if IsMonster(nil, cls) or IsStalker(nil, cls) or IsAnomaly(nil, cls) or not ini:line_exist(sec, "inv_weight") then
  return no("class " .. cname .. " is not an inventory item; use game_spawn")
end
return J({ok = true, ammo = IsAmmo(nil, cls) and true or false})
'''

SC_INV = r'''
if not in_game() then return fail(NOGAME) end
local want, have, n = {}, {}, 0
for _, id in ipairs(A.ids) do want[id] = true end
db.actor:iterate_inventory(function(_, it)
  if want[it:id()] then have[#have + 1] = it:id() end
  if it:section() == A.section then n = n + 1 end
end, db.actor)
return J({have = have, count = n})
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
    slot = slot + 1 -- engine slot = ini slot + 1 (CInventoryItem::Load)
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
    cat_key = sc_catalog(inst).get(section, (None,))[0]
    if cat_key and not sc_kind_match("item", cat_key):
        raise ValueError(f"{section} is a {cat_key} section, not an inventory item; use game_spawn")
    if not cat_key:  # not in the debug spawner lists (quest items etc.): any item section of system_ini
        chk = sc_exec(inst, SC_ITEMSEC, {"section": section})
        if not chk["ok"]:
            if chk["reason"].startswith("no such"):
                sc_section(inst, section)  # raises with similar section names
            raise ValueError(f"{section}: {chk['reason']}")
        cat_key = "weapons_ammo" if chk["ammo"] else "items"
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
    deadline = time.time() + 5  # alife create is async: return once every id is in the actor's inventory
    while True:
        inv = sc_exec(inst, SC_INV, {"section": section, "ids": out["ids"]})
        if len(inv["have"]) >= len(out["ids"]) or time.time() >= deadline:
            break
        time.sleep(0.1)
    res["in_inventory"], res["inventory_count"] = len(inv["have"]), inv["count"]
    if len(inv["have"]) < len(out["ids"]):
        res["note"] = f"timeout: only {len(inv['have'])}/{len(out['ids'])} items in the inventory after 5 s"
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
    """What can be spawned (mods' sections included). No filter: categories with counts. kind: item, weapon, ammo,
    outfit, artefact, monster, stalker, squad, monster_squad, stalker_squad, or a category glob. pattern: glob /
    substring on the section name. faction: squads of that faction."""
    inst = get(instance)
    return await run(spawnable, inst, kind, pattern, faction, max(1, min(int(limit), 2000)))


@tool
async def game_spawn(section: str, count: int = 1, position: list[float] | None = None,
                     distance: float | None = None, relation: str | None = None, instance: str | None = None,
                     wait: float = 3) -> dict:
    """Spawn count (1..50) of a section (see game_spawnable): items, monsters, stalkers, squads, anomalies, vehicles.
    Placed at position [x,y,z], else `distance` m ahead of the camera, on the walkable map. relation (squads /
    stalkers): enemy / neutral / friend towards the actor. Returns the ids. Remove them with
    game_release(spawned=True)."""
    inst = get(instance)
    return with_error_hint(inst, await run(spawn, inst, section, int(count), position, distance, relation,
                                           max(0.0, min(float(wait), 30.0))))


@tool
async def game_give(section: str, count: int = 1, rounds: int | None = None, equip: bool = False,
                    remove: bool = False, instance: str | None = None) -> dict:
    """Give the actor count items of section. Ammo: count = boxes, or rounds = an exact number of rounds. equip=True:
    put it in its slot (weapon in hands, outfit worn). remove=True: take up to count of them away instead (0 = all)."""
    inst = get(instance)
    return with_error_hint(inst, await run(give, inst, section, int(count), rounds, equip, remove))


@tool
async def game_actor(heal: bool = False, health: float | None = None, stamina: float | None = None,
                     radiation: float | None = None, satiety: float | None = None, psy_health: float | None = None,
                     bleeding: float | None = None, money: int | None = None,
                     instance: str | None = None) -> dict:
    """Set actor stats (0..1): health, stamina, radiation, satiety, psy_health; bleeding=0 stops it; money = exact
    amount; heal=True = full restore. Returns before and after. Cheats are console cvars: console_exec('g_god on' /
    'g_unlimitedammo on' / 'g_infinite_mag on' / 'g_norecoil on')."""
    inst = get(instance)
    return with_error_hint(inst, await run(lambda: actor_set(inst, heal=heal, health=health, stamina=stamina,
                                                             radiation=radiation, satiety=satiety,
                                                             psy_health=psy_health, bleeding=bleeding, money=money)))


@tool
async def game_kill(ids: list[int], by_actor: bool = False, instance: str | None = None) -> dict:
    """Kill nearby (online) creatures by id (a squad id = its members) with normal death handling. by_actor=True: the
    actor gets the kill (quests, goodwill). Offline creatures cannot be killed: use game_release."""
    inst = get(instance)
    return with_error_hint(inst, {"instance": inst.id, "results": await run(
        sc_exec, inst, SC_KILL, {"ids": [int(i) for i in ids], "by_actor": by_actor})})


@tool
async def game_release(ids: list[int] | None = None, spawned: bool = False, instance: str | None = None) -> dict:
    """Remove objects from the game without a death: ids (any objects; squads go with their members), or spawned=True =
    everything game_spawn created."""
    inst = get(instance)
    return with_error_hint(inst, await run(release, inst, ids, spawned))


@tool
async def game_weather(weather: str | None = None, time: str | None = None, advance_hours: float | None = None,
                       instance: str | None = None) -> dict:
    """Weather and time of day. No arguments: the current state and the valid weathers. weather: a cycle (clear,
    cloudy, rainy, stormy, foggy) or a preset, applied at once. time: 'HH:MM', reached by advancing the clock (never
    backwards); advance_hours: advance further."""
    inst = get(instance)
    return with_error_hint(inst, await run(weather_set, inst, weather, time, advance_hours))


@tool
async def game_emission(kind: str = "surge", action: str = "status", instance: str | None = None) -> dict:
    """Start, stop or check an emission (kind='surge') or psi-storm (kind='psi_storm') with the real scripted effects.
    action: start / stop / status. The game can refuse a start (underground level, disabled in options); notes say
    why."""
    inst = get(instance)
    if kind not in ("surge", "psi_storm"):
        raise ValueError("kind must be surge or psi_storm")
    if action not in ("start", "stop", "status"):
        raise ValueError("action must be start, stop or status")
    return with_error_hint(inst, {"instance": inst.id,
                                  **await run(sc_exec, inst, SC_EMISSION, {"kind": kind, "action": action})})


# ---------------------------------------------------------------- game_wait
# One JSON condition tree evaluated supervisor-side. Events and log lines are push-based (the reader notifies
# inst.wake); state / object / Lua leaves share one pipelined request batch per poll (the engine answers all of them
# in the same frame). Lua runs with lua.exec quiet (no exec marker lines in the log).

WT_EVENT_TYPES = ("script_error", "level_loaded", "level_unloaded", "actor_spawned", "actor_died", "game_saved",
                  "game_loaded", "menu_opened", "menu_closed", "task_given", "task_completed", "task_failed",
                  "task_in_progress", "task_stage", "info_given", "info_removed", "death", "hit", "combat", "relation",
                  "guard_fight")
WT_OPS = ("eq", "ne", "lt", "le", "gt", "ge", "in", "match", "exists", "within")
WT_LEAVES = {  # leaf key -> extra keys it accepts (besides the comparison ops where marked by "ops")
    "in_control": (), "loaded": (), "mode": (), "dialog": ("open",), "event": ("where",), "log": ("since", "ignore_case"),
    "sim_ms": (), "frames": (), "game_clock": (), "state": ("ops",), "has_item": ("ops",),
    "object": ("is", "field", "ops"), "count": ("ops",), "lua": (), "hung": (), "crash": ()}
WT_OBJECT_IS = ("exists", "gone", "alive", "dead", "online", "offline")
WT_COUNT_KEYS = ("kinds", "pattern", "radius", "alive", "hostile_only", "visible_only", "position", "offline")
WT_LUA = r'''local f = loadstring("return " .. @E@)
local e
if not f then f, e = loadstring(@E@) end
if not f then return "E", e end
local ok, v = pcall(f)
if not ok then return "E", tostring(v) end
return "V", (v ~= nil and v ~= false) and "1" or "0", tostring(v)'''


class WaitAbort(Exception):
    pass


def wt_luastr(s):
    """Lua long-bracket literal (no escapes needed)."""
    eq = 0
    while f"]{'=' * eq}]" in s:
        eq += 1
    return f"[{'=' * eq}[{s}]{'=' * eq}]"


def wt_compile(spec, where="until"):
    """Validate a condition and turn it into a node {k, spec, of?} (shorthand strings expanded)."""
    if isinstance(spec, list):
        if not spec:
            raise ValueError(f"{where}: empty condition list")
        return {"k": "all", "spec": "all", "of": [wt_compile(s, where) for s in spec]}
    if isinstance(spec, str):
        if spec in ("in_control", "loaded", "hung", "crash"):
            spec = {spec: True}
        elif spec == "main_menu":
            spec = {"mode": "main_menu"}
        elif any(fnmatch.fnmatchcase(t, spec) for t in WT_EVENT_TYPES):
            spec = {"event": spec}
        else:
            raise ValueError(f"{where}: unknown condition {spec!r}; strings: in_control, loaded, main_menu, hung, crash "
                             f"or an event type/glob {list(WT_EVENT_TYPES)}; else use an object like "
                             '{"state": "actor.health", "lt": 0.5}')
    if not isinstance(spec, dict):
        raise ValueError(f"{where}: a condition is a string, an object or a list, not {spec!r}")
    keys = [k for k in spec if k in WT_LEAVES or k in ("all", "any", "not")]
    if len(keys) != 1:
        raise ValueError(f"{where}: {json.dumps(spec)} needs exactly one of {list(WT_LEAVES) + ['all', 'any', 'not']}")
    k = keys[0]
    n = {"k": k, "spec": spec, "met": False, "value": None}
    if k in ("all", "any"):
        if not isinstance(spec[k], list) or not spec[k] or len(spec) != 1:
            raise ValueError(f"{where}: {k} takes a non-empty list of conditions")
        n["of"] = [wt_compile(s, where) for s in spec[k]]
        return n
    if k == "not":
        n["of"] = [wt_compile(spec[k], where)]
        return n
    extra = WT_LEAVES[k]
    allowed = {k, *(x for x in extra if x != "ops"), *(WT_OPS + ("of",) if "ops" in extra else ())}
    bad = [x for x in spec if x not in allowed]
    if bad:
        raise ValueError(f"{where}: {k} does not take {bad}; it takes {sorted(allowed - {k})}")
    v = spec[k]
    if k in ("sim_ms", "frames") and not (isinstance(v, (int, float)) and v >= 0):
        raise ValueError(f"{where}: {k} must be a number >= 0")
    if k == "game_clock":
        m = re.match(r"^(\d{1,2}):(\d{2})$", str(v))
        if not m or int(m[1]) > 23 or int(m[2]) > 59:
            raise ValueError(f"{where}: game_clock must be 'HH:MM'")
        n["target_ms"] = (int(m[1]) * 60 + int(m[2])) * 60000
    if k == "log":
        n["rx"] = re.compile(v, re.I if spec.get("ignore_case") else 0)
        if "since" in spec and not (isinstance(spec["since"], int) and spec["since"] >= 0):
            raise ValueError(f"{where}: log since must be a seq >= 0 (log_tail last_seq)")
    if k == "event":
        if not isinstance(v, str):
            raise ValueError(f"{where}: event takes a type name or glob, e.g. 'task_*'")
        if not any(fnmatch.fnmatchcase(t, v) for t in WT_EVENT_TYPES):
            raise ValueError(f"{where}: event {v!r} matches no event type {list(WT_EVENT_TYPES)}")
        if not isinstance(spec.get("where", {}), dict):
            raise ValueError(f"{where}: where must be an object of data fields")
    if k in ("mode", "dialog", "has_item", "state", "lua") and not isinstance(v, (str, list) if k == "mode" else str):
        raise ValueError(f"{where}: {k} takes a string" + (" or a list" if k == "mode" else ""))
    if k == "object":
        if not isinstance(v, int) or not 0 <= v < 0xffff:
            raise ValueError(f"{where}: object takes an id 0..65534")
        if spec.get("is", "exists") not in WT_OBJECT_IS:
            raise ValueError(f"{where}: object is must be one of {list(WT_OBJECT_IS)}")
    if k == "count":
        if not isinstance(v, dict) or [x for x in v if x not in WT_COUNT_KEYS]:
            raise ValueError(f"{where}: count takes a game_objects filter object with keys {list(WT_COUNT_KEYS)}")
        bk = [x for x in v.get("kinds") or [] if x not in WORLD_KINDS]
        if bk:
            raise ValueError(f"{where}: unknown kinds {bk}; valid: {list(WORLD_KINDS)}")
        p = {"radius": str(float(v.get("radius", 100))), "summary": 1}
        if v.get("kinds"):
            p["kinds"] = ",".join(v["kinds"])
        if v.get("pattern"):
            p["pattern"] = v["pattern"]
        if v.get("alive") is not None:
            p["alive"] = int(bool(v["alive"]))
        for f in ("hostile_only", "visible_only", "offline"):
            if v.get(f):
                p[f] = 1
        if v.get("position") is not None:
            p["position"] = ",".join(str(float(c)) for c in v["position"])
        n["params"] = p
    if "within" in spec:
        o = spec.get("of")
        if not (o == "actor" or isinstance(o, int) or (isinstance(o, list) and len(o) == 3)):
            raise ValueError(f'{where}: within needs of = [x,y,z], "actor" or an object id')
    return n


def wt_nodes(n):
    yield n
    for c in n.get("of") or []:
        yield from wt_nodes(c)


def wt_needs(nodes):
    need = {"state": None, "probe": False, "objects": set(), "counts": {}, "lua": set(), "hb": False, "time": False}

    def state(*secs):
        need["state"] = (need["state"] or set()) | set(secs)
    for n in nodes:
        k, sp = n["k"], n["spec"]
        if k in ("in_control", "loaded", "mode", "dialog", "sim_ms", "game_clock"):
            state()
        if k == "in_control":
            need["probe"] = True
        if k in ("sim_ms", "game_clock"):
            need["time"] = True
        if k == "frames":
            need["hb"] = True
        if k == "state":
            sec = sp["state"].split(".")[0]
            state(*([sec] if sec in STATE_SECTIONS else []))
        if k == "has_item":
            state("inventory")
        if k == "object":
            need["objects"].add(sp["object"])
        if k == "count":
            need["counts"][json.dumps(n["params"], sort_keys=True)] = n["params"]
        if k == "lua":
            need["lua"].add(sp["lua"])
        if isinstance(n["spec"], dict) and "within" in sp:
            if sp.get("of") == "actor":
                state()
            elif isinstance(sp.get("of"), int):
                need["objects"].add(sp["of"])
    need["polled"] = bool(need["state"] is not None or need["probe"] or need["objects"] or need["counts"]
                          or need["lua"] or need["hb"])
    return need


def wt_batch(inst, calls, timeout):
    """Send all requests at once (one engine frame answers them), then collect: result or Exception per call."""
    if not calls:
        return []
    s = inst.sock
    if s is None:
        return [RuntimeError("not connected")] * len(calls)
    rids, data = [], []
    for m, p in calls:
        rid = next(inst.ids)
        inst.pending[rid] = [threading.Event(), None]
        rids.append(rid)
        data.append(json.dumps({"id": rid, "method": m, "params": p}) + "\n")
    out = []
    try:
        with inst.wfile_lock:
            s.sendall("".join(data).encode())
        deadline = time.monotonic() + timeout
        for rid, (m, _) in zip(rids, calls):
            p = inst.pending[rid]
            if not p[0].wait(max(0.0, deadline - time.monotonic())):
                out.append(TimeoutError(f"{m}: no reply in {timeout:.1f} s"))
            elif p[1] is None:
                out.append(RuntimeError(f"{m}: connection lost"))
            elif "error" in p[1]:
                out.append(RuntimeError(f"{m}: {p[1]['error'].get('message')}"))
            else:
                out.append(p[1].get("result"))
    except OSError as e:
        out += [RuntimeError(f"send failed: {e}")] * (len(calls) - len(out))
    finally:
        for rid in rids:
            inst.pending.pop(rid, None)
    return out


def wt_poll(inst, need, budget, clock):
    """heartbeat (net thread), then unless loading/hung one batch of main-thread calls."""
    snap = {"hb": None, "loading": None, "error": None, "state": None, "probe": None, "objects": {}, "counts": {},
            "lua": {}}
    try:
        snap["hb"] = inst.call("engine.heartbeat", timeout=max(0.2, min(5.0, budget)))
        snap["loading"] = bool(snap["hb"].get("loading"))
    except Exception as e:
        snap["error"] = f"{type(e).__name__}: {e}"
        return snap
    if snap["loading"] or inst.hung:
        return snap
    calls = []
    if need["state"] is not None or clock:
        calls.append(("state", None, "state.get", {"sections": ",".join(sorted((need["state"] or set()) | {"stats"}))}))
    if need["probe"]:
        calls.append(("probe", None, "lua.exec", {"code": TP_PROBE, "quiet": 1}))
    for i in sorted(need["objects"]):
        calls.append(("objects", i, "world.object", {"id": i}))
    for key, p in need["counts"].items():
        calls.append(("counts", key, "world.objects", p))
    for code in need["lua"]:
        calls.append(("lua", code, "lua.exec", {"code": WT_LUA.replace("@E@", wt_luastr(code)), "quiet": 1}))
    res = wt_batch(inst, [(m, p) for _, _, m, p in calls], max(0.2, min(3.0, budget)))
    for (slot, key, _, _), r in zip(calls, res):
        if key is None:
            snap[slot] = r
        else:
            snap[slot][key] = r
    return snap


def wt_clock(snap):
    st = snap["state"] if isinstance(snap["state"], dict) else {}
    stats = st.get("stats") or {}
    return {"frame": (snap["hb"] or {}).get("frame"), "time_global_ms": stats.get("time_global_ms"),
            "game_time_ms": stats.get("game_time_ms"), "mode": st.get("mode")}


def wt_path(obj, path):
    for part in path.split(".") if path else []:
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.lstrip("-").isdigit() and -len(obj) <= int(part) < len(obj):
            obj = obj[int(part)]
        elif isinstance(obj, list):  # element by id, e.g. tasks.<task id>.stage
            obj = next((e for e in obj if isinstance(e, dict) and str(e.get("id")) == part), None)
        else:
            return None
    return obj


def wt_pos(v):
    if isinstance(v, dict):
        v = [v.get("x"), v.get("y"), v.get("z")]
    if isinstance(v, list) and len(v) == 3 and all(isinstance(c, (int, float)) for c in v):
        return v
    return None


def wt_obj(r):
    """world.object result -> (object dict or None, problem string or None)."""
    if isinstance(r, Exception):
        return None, f"{type(r).__name__}: {r}"
    if not isinstance(r, dict) or not r.get("available"):
        return None, (r or {}).get("reason", "unavailable") if isinstance(r, dict) else "unavailable"
    return r.get("object"), None


def wt_ops(n, v, snap):
    """Apply the comparison ops of a leaf to its value; no op = truthy."""
    sp = n["spec"]
    ops = [o for o in WT_OPS if o in sp]
    if not ops:
        return bool(v)
    num = isinstance(v, (int, float)) and not isinstance(v, bool)
    for o in ops:
        a = sp[o]
        if o == "exists":
            ok = (v is not None) == bool(a)
        elif o == "eq":
            ok = v == a
        elif o == "ne":
            ok = v != a
        elif o == "in":
            ok = isinstance(a, list) and v in a
        elif o == "match":
            ok = isinstance(v, str) and fnmatch.fnmatch(v.lower(), str(a).lower())
        elif o == "within":
            of = sp["of"]
            if of == "actor":
                ref = wt_pos(wt_path(snap["state"] if isinstance(snap["state"], dict) else None, "actor.position"))
            elif isinstance(of, int):
                o2, _ = wt_obj(snap["objects"].get(of))
                ref = wt_pos((o2 or {}).get("position"))
            else:
                ref = wt_pos(of)
            p = wt_pos(v)
            n["distance"] = round(sum((a - b) ** 2 for a, b in zip(p, ref)) ** 0.5, 2) if p and ref else None
            ok = n["distance"] is not None and n["distance"] <= a
        else:
            ok = num and isinstance(a, (int, float)) and {"lt": v < a, "le": v <= a, "gt": v > a, "ge": v >= a}[o]
        if not ok:
            return False
    return True


def wt_items(inv, pattern):
    """Count of inventory items whose section matches the glob (backpack groups count their items)."""
    total = 0
    for group in ("slots", "belt", "backpack"):
        for it in (inv or {}).get(group) or []:
            if isinstance(it, dict) and fnmatch.fnmatch(str(it.get("section", "")).lower(), pattern.lower()):
                total += it.get("count", 1)
    return total


def wt_eval(n, snap, ctx):
    """Evaluate a node; polled leaves keep their last result when snap is None (no poll this round)."""
    k, sp = n["k"], n["spec"]
    if k in ("all", "any", "not"):
        r = [wt_eval(c, snap, ctx) for c in n["of"]]
        n["met"] = all(r) if k == "all" else any(r) if k == "any" else not r[0]
        return n["met"]
    inst = ctx["inst"]
    if k == "event":
        if "hit" not in n:
            where = sp.get("where") or {}
            for e in inst.events.select(since=ctx["ev_since"]):
                if not fnmatch.fnmatchcase(e.get("type", ""), sp["event"]):
                    continue
                d = e.get("data") or {}
                if all(f in d and (fnmatch.fnmatch(str(d[f]).lower(), w.lower()) if isinstance(w, str) else d[f] == w)
                       for f, w in where.items()):
                    n["hit"] = e
                    break
        n["met"] = "hit" in n
        n["value"] = event_out(n["hit"]) if n["met"] else None
        return n["met"]
    if k == "log":
        if "hit" not in n:
            items = inst.lines(since=n.get("pos", n["spec"].get("since", ctx["log_since"])))
            for seq, line, _ in items:
                if n["rx"].search(line):
                    n["hit"] = {"seq": seq, "line": line[:MAX_LINE_CHARS]}
                    break
            if items and "hit" not in n:
                n["pos"] = items[-1][0]
        n["met"] = "hit" in n
        n["value"] = n.get("hit")
        return n["met"]
    if k == "hung":
        n["met"], n["value"] = inst.hung, {"frame_stall_s": inst.stall_s} if inst.hung else None
        return n["met"]
    if k == "crash":
        c = inst.crash_event is not None or inst.crash is not None
        n["met"], n["value"] = c, wt_crash(inst) if c else None
        return n["met"]
    if snap is None:
        return n["met"]
    if k == "frames":
        f = (snap["hb"] or {}).get("frame")
        if f is not None:
            n.setdefault("base", f)
            n["value"] = f - n["base"]
        n["met"] = n["value"] is not None and n["value"] >= sp["frames"]
        return n["met"]
    if snap["error"] or snap["loading"] or inst.hung:
        n["met"], n["value"] = False, snap["error"] or ("loading" if snap["loading"] else "hung")
        return False
    st = snap["state"]
    if isinstance(st, Exception):
        st = None
        n["value"] = f"state.get failed: {snap['state']}"
    mode = (st or {}).get("mode")
    if k == "in_control":
        pr = snap["probe"]
        if st is None or mode in ("loading", "main_menu") or isinstance(pr, Exception) or not pr:
            n["met"] = False
            n["value"] = {"mode": mode, "probe": str(pr) if isinstance(pr, Exception) else None}
            return False
        r = (pr.get("results") or []) + ["", "", ""]
        n["value"] = {"mode": mode, "level": r[0] or None, "first_update": r[1] == "true", "alive": r[2] == "true"}
        n["met"] = bool(r[0]) and r[1] == "true" and r[2] == "true" and (sp["in_control"] in (True, r[0]))
        return n["met"]
    if k in ("loaded", "mode", "dialog", "sim_ms", "game_clock", "state", "has_item") and st is None:
        n["met"] = False
        return False
    if k == "loaded":
        n["met"], n["value"] = mode != "loading", mode
    elif k == "mode":
        want = sp["mode"] if isinstance(sp["mode"], list) else [sp["mode"]]
        n["value"] = mode if mode != "console" else f"console (under: {st.get('under_console')})"
        n["met"] = any(fnmatch.fnmatchcase(str(mode), w) for w in want)
    elif k == "dialog":
        d = wt_path(st, "ui.dialog")
        hit = isinstance(d, dict) and any(isinstance(x, str) and fnmatch.fnmatch(x.lower(), sp["dialog"].lower())
                                          for x in d.values())
        n["value"] = d
        n["met"] = hit if sp.get("open", True) else not hit
    elif k in ("sim_ms", "game_clock"):
        key = "time_global_ms" if k == "sim_ms" else "game_time_ms"
        t = wt_path(st, f"stats.{key}")
        if t is None:
            n["met"], n["value"] = False, "no level" if k == "game_clock" else None
            return False
        if "base" not in n or t < n["base"]:  # earlier save loaded: count from here
            n["base"] = t
            if k == "game_clock":
                n["need"] = (n["target_ms"] - t % 86400000) % 86400000
        if k == "sim_ms":
            n["value"] = t - n["base"]
            n["met"] = n["value"] >= sp["sim_ms"]
        else:
            n["value"] = (wt_path(st, "world.game_time") or "")[11:16] or None
            n["met"] = t - n["base"] >= n["need"]
    elif k == "state":
        n["value"] = wt_path(st, sp["state"])
        n["met"] = wt_ops(n, n["value"], snap)
    elif k == "has_item":
        n["value"] = wt_items(st.get("inventory"), sp["has_item"])
        n["met"] = wt_ops(n, n["value"], snap)
    elif k == "object":
        o, problem = wt_obj(snap["objects"].get(sp["object"]))
        if problem:
            n["met"], n["value"] = False, problem
            return False
        n["value"] = {f: o.get(f) for f in ("id", "kind", "section", "name", "alive", "position", "distance", "offline")
                      if f in o} if o else None
        how = sp.get("is")
        if "field" in sp or any(x in sp for x in WT_OPS):
            v = wt_path(o, sp["field"]) if "field" in sp else (o or {}).get("position")
            if "field" in sp:
                n["value"] = {"id": sp["object"], sp["field"]: v}
            n["met"] = o is not None and wt_ops(n, v, snap) and (how is None or wt_is(o, how))
        else:
            n["met"] = wt_is(o, how or "exists")
    elif k == "count":
        r = snap["counts"].get(json.dumps(n["params"], sort_keys=True))
        if isinstance(r, Exception) or not isinstance(r, dict) or not r.get("available"):
            n["met"] = False
            n["value"] = str(r) if isinstance(r, Exception) else (r or {}).get("reason")
            return False
        total = r.get("total", 0) + (r.get("offline_total") or 0)
        n["value"] = {"total": total, "counts": r.get("counts")}
        n["met"] = wt_ops(n, total, snap)
    elif k == "lua":
        r = snap["lua"].get(sp["lua"])
        if isinstance(r, Exception):
            if "script engine is not ready" in str(r):
                n["met"], n["value"] = False, "script engine not ready"
                return False
            raise WaitAbort(str(r))
        res = (r or {}).get("results") or []
        if res[:1] == ["E"]:
            raise WaitAbort(res[1] if len(res) > 1 else "error")
        n["value"] = res[2] if len(res) > 2 else None
        n["met"] = res[1:2] == ["1"]
    return n["met"]


def wt_is(o, how):
    if how == "gone":
        return o is None
    if o is None:
        return False
    return {"exists": True, "alive": o.get("alive") is True, "dead": o.get("alive") is False,
            "online": not o.get("offline"), "offline": bool(o.get("offline"))}[how]


def wt_crash(inst):
    c = inst.crash or {}
    ev = inst.crash_event or {}
    return {"reason": c.get("reason") or ev.get("reason"), "exit_code": inst.exit_code,
            "exit_code_name": c.get("exit_code_name") or (exit_code_name(inst.exit_code) if inst.dead else None),
            "stack": (c.get("stack") or compact_stack(ev.get("stack") or ""))[:3000] or None,
            "dump": c.get("dump") or ev.get("dump"), "note": "full record: game_last_crash"}


def wt_leaves(n, negated=False):
    if n["k"] in ("all", "any", "not"):
        return [x for c in n["of"] for x in wt_leaves(c, negated != (n["k"] == "not"))]
    return [{"cond": n["spec"], "met": n["met"], "value": n["value"],
             **({"distance": n["distance"]} if "distance" in n else {}), **({"negated": True} if negated else {})}]


def wait_run(inst, until, fail_on, timeout_s, since, log_since, poll_ms):
    if not 0 <= timeout_s <= 3600:
        raise ValueError("timeout_s must be 0..3600")
    if not 10 <= poll_ms <= 5000:
        raise ValueError("poll_ms must be 10..5000")
    root = wt_compile(until)
    fails = [wt_compile(f, "fail_on") for f in (fail_on or [])]
    nodes = [x for r in [root, *fails] for x in wt_nodes(r)]
    need = wt_needs(nodes)
    ctx = {"inst": inst, "ev_since": inst.events.last_seq if since is None else int(since),
           "log_since": inst.last_seq if log_since is None else int(log_since)}
    t0 = time.perf_counter()
    deadline, poll_s = t0 + timeout_s, poll_ms / 1000
    start = last = None
    polls, last_poll = 0, -1e9

    def result(met, failed=None, timed_out=False):
        end = last
        if not inst.dead and inst.crash_event is None and (end is None or time.perf_counter() - last_poll > poll_s):
            end = wt_clock(wt_poll(inst, {"state": None, "probe": False, "objects": (), "counts": {}, "lua": ()},
                                   1.0, True))
        out = {"instance": inst.id, "met": met, "failed_on": failed, "timed_out": timed_out,
               "elapsed_s": round(time.perf_counter() - t0, 3), "polls": polls}
        for key, name, scale in (("frame", "frames", 1), ("time_global_ms", "sim_ms", 1),
                                 ("game_time_ms", "game_s", 1000)):
            a, b = (start or {}).get(key), (end or {}).get(key)
            out[name] = None if a is None or b is None else round((b - a) / scale, 3) if scale > 1 else b - a
        out["mode"] = (end or {}).get("mode")
        out["conditions"] = wt_leaves(root)
        hits = [x["hit"]["seq"] for x in nodes if x["k"] == "event" and x["met"] and "hit" in x]
        out["event_seq"] = max(hits) if hits and (met or failed) else max(ctx["ev_since"], ctx.get("ev_end", 0))
        lh = [x["hit"]["seq"] for x in nodes if x["k"] == "log" and x["met"] and "hit" in x]
        out["log_seq"] = max(lh) if lh and (met or failed) else max(ctx["log_since"], ctx.get("log_end", 0))
        return out

    while True:
        crashed = inst.crash_event is not None or inst.dead
        snap = None
        if not crashed and (need["polled"] or start is None) and time.perf_counter() - last_poll >= poll_s:
            snap = wt_poll(inst, need, deadline - time.perf_counter(), start is None)
            polls, last_poll = polls + 1, time.perf_counter()
            clk = wt_clock(snap)
            if clk["frame"] is not None:
                last = {**(last or {}), **{k: v for k, v in clk.items() if v is not None}}
                if start is None:
                    start = clk
                    st = snap["state"] if isinstance(snap["state"], dict) else {}
                    if need["time"] and wt_path(st, "ui.mcp_paused"):
                        tg = inst.call("time.get", timeout=5)
                        if tg.get("applied") and not tg.get("steps_left"):
                            raise ValueError("the game is MCP-paused: simulation / game time does not advance, so a "
                                             "sim_ms or game_clock wait would never end; use game_step(ms=...) or "
                                             "game_time(pause=false) first")
        ctx["ev_end"], ctx["log_end"] = inst.events.last_seq, inst.last_seq
        try:
            failed = next((f for f in fails if wt_eval(f, snap, ctx)), None)
            met = failed is None and wt_eval(root, snap, ctx)
        except WaitAbort as e:
            return result(False, {"cond": "lua_error", "error": str(e)[:4000]})
        if failed:
            return result(False, {"cond": failed["spec"], "conditions": wt_leaves(failed)})
        if met:
            return result(True)
        if crashed:
            return result(False, {"cond": "crash" if inst.crash_event is not None or inst.crash else "exit",
                                  "value": wt_crash(inst)})
        now = time.perf_counter()
        if now >= deadline:
            return result(False, timed_out=True)
        wait = (poll_s - (now - last_poll)) if need["polled"] else 0.25
        with inst.wake:  # the reader notifies under this lock after updating the buffers: no missed wake-up
            if inst.events.last_seq == ctx["ev_end"] and inst.last_seq == ctx["log_end"]:
                inst.wake.wait(max(0.001, min(wait, deadline - now)))


@tool
async def game_wait(until: str | dict | list, instance: str | None = None, timeout_s: float = 30,
                    fail_on: list | None = None, since: int | None = None, log_since: int | None = None,
                    poll_ms: int = 50) -> dict:
    """Block until a condition holds (returns at once if it already does). Use it instead of sleeping or polling.
    until, as a string: "in_control" (level loaded, actor playable), "loaded", "main_menu", "hung", "crash", or an
    event type / glob ("actor_died", "task_*").
    until, as an object: {"in_control": "l01_escape"} | {"mode": "dialog"} | {"dialog": "talk"} ("open": false =
    closed) | {"event": "task_given", "where": {"id": "x"}} | {"log": "regex", "ignore_case": true} | {"sim_ms":
    5000} | {"frames": 100} | {"game_clock": "21:00"} | {"state": "actor.health", "lt": 0.5} (any game_state path,
    also "tasks.<id>.stage") | {"has_item": "wpn_ak74*"} | {"object": id, "is": "dead"} (exists, gone, alive, dead,
    online, offline) | {"object": id, "within": 5, "of": "actor"} | {"object": id, "field": "health", "lt": 0.3} |
    {"count": {"kinds": ["monster"], "radius": 50, "hostile_only": true}, "eq": 0} | {"lua": "db.actor:money() >
    5000"} | {"all": [...]} / {"any": [...]} / {"not": {...}}; a plain list = all.
    Ops: eq ne lt le gt ge in match exists; no op = truthy.
    fail_on: conditions that abort the wait, e.g. ["script_error", "actor_died"]; a crash always ends it. timeout_s
    (30, max 3600): a timeout is a normal result (met false, timed_out true).
    Events and log lines count only from the start of the call. To catch one caused by an action issued just before,
    pass since (event seq) / log_since (log_tail last_seq) taken before the action; chain waits with the returned
    event_seq / log_seq.
    Result: met, failed_on, timed_out, conditions [{cond, met, value}]."""
    inst = get(instance)
    return await run(wait_run, inst, until, fail_on, float(timeout_s), since, log_since, int(poll_ms))


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
