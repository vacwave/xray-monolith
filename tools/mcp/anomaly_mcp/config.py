"""Settings from environment variables, read at call time so tests can override them."""

import os
from pathlib import Path

HOST = "127.0.0.1"


def anomaly_dir() -> Path:
    return Path(os.environ.get("ANOMALY_DIR", r"C:\Anomaly"))


def port() -> int:
    return int(os.environ.get("ANOMALY_MCP_PORT", "28015"))


def exe_name() -> str:
    return os.environ.get("ANOMALY_EXE", "AnomalyDX11AVX.exe")
