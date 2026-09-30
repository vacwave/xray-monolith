"""Shared helpers for GAMMA optimization scripts. Read-only helpers; nothing here modifies files."""
import os
from pathlib import Path

GAMMA_DIR = Path(os.environ.get("GAMMA_DIR", r"C:\GAMMA"))
ANOMALY_DIR = Path(os.environ.get("ANOMALY_DIR", r"C:\Anomaly"))
MODS_DIR = GAMMA_DIR / "mods"
PROFILE = os.environ.get("GAMMA_PROFILE", "G.A.M.M.A")


def enabled_mods(profile=PROFILE):
    """Enabled mods in load order (lowest priority first), as MO2 applies them."""
    ml = GAMMA_DIR / "profiles" / profile / "modlist.txt"
    names = []
    for line in ml.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith("+"):
            names.append(line[1:].strip())
    names.reverse()  # modlist.txt is highest priority first
    return [MODS_DIR / n for n in names if (MODS_DIR / n).is_dir()]


def virtual_gamedata(profile=PROFILE):
    """Map relative gamedata path (lowercase) -> list of providing files, winner last."""
    vfs = {}
    for mod in enabled_mods(profile):
        gd = mod / "gamedata"
        if not gd.is_dir():
            continue
        for root, _, files in os.walk(gd):
            for f in files:
                p = Path(root) / f
                rel = str(p.relative_to(gd)).lower().replace("\\", "/")
                vfs.setdefault(rel, []).append(p)
    return vfs


def winner(rel, vfs):
    return vfs[rel][-1] if rel in vfs else None
