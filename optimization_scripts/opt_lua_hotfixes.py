r"""Bulk-patch known Lua CPU hot-spots in ENABLED mods into a new override mod "zzz_perf_lua_fixes".
Each patch copies the currently-winning script, applies a small, targeted text change, and writes
it to the override mod. Originals are never touched. Patches whose target text isn't found are
skipped (reported), so this is safe to re-run after GAMMA updates.

Patches:
  1. SSS23 ssfx_wetness.script  - console command executed EVERY FRAME -> only when value changes.
  2. grok_bo.script (CQC)       - ini_file(model ltx) parsed on EVERY NPC HIT -> memoized.
  3. grok_nes.script / grok_no_north_faction_in_south.script - same ini_file memoization.
  4. FDDA lam2.script           - log() args built every frame even with debug off -> guarded.
Usage: python opt_lua_hotfixes.py [--dry-run]
Then enable "zzz_perf_lua_fixes" at the very bottom of MO2's left pane (highest priority).
"""
import re, sys
from _common import MODS_DIR, virtual_gamedata, winner

OUT = MODS_DIR / "zzz_perf_lua_fixes" / "gamedata"
dry = "--dry-run" in sys.argv


def ssfx(t):
    old = 'get_console():execute("ssfx_gloss_factor " .. ( Wetness_gloss * 0.96 ) )'
    new = ('local _v = Wetness_gloss * 0.96\n'
           '\t\tif not _perf_last_gloss or math.abs(_v - _perf_last_gloss) > 0.005 then  -- [perf fix]\n'
           '\t\t\t_perf_last_gloss = _v\n'
           '\t\t\tget_console():execute("ssfx_gloss_factor " .. _v)\n'
           '\t\tend')
    return t.replace(old, new, 1) if old in t else None


def memo_ini(t):
    # ini_file(<var>) where var is a model/armor path -> cached lookup
    pat = re.compile(r"\bini_file\(\s*(npc_armor_path|npc_model_path|model_path)\s*\)")
    if not pat.search(t):
        return None
    t = pat.sub(r"_perf_ini(\1)", t)
    helper = ("-- [perf fix] memoized ini_file for per-hit/per-update lookups\n"
              "local _perf_ini_cache = {}\n"
              "local function _perf_ini(p)\n"
              "\tlocal i = _perf_ini_cache[p]\n"
              "\tif not i then i = ini_file(p); _perf_ini_cache[p] = i end\n"
              "\treturn i\nend\n\n")
    return helper + t


def lam2(t):
    m = re.search(r'\n(\s*)log\("\[CORE\] LAM2 Update:', t)
    if not m:
        return None
    start = m.start() + 1 + len(m[1])
    i, depth = t.index("(", start), 0
    while True:  # find matching paren (strings here contain no parens)
        c = t[i]
        depth += c == "("
        depth -= c == ")"
        i += 1
        if depth == 0:
            break
    return t[:start] + "if b_is_debug_enable then " + t[start:i] + " end" + t[i:]


PATCHES = [
    ("scripts/ssfx_wetness.script", ssfx),
    ("scripts/grok_bo.script", memo_ini),
    ("scripts/grok_nes.script", memo_ini),
    ("scripts/grok_no_north_faction_in_south.script", memo_ini),
    ("scripts/lam2.script", lam2),
]

vfs = virtual_gamedata()
for rel, fn in PATCHES:
    src = winner(rel, vfs)
    if not src:
        print(f"SKIP {rel}: not in enabled mods")
        continue
    text = src.read_text(encoding="cp1251", errors="ignore")
    new = fn(text)
    if new is None:
        print(f"SKIP {rel}: target code not found in {src.parent.parent.parent.name}")
        continue
    print(f"PATCH {rel}  (from {src.parent.parent.parent.name})")
    if not dry:
        dst = OUT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(new, encoding="cp1251", errors="ignore")
print("Dry run - nothing written." if dry else f"Written to {OUT.parent}")
