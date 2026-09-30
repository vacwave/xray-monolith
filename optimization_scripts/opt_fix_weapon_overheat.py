r"""Fix the per-frame Lua error in item_weapon.script update_overheat (seen 121x in your log:
"Incorrect bone_id provided for wpn_toz34 ... fallback to root bone" + full traceback each frame).

Also caches the 3 ini reads that run EVERY FRAME while a barrel is hot.

Non-destructive: copies the currently-winning item_weapon.script into a NEW MO2 mod folder
"zzz_perf_overheat_fix" (enable it at the bottom of the left pane so it wins). Originals untouched.
Usage: python opt_fix_weapon_overheat.py
"""
from _common import MODS_DIR, virtual_gamedata, winner

OUT = MODS_DIR / "zzz_perf_overheat_fix" / "gamedata" / "scripts" / "item_weapon.script"

OLD = '''			local hud = utils_data.read_from_ini(nil,wpn:section(),"hud","string",nil)
			local fire_bone = utils_data.read_from_ini(nil,hud,"fire_bone","string",nil) or "wpn_body"
			local offset = utils_data.read_from_ini(nil,hud,"fire_point","string",nil) or VEC_ZERO
			offset = offset and utils_data.string_to_vector(offset)
			smoke:move_to( wpn:bone_position(fire_bone), offset )'''

NEW = '''			-- [perf fix] cache per section + skip invalid bones (was logging a traceback every frame)
			local sec = wpn:section()
			local c = _perf_smoke_cache[sec]
			if not c then
				local hud = utils_data.read_from_ini(nil,sec,"hud","string",nil)
				local fire_bone = hud and utils_data.read_from_ini(nil,hud,"fire_bone","string",nil) or "wpn_body"
				local offset = hud and utils_data.read_from_ini(nil,hud,"fire_point","string",nil)
				offset = offset and utils_data.string_to_vector(offset) or VEC_ZERO
				local ok = true
				if wpn.bone_id then
					local id = wpn:bone_id(fire_bone)
					ok = id ~= nil and id ~= 65535
				end
				c = { bone = fire_bone, offset = offset, ok = ok }
				_perf_smoke_cache[sec] = c
			end
			if c.ok then
				smoke:move_to( wpn:bone_position(c.bone), c.offset )
			else
				smoke:move_to( wpn:position(), c.offset )
			end'''

vfs = virtual_gamedata()
src = winner("scripts/item_weapon.script", vfs)
print("Winning item_weapon.script:", src)
text = src.read_text(encoding="cp1251", errors="ignore").replace("\r\n", "\n")
if OLD not in text:
    raise SystemExit("Target block not found (script changed?) - nothing written.")
text = text.replace(OLD, NEW, 1)
text = text.replace("function update_overheat()", "_perf_smoke_cache = {}\nfunction update_overheat()", 1)
# same for the play_at_pos call which uses wpn_body unconditionally
text = text.replace('smoke:play_at_pos( wpn:bone_position("wpn_body") )', 'smoke:play_at_pos( wpn:position() )', 1)
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(text, encoding="cp1251", errors="ignore")
print("Wrote", OUT, "\nEnable 'zzz_perf_overheat_fix' in MO2 at the lowest position (highest priority).")
