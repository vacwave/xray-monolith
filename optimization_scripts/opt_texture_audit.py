r"""Audit the DDS textures that actually win in the virtual file system (enabled mods only).
Flags: missing mipmaps (big stutter/aliasing + bandwidth), uncompressed formats, oversized (>limit).
Optional: --fix <texconv.exe> writes optimized copies (BC-compressed, mipmapped, capped size) into a
NEW mod folder "zzz_perf_textures" - originals are never touched. Get texconv from
https://github.com/microsoft/DirectXTex/releases
Usage: python opt_texture_audit.py [--max 2048] [--fix C:\path\texconv.exe] [--csv out.csv]
Tip: skip ui/ and hud-scope textures when fixing if you see issues (--skip ui/,wpn/).
"""
import argparse, csv, struct, subprocess
from _common import MODS_DIR, virtual_gamedata

ap = argparse.ArgumentParser()
ap.add_argument("--max", type=int, default=2048)
ap.add_argument("--fix")
ap.add_argument("--csv", default="texture_audit.csv")
ap.add_argument("--skip", default="ui/,fonts/,shaders/,sky/,wpn/scope_reticles/")
a = ap.parse_args()
skip = tuple(s for s in a.skip.split(",") if s)

def dds_info(p):
    with open(p, "rb") as f:
        h = f.read(148)
    if len(h) < 128 or h[:4] != b"DDS ":
        return None
    height, width = struct.unpack_from("<II", h, 12)
    mips = struct.unpack_from("<I", h, 28)[0] or 1
    pf_flags = struct.unpack_from("<I", h, 80)[0]
    fourcc = h[84:88]
    if fourcc == b"DX10" and len(h) >= 132:
        fmt = f"DXGI{struct.unpack_from('<I', h, 128)[0]}"
    elif pf_flags & 0x4:
        fmt = fourcc.decode("ascii", "replace")
    else:
        fmt = f"RGB{struct.unpack_from('<I', h, 88)[0]}"  # uncompressed, bitcount
    return width, height, mips, fmt

vfs = virtual_gamedata()
rows, total_bad_mb = [], 0.0
for rel, provs in vfs.items():
    if not rel.startswith("textures/") or not rel.endswith(".dds"):
        continue
    p = provs[-1]
    info = dds_info(p)
    if not info:
        continue
    w, h, mips, fmt = info
    issues = []
    if mips == 1 and max(w, h) > 64 and not rel.startswith(("textures/ui/", "textures/sky/")):
        issues.append("no_mips")
    if fmt.startswith("RGB") and max(w, h) > 256:
        issues.append("uncompressed")
    if max(w, h) > a.max:
        issues.append(f">{a.max}")
    if issues:
        mb = p.stat().st_size / 2**20
        total_bad_mb += mb
        rows.append((rel, w, h, mips, fmt, round(mb, 2), "|".join(issues), p.parent.as_posix()))

rows.sort(key=lambda r: -r[5])
with open(a.csv, "w", newline="", encoding="utf-8") as f:
    csv.writer(f).writerows([("path", "w", "h", "mips", "fmt", "MB", "issues", "source")] + rows)
print(f"{len(rows)} problem textures, {total_bad_mb:.0f} MB. Details: {a.csv}")
for r in rows[:25]:
    print(f"{r[5]:8.1f}MB {r[1]}x{r[2]} mips={r[3]} {r[4]:8} {r[6]:22} {r[0]}")

if a.fix:
    out_root = MODS_DIR / "zzz_perf_textures" / "gamedata"
    for rel, w, h, mips, fmt, mb, issues, _ in rows:
        if rel[len("textures/"):].startswith(skip):
            continue
        src = vfs[rel][-1]
        dst = out_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        cmd = [a.fix, "-nologo", "-y", "-m", "0", "-o", str(dst.parent),
               "-f", "BC3_UNORM",  # DXT5: safe for X-Ray diffuse (alpha) and bump maps
               "-w", str(min(w, a.max)), "-h", str(min(h, a.max)), str(src)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        print(("OK  " if r.returncode == 0 else "ERR ") + rel)
    print("Done. Enable 'zzz_perf_textures' at the bottom of MO2 (highest priority). Test in-game.")
