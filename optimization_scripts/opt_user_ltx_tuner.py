r"""Apply performance-oriented engine settings to Anomaly's user.ltx and commandline.txt.
Always writes a timestamped backup first. Dry-run by default; pass --apply to write.

Findings from your current config that this addresses:
  * commandline.txt has -dbg  -> debug mode: extra checks/logging, noticeably slower. Removed.
  * r__detail_radius 160      -> grass draw radius is very expensive. -> 100
  * r__tex_evict_enabled 0    -> VRAM fills up on long sessions -> stutter. Enabled.
  * r__clear_*_on_unload off  -> memory bloat across level changes. Enabled.
  * r2_volumetric_lights on   -> heavy with many torches/anomalies. (only in --aggressive)
  * r3_dynamic_wet_surfaces   -> extra shadow pass while raining. (only in --aggressive)
  * mt_* 0                    -> enable remaining multithreading toggles.
Usage: python opt_user_ltx_tuner.py [--apply] [--aggressive]
"""
import re, shutil, sys, time
from _common import ANOMALY_DIR

USER = ANOMALY_DIR / "appdata" / "user.ltx"
CMD = ANOMALY_DIR / "commandline.txt"

SAFE = {
    "r__detail_radius": "100",
    "r__tex_evict_enabled": "1",
    "r__clear_models_on_unload": "on",
    "r__clear_resources_on_unload": "on",
    "r__fast_details_update": "on",
    "r__optimize_calculate_bones": "1",
    "r__optimize_dynamic_geom": "1",
    "r__optimize_static_geom": "1",
    "r__optimize_shadow_geom": "on",
    "r__optimize_torch": "1",
    "mt_level_call": "1",
    "mt_task_manager": "1",
    "mt_ui": "1",
    "mt_particles": "on",
    "mt_bullets": "on",
    "r__use_precompiled_shaders": "on",
    "r__tf_mipbias": "0.",      # negative bias = more texture bandwidth
}
AGGRESSIVE = {
    "r2_volumetric_lights": "off",
    "r3_dynamic_wet_surfaces": "off",
    "r__detail_radius": "80",
    "r__detail_density": "0.4",   # higher value = sparser grass
    "r__geometry_lod": "1.0",
    "r2_sun_quality": "st_opt_low",
    "r__tf_aniso": "8",
    "r__wallmark_ttl": "20.",
    "ai_use_torch_dynamic_lights": "off",
    "r2_dof_enable": "off",
}

apply = "--apply" in sys.argv
wanted = dict(SAFE, **(AGGRESSIVE if "--aggressive" in sys.argv else {}))
stamp = time.strftime("%Y%m%d_%H%M%S")

lines = USER.read_text(encoding="cp1251").splitlines()
seen = set()
for i, l in enumerate(lines):
    k = l.split(" ", 1)[0]
    if k in wanted:
        seen.add(k)
        new = f"{k} {wanted[k]}"
        if l.strip() != new:
            print(f"  {l.strip():45} -> {new}")
            lines[i] = new
for k in wanted.keys() - seen:
    print(f"  (missing, not added: {k} - this engine build may not support it)")

cmd = CMD.read_text().strip() if CMD.exists() else ""
new_cmd = re.sub(r"\s*-dbg\b", "", cmd).strip()
if new_cmd != cmd:
    print(f"  commandline.txt: '{cmd}' -> '{new_cmd}'")

if apply:
    shutil.copy2(USER, USER.with_name(f"user.ltx.bak_{stamp}"))
    USER.write_text("\n".join(lines) + "\n", encoding="cp1251")
    if new_cmd != cmd:
        shutil.copy2(CMD, CMD.with_name(f"commandline.txt.bak_{stamp}"))
        CMD.write_text(new_cmd)
    print("Applied. Backups saved with suffix", stamp)
else:
    print("\nDry run. Re-run with --apply to write (backups are made automatically).")
