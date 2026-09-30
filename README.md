# Stalker GAMMA Optimization Scripts

Python scripts that reduce stutter and raise FPS in S.T.A.L.K.E.R. Anomaly / GAMMA.

None of them edit or delete existing mod files. Fixes are written into **new** Mod Organizer 2 mods named
`zzz_perf_*`, and engine settings changes are backed up with a timestamp first.

## Requirements
- Python 3.10+
- A standard GAMMA install (Mod Organizer 2 + Anomaly)
- Optional: [texconv.exe](https://github.com/microsoft/DirectXTex/releases) for texture compression

## Installation
Place the `optimization_scripts` folder **inside your GAMMA folder** (the folder that contains
`ModOrganizer.exe`, `mods\` and `profiles\`):

```
<GAMMA>\
  ModOrganizer.exe
  mods\
  profiles\
  optimization_scripts\   <-- here
<Anomaly>\
  bin\
  appdata\
```

The scripts assume `C:\GAMMA` and `C:\Anomaly` by default. If your folders are elsewhere, or you use a
different MO2 profile, set these environment variables before running:

```
set GAMMA_DIR=D:\Games\GAMMA
set ANOMALY_DIR=D:\Games\Anomaly
set GAMMA_PROFILE=G.A.M.M.A
```

Close the game and Mod Organizer before running the scripts. Run them from inside the folder:
`python <script>.py`

## Scripts (biggest win first)

| Script | What it does |
|---|---|
| `opt_alife_tuner.py [balanced\|fps\|smooth] [--dry-run]` | The mod "Turn this on if you stutter" sets the NPC simulation range to 1500 m, overriding GAMMA's 450 m. Everything in that range is fully simulated, which costs a lot of CPU. This writes a preset (700 m by default) to `zzz_perf_alife`. |
| `opt_user_ltx_tuner.py [--apply] [--aggressive]` | Previews by default. With `--apply` it removes `-dbg` from `commandline.txt`, lowers grass draw radius, enables freeing unused VRAM and model/resource cleanup on level change, and turns on multithreading options. `--aggressive` also disables volumetric lights, wet surfaces and depth of field, among others. Backs up both files first. |
| `opt_fix_weapon_overheat.py` | Fixes a Lua error in `item_weapon.script` that fires every frame and writes a full error trace to the log each time (for weapons without the expected bone, e.g. TOZ-34). Output goes to `zzz_perf_overheat_fix`. |
| `opt_lua_hotfixes.py [--dry-run]` | Patches known Lua hot spots into `zzz_perf_lua_fixes`: Screen Space Shaders' per-frame console command, model files parsed from disk on every NPC hit (GBOOBS/CQC, No Exos in the South), and FDDA building debug log text every frame. Patches that no longer match are skipped. |
| `opt_texture_audit.py [--max N] [--fix texconv.exe] [--csv file]` | Lists textures without mipmaps, uncompressed or larger than N. With `--fix`, it writes compressed copies to `zzz_perf_textures`. Use `--max 16384` to compress only, without resizing. It skips UI, sky and scope reticle textures by default. Compression is lossy, so check the results in game. |

## After running
1. In MO2, enable each generated `zzz_perf_*` mod and drag it to the **bottom** of the left pane, which is the highest priority.
2. Delete `<Anomaly>\appdata\shaders_cache`. The game rebuilds it.
3. Load a save.

**To undo:** untick or delete the `zzz_perf_*` mods, and restore the `user.ltx.bak_*` / `commandline.txt.bak_*` backups.

**Note:** if the game becomes unstable after `opt_user_ltx_tuner.py`, set `mt_level_call` and
`mt_task_manager` back to `0` in `user.ltx`.

## Ideas not yet scripted
- Hide junk files in mods (`.bak`, `.txt`, images) with `.mohidden`
- Pack large loose-file mods into `.db` archives
- Lower `max_particles` in `particles.xr` and cap weather `far_plane` and sun shafts
- Repair ogg comments on sound files
- Add throttles to Dynamic Anomalies, disguise and RF scanner scripts that loop over all 65534 object IDs
