r"""Resolve the A-Life (NPC simulation) config conflict and write a balanced override.

Your install: "Turn this on if you stutter" (enabled) beats "G.A.M.M.A. Alife optimization":
  switch_distance 1500, auto_switch false, schedule 1/1 ms, objects_per_update 20
  -> everything within 1.5 km is fully simulated online = big constant CPU cost / low FPS.
This writes the winning alife.ltx with a preset into a NEW mod "zzz_perf_alife" (enable at the bottom
of MO2). Nothing else is changed. Alternatively, just untick "Turn this on if you stutter" in MO2.
Usage: python opt_alife_tuner.py [balanced|fps|smooth] [--dry-run]
  fps      = GAMMA optimization values (450 m) - most FPS, may hitch when NPCs spawn in
  balanced = 700 m, auto switch on (default)
  smooth   = 1000 m - fewer spawn-in hitches, lower FPS
"""
import re, sys
from _common import MODS_DIR, virtual_gamedata

PRESETS = {
    "fps":      dict(switch_distance=450, auto_switch_distance_normal=450, auto_switch="true",
                     schedule_min=170, schedule_max=470, process_time=4320, objects_per_update=3),
    "balanced": dict(switch_distance=700, auto_switch_distance_normal=700, auto_switch="true",
                     schedule_min=100, schedule_max=300, process_time=3000, objects_per_update=5),
    "smooth":   dict(switch_distance=1000, auto_switch_distance_normal=1000, auto_switch="true",
                     schedule_min=50, schedule_max=150, process_time=2000, objects_per_update=10),
}
preset = next((a for a in sys.argv[1:] if a in PRESETS), "balanced")
vfs = virtual_gamedata()
provs = vfs["configs/alife.ltx"]
print("alife.ltx providers (lowest -> highest priority):")
for p in provs:
    print("  ", p.parents[2].name)
text = provs[-1].read_text(encoding="cp1251", errors="ignore")
for k, v in PRESETS[preset].items():
    text, n = re.subn(rf"(?m)^(\s*{k}\s*=\s*)[^\s;]+", rf"\g<1>{v}", text)
    print(f"  {k} = {v}" + ("" if n else "   (key not found!)"))
if "--dry-run" not in sys.argv:
    out = MODS_DIR / "zzz_perf_alife" / "gamedata" / "configs" / "alife.ltx"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="cp1251")
    print("Wrote", out, "- enable 'zzz_perf_alife' in MO2 (lowest in list). Needs a save reload.")
