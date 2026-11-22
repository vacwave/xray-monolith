# Anomaly MCP: tool reference

Full argument and result details per tool. The tool descriptions the agent sees are short on purpose; read the section you need here.

## game_launch

`game_launch(exe: str | None=None, game_root: str | None=None, show_window: bool=False, new_game: bool=False, extra_args: str | None=None, timeout: float=180, full_dumps: bool=False, hang_timeout: float | None=None, load_timeout: float | None=None, load: str | None=None, keep_shader_cache: bool=False, debug: bool=True, cheats: bool=True, god: bool=True, infinite_mag: bool=True, norecoil: bool=True)`

Launch a game instance (silent/minimized unless show_window) and connect to it.

debug: pass -dbg (debug mode: script error details, debug console commands and menus); default on.
Cheats (on by default, set once via the console; they hold across loads, new games and level changes): god
(g_god), infinite_mag (g_infinite_mag: the actor's magazine never empties; not the grenade launcher / RPG),
norecoil (g_norecoil: no camera recoil kick); cheats=False turns all three off. Result cheats {cvar: on};
game_state actor.cheats shows the live state. g_unlimitedammo (infinite reserve) is not touched here
(the slot's user.ltx template turns it on).
game_root: default C:\AnomalyMO2 (the testers' MO2: C:\Anomaly with their mods); C:\Anomaly = plain vanilla;
C:\GAMMA counts against the GAMMA cap. An MO2 folder (ModOrganizer.exe, no fsgame.ltx) is started through its own
MO2 (`run`, MO2's selected profile, active mods and gamePath), which is killed once the game runs; refused while
any MO2 runs. exe: default = <game path>\bin_mcp\AnomalyDX11AVX.exe (the deployed MCP
build; <gamePath> of the MO2 ini for an MO2 root). Result: game_root, exe, mo2 {profile, game_path, mods_active,
mods_total} (null without MO2).
The game saves its options (axr_options.ltx) into the slot's appdata, never into the game's. new_game: start a new game straight into a level. extra_args: more engine args.
GAMMA: every launch writes mcp/templates/options_overrides_gamma.ltx into that copy (result `option_overrides`); with
overrides, new_game starts the game from the main menu after they are applied (no `-start` in args).
timeout: seconds to wait for the first engine.info reply after connecting (a new_game level load blocks it).
full_dumps: Windows Error Reporting writes full dumps (GBs) instead of mid dumps; the setting is global per exe
name, so the last launch wins for all running instances. hang_timeout / load_timeout: seconds of stalled frames
before the instance is marked hung (defaults 30 / 600 while a level loads).
Refused when the machine-wide instance cap is reached. If the engine never opens its port, the game is dumped
and killed, and the error names the dump. load: start straight into that save (it must be in the slot's
savedgames, e.g. imported earlier by game_saves); returns once the actor is in control (result.load, as
game_load). keep_shader_cache: by default the slot's shaders_cache is deleted before every launch (and
game_restart) and the game recompiles it; true keeps it (a new slot is seeded from the game root's cache).

## game_restart

`game_restart(instance: str | None=None, new_game: bool | None=None)`

Stop the instance if it still runs (a hung one is killed), then relaunch it with the same exe, game root and
args into the same slot and appdata. new_game overrides the original launch's choice when given.

## game_last_crash

`game_last_crash(instance: str | None=None, lines: int=30)`

The instance's crash record: plain-words reason (FATAL description/expression/file:line, or exception code +
module), exit code name, engine stack, dump paths (engine .mdmp, WER dump by pid, in .mcp-run/<id>/dumps),
the .log path and tail, and the last buffered lines. crashed=false if it has not crashed.

## game_dump

`game_dump(instance: str | None=None)`

Write a minidump of the running game from the supervisor (all thread stacks; use it on a hung instance).
Saved in .mcp-run/<id>/dumps; old dumps beyond the retention limits are deleted and listed.

## game_attach

`game_attach(port: int | None=None, appdata: str | None=None)`

Attach to a running game started with -mcp_port, by TCP port or by its -appdata dir (reads mcp_port there).
Attached games take no slot and are not killed when the supervisor exits.

## game_status

`game_status(instance: str | None=None, lines: int=20)`

pid, alive, port, crashed (see game_last_crash), hung (frames stalled past the threshold; see game_dump),
engine.info, log rate (lines/s) and the last log lines of an instance.

## game_list

`game_list()`

Instances known to this supervisor, plus the machine-wide caps.

## game_stop

`game_stop(instance: str | None=None)`

Quit the game gracefully (console 'quit'), kill it after 30 s, and release its slot. The log buffer stays.

## console_exec

`console_exec(cmd: str, instance: str | None=None, timeout: float=30)`

Run an engine console command; returns the log lines it printed (repeats collapsed, size capped).

## lua_exec

`lua_exec(code: str, instance: str | None=None, timeout: float=30)`

Run Lua in the game's script engine (main thread). An expression list returns its values as strings
(e.g. 'level.name()'); statements run as a chunk. print() output and other log lines come back in lines.
Errors are returned as tool errors with the traceback.

## log_tail

`log_tail(instance: str | None=None, n: int=100, pattern: str | None=None, since: int | None=None, ignore_case: bool=False, grep: str | None=None)`

Log lines from the supervisor buffer (20k lines; survives reconnects and game exit).

Without since: the newest n (max 1000). With since (a seq marker): lines with seq > since, oldest first, the first
n of them; `truncated` = matches left out, continue with since=next_since. Lines the buffer no longer holds are
fetched from the engine ring (10k) while the game runs; `lost` counts lines neither holds.
pattern: regex (re.search; ignore_case for case-insensitive; `grep` is an alias) -> `matches` [{seq, line}];
without a pattern -> `lines` (repeats collapsed). last_seq = the newest seq buffered now: pass it as since (here
or as game_wait log_since) to see only what comes after. Grep after an action: m = log_tail(n=1).last_seq; act;
log_tail(pattern=..., since=m).

## game_events

`game_events(instance: str | None=None, since: int | None=None, types: list[str] | None=None, limit: int=100)`

Structured game events from the supervisor buffer (survives reconnects and game exit), oldest first.
Types: script_error (data: kind, fatal, message, file, line, script, traceback, count, since_last), level_loaded
(level, previous), level_unloaded, actor_spawned, actor_died (killer_id/name/section), game_saved, game_loaded,
menu_opened, menu_closed, task_given, task_completed, task_failed, task_in_progress (id, title, title_id),
task_stage (id, title, old_stage, stage, target: object brief or null; polled once per second), info_given /
info_removed (name), death (victim {id,name,section,char_name,community,story_id}, killer brief or null, weapon,
hit_type, bone, level, position), hit (attacker, victim, count, damage, health_before/after, hit_type, bone, weapon;
first hit per pair at once, then one summary per 1 s), combat (state enter/switch/leave, npc, enemy, previous),
relation (kind personal (actor only)/community_goodwill/community_relation/bulk, from/to or community/to_community,
old, new, count), guard_fight (idle guard, see game_guard: phase start {id, name, section, distance, attacking} /
end {id, name, section, shots, hits, killed, ms, end}).
since: only events with seq > since (page with the returned last_seq); without it the newest `limit` are returned.
types: type names or fnmatch patterns (e.g. ['task_*']). limit: max 500. Script errors repeating within 5 s are
coalesced: count = total so far, since_last = occurrences this event stands for. Fatal errors (kind callback/
luabind/cast/panic) end the game; see game_last_crash.

## game_teleport

`game_teleport(instance: str | None=None, faction: str | None=None, level: str | None=None, position: list[float] | None=None, cvars: list[str] | None=None, snap: bool=True, timeout: float=600)`

Get a playable session on a map: returns once the actor is in control there (or errors/times out).

Main menu: starts a new game as faction (default stalker) with its stock kit (PDA included) on level (default:
the faction's start). The stock start location on that level is used, else a smart terrain's graph vertex;
two loads (fake_start, then the level). In game: other level -> level change; position [x, y, z] -> moves the
actor there (snapped to the nearest AI node if off the map; snap=False: the exact point, result `exact`
{on_ai_map, node_dy} - off the map it may be inside geometry or fall). faction in game only switches the actor's
community in place. cvars: console commands applied after arrival (e.g. ["g_god on"]).
Unknown level/faction -> error listing the valid ones. Result: level, position, faction, load_s, notes.

## game_changelevel

`game_changelevel(instance: str | None=None, changer: str | int='', override: bool=False, timeout: float=600)`

Go through an adjacent-level changer on this level as if the actor walked into it and confirmed (no need to
stand in its trigger): the changer's own destination (game/level vertex, position, angles), stock ChangeLevel
(autosave first). changer: object id, object name, or destination level name (several -> the nearest enabled; noted).
No match -> {error: NoChanger, changers: [id, name, dest_level, enabled, distance]}. A disabled changer ->
{error: LevelLocked, reason, changer} without changing level; override=True changes anyway. Returns once the
actor is in control on the new level: level, position, from_level, changer used, load_s.

## game_save

`game_save(name: str, instance: str | None=None, timeout: float=60)`

Save the running game as `name` in the instance's own appdata (savedgames); returns once the save is on disk:
file, size, parts (scop/scoc/dds), level, position, money, health, save_s. Name: letters, digits, _ - . (others
become _; renamed is set then); an existing save of that name is overwritten. Uses the console `save` command, so
the UI-only save vetoes (campfire-only saves, Agony/timer conditions, emission/combat checks) do not apply.
Errors: main menu without a game, loading, actor dead.

## game_load

`game_load(name: str, instance: str | None=None, timeout: float=600)`

Load save `name` (from game_saves) from the main menu or a running game, also while the actor is dead; returns
once the actor is in control on the loaded level (same check as game_teleport): level, position, money, health,
load_s, via. Missing save -> error listing the saves. If the game dies during the load the result has loaded=false,
the crash record and, when the log shows skipped objects ("Can't create entity"), missing_sections: the save was
made with mods whose sections this game does not have. Timeout -> error; the load may still finish later.

## game_saves

`game_saves(instance: str | None=None, delete: str | None=None, import_from: str | None=None, import_as: str | None=None, details: bool=True)`

List the instance's saves, newest first: name, size (all parts), mtime, and with details (game running, not
loading) level, game_time, actor_health read from the save. import_from: a .scop path (e.g.
C:\Anomaly\appdata\savedgames\1.scop) copied with its .scoc/.dds into the instance's savedgames, optionally as
import_as (the source is never modified). delete: removes that save (all parts) from the instance's own
savedgames only.

## game_state

`game_state(instance: str | None=None, sections: list[str] | None=None)`

Where am I and what is the game doing: one cheap structured snapshot (no screenshot / lua_exec needed).

mode: main_menu (no game), loading, in_game, in_game_menu (ESC menu over a game), dialog (a UI window owns input,
see ui.dialog), dead, paused, tutorial, console (console open; under_console = the mode beneath), hung.
ui: main_menu, console, paused, cursor, cursor_pos {x, y (UI units 1024x768), visible, mcp_owned}, input_receiver (engine class on top of the input stack), actor_control
(the actor takes movement/fire input now), dialog / menu_dialog {kind: actor_menu (menu_mode inventory / trade /
upgrade / loot_body / loot_box) | pda | talk | message_box | script | other, class, script_class (Lua class of
script windows, e.g. sleep), partner_id/name/section}, tutorial, input_disabled.
In a level: actor {id, name, position, yaw/pitch (deg; yaw 0 = +z, +90 = +x, i.e. + = turned right like
game_input look; pitch + = up), level_vertex, game_vertex, alive, health, stamina, radiation, bleeding,
psy_health, satiety, thirst/sleepiness (0..1, Anomaly scripts), move (stand/run/walk/sprint/crouch/crouch_low/
jump/landing/climb), speed (m/s), weight/max_weight/max_walk_weight, money, community, rank, reputation, god_mode,
outfit, helmet}; hands {active_slot, item {section, name, condition, state (idle/showing/hiding/hidden/fire/
reload/misfire/...), ammo, mag_size, ammo_type, spare_ammo, fire_mode (-1 = auto), zoomed, jammed}, torch,
night_vision, detector {working}}; target (crosshair) {distance, id, section, object, name, alive, relation};
world {level, game_time, game_time_factor, time_factor, weather, surge, psi_storm, in_cover}.
sections (optional, cheap but bigger): "inventory" (slots, belt, backpack grouped by section; capped at 100),
"tasks" (active tasks: id, title, type, stage, active; details: game_tasks), "stats" (frame, fps, frame_ms, times); ["all"] = all of them.
Fields that do not apply are absent. While a level loads the answer is {mode: loading, source: supervisor}.

## game_objects

`game_objects(instance: str | None=None, radius: float=100, kinds: list[str] | None=None, pattern: str | None=None, alive: bool | None=None, hostile_only: bool=False, visible_only: bool=False, los: bool=False, limit: int=30, summary: bool=False, offline: bool=False, position: list[float] | None=None)`

Find things around the actor (or `position` [x,y,z]) without screenshots: nearest first, compact, read-only.

radius: metres (3D distance from the origin). kinds: stalker (living NPC, incl.
traders), monster, corpse (dead stalker/monster, `of`), item, weapon, ammo, artefact, anomaly, campfire,
container, door, physic, vehicle, ladder, level_changer, smart_terrain, restrictor, cover (smart cover), squad
(offline only), other (default: all but restrictor and cover); aliases
npc=stalker, box=container, zone=anomaly, loot=item+weapon+ammo+artefact, creature=stalker+monster+corpse, all.
pattern: case-insensitive glob on section / object name / NPC name. alive: true = living creatures only, false =
corpses only. hostile_only: living creatures whose relation to the actor is enemy. visible_only: inside the
current camera view (frustum, not occlusion). los: per object an unobstructed ray from the camera to its centre
(level geometry and other objects block; at most 64 rays per call, later objects get no los field).
limit: max objects (default 30, cap 100); summary=true returns only counts per kind (cheap first look).
offline: also alife server entities on this level that have no client object (beyond the online radius, squads):
listed separately in `offline` (id, kind, section, name, position, distance, bearing, alive, squad, smart;
offline: true); hostile_only / visible_only disable it.
Per object: id, kind, section, name (unique object name), display (NPC character name / item name), position,
distance, bearing (deg to turn right to face its centre: game_input {"look": {"yaw": bearing, "pitch":
elevation}}, or {"look_at": position}), elevation (deg up). Creatures: alive, health, community, rank, relation
(friend/neutral/enemy/worst_enemy), wounded, in_combat, enemy_id, sees_actor, squad_id/squad/smart; any object:
story_id / squad_story_id (story_objects), active_section (xr_logic, db.storage); corpses:
items, has_loot. Items: condition, cost, ammo/mag_size/ammo_type (weapons), rounds (ammo). Anomalies: type (engine
class), radius, enabled (campfires: lit). Containers: empty, items, closed. Level changers: destination. Doors:
door {state open/opening/closed/closing/locked (ph_door logic; scripted = other scheme, no_logic = free physics
door), logic (active section), locked, not_for_npc (cabinet/safe doors), on_use/on_info/... (the logic's condlists:
which info portion or condition opens/unlocks it), tip (the use text a player sees)}. Also counts (per kind,
all matches), total, omitted, target (crosshair: distance, point, id/kind/section or material for level geometry),
cost_ms (engine time). Outside a level: {available: false, reason: no_level/loading/no_actor}.
Detail for one object (inventory, money, active item, zone power): game_object(id).

## game_object

`game_object(id: int, instance: str | None=None)`

Full detail for one object by id (from game_objects / game_state.target): the game_objects fields plus los
(always), inventory (grouped by section: count, rounds; capped at 50) / items / money / active_item for NPCs and
corpses, inventory of containers, effective_radius / max_power for anomalies, parent_id when the object is inside
an inventory. Creatures add enemy {id, name, section, display} (engine best enemy), mental_state (stalkers:
free/danger/panic), danger (selected danger: attacked/enemy_sound/grenade/corpse/...); objects with xr_logic add
logic {scheme, logic (logic section), file (ini or <customdata>), settings (first 12 keys of the active section,
values cut at 120 chars)}. Script facts absent when unknown (offline objects get story_id only).
Not online -> the alife server entity (offline: true). Unknown id -> object null.

## game_tasks

`game_tasks(instance: str | None=None, id: str | list[str] | None=None, active_only: bool=False, history: bool=True, full: bool | None=None)`

Quest state as the PDA knows it (read-only). tasks: every task in progress: id, title, type
(storyline/additional), active (the PDA's active task), state, received (game time), stage (script stage; changes
fire `task_stage` events), status (task_manager status), pending (complete/fail decided, applied on the next task
update or at hand-in), forced_status, target (PDA marker object: id, name, character, section, kind, level, alive,
online, position; distance (m from the actor) and bearing (deg to turn right) only when it is on the actor's
level; null = no marker), giver (id, name, character, level, alive); with full: description (translated, as shown
in the PDA) and def (task ltx strings: stage_complete, target_functor, status_functor, condlist_N, on_complete,
on_fail).
id: one task id, or a list (or "a,b"); fnmatch patterns allowed (e.g. "esc_*"); filters tasks and history.
full: include description/def (default: only when id is given; compact otherwise). active_only: in-progress tasks
only (no history). history=true adds completed/failed tasks, newest first (max 50): id, title, state, finished (game time),
finished_game_ms, last_stage/giver when seen this session (+ description with full). History is the engine task
registry, saved with the game: it follows the loaded save and survives reconnects and level changes.
Wait for a stage: game_wait {"event": "task_stage", "where": {"id": ..., "stage": 1}} or {"state":
"tasks.<id>.stage", "eq": 1}; completion: {"event": "task_completed", "where": {"id": ...}}.
Outside a level: {available: false, reason: no_level/loading/no_actor}.

## game_infos

`game_infos(instance: str | None=None, match: str | None=None, has: list[str] | None=None, give: list[str] | None=None, remove: list[str] | None=None)`

The actor's info portions (story flags such as esc_2_12_stalker_fanat_task_2_done). give / remove: names to
add / drop first (same as db.actor:give_info_portion / disable_info_portion; result given / removed lists only the
ones that changed). has: names -> {name: bool}. match: case-insensitive glob over the known names (sorted, max 500);
without has the full list is returned (filtered by match). count = all known. Each change fires an
info_given / info_removed event. Outside a level: {available: false, reason}.

## game_nav

`game_nav(query: str='at', instance: str | None=None, position: list[float] | None=None, to: list[float] | None=None, to_level: str | None=None, radius: float | None=None, rays: int=16, grid: bool=True, grid_cell: float=2, grid_radius: float | None=None, orient: str='view', max_nodes: int | None=None, max_waypoints: int=64, show: bool=False, random_radius: float | None=None, count: int=1, on: bool | None=None, path: list[list[float]] | None=None, markers: list[list[float]] | None=None, clear: bool=False)`

Where can the actor walk? Read-only queries on the level AI map (the navmesh NPCs path on: a grid of ~0.7 m
walkable cells linked to their neighbours) and the cross-level game graph. Origin = `position` [x,y,z] or the actor.

query:
  info     level AI map: nodes, cell_size, box, game_vertices, exits (game-graph edges to other levels: to_level,
           position) and level_changers (world objects with destination).
  at       is the point on the navmesh? on_mesh (a node under x/z within 1.5 m vertically), node {id, position,
           horizontal, vertical, distance} (nearest node when off-mesh), height_above_node, plane_y, links (walkable
           neighbours 0-4), cover {high, low}, game_vertex, actor_node (= Lua db.actor:level_vertex_id()).
  around   walkable space nearby: `rays` (default 16) straight walks along the navmesh at bearings relative to the
           camera (0 = ahead, +90 = right; game_input look convention) up to `radius` (30 m): distance, open
           (reached radius), end point; nodes_in_radius, reachable (linked to the origin node within radius),
           reachable_box; grid = ASCII map (top = camera forward, orient "north" = +z up), grid_cell metres per
           char (2), grid_radius (min(radius, 30)): '@' origin, '.' reachable, 'o' walkable but not reachable within
           radius, '#' no AI node (walls, water, cliffs, off-map).
  path     level route from the origin (or `position`) to `to`: found, length (along corners), straight, nodes,
           visited, waypoints (corner points: position, distance, heading = world yaw, bearing = turn from the
           current camera yaw, turn = change from the previous leg), reason when not found (no_route: unreachable,
           target_isolated: target in a closed area, search_limit: truncated, off_mesh) + closest_reachable;
           to_snapped when `to` is off the mesh. Engine A* (NPC parameters, 65500 nodes) with an mcp A* fallback
           for longer searches (max_nodes, default 400000). show=true draws it on the overlay. With `to_level`
           (another level, `to` optional on it): game-graph route: legs [{level, enter, exit}], level_changer that
           leads there (match destination_vertex = its destination is the next leg, nearest = closest changer to
           that level) and to_exit (level path from the origin to that changer).
  nearest  nearest navmesh point to `position` (spawn/teleport placement); random_radius: `count` random reachable
           points within that radius.
  overlay  3D debug draw in the game view (visible in game_screenshot): on (true/false), radius (20 m, max 60):
           AI nodes around the actor as squares on the ground (green = reachable from the actor's node, orange =
           not), path [[x,y,z],...] as a yellow line with cyan posts, markers as magenta posts; clear drops
           path/markers. Off by default, reset when the supervisor disconnects.
Outside a level: {available: false, reason: no_level/loading/no_actor/no_ai_map}. cost_ms = engine time.

## game_nav_map

`game_nav_map(instance: str | None=None, position: list[float] | None=None, radius: float=40, cell: float | None=None, orient: str='view', path_to: list[float] | None=None, objects: bool=True, max_px: int=400)`

Top-down PNG of the AI map around the actor (or `position`): walkable cells (green = reachable from the
origin, orange = walkable but not linked, dark = no AI node; brighter = higher), the actor as a white arrow
(camera direction), nearby objects from game_objects as coloured dots, and with path_to [x,y,z] the nav path to
it in yellow. radius metres (40, max 1000), cell metres per grid sample (default the AI cell ~0.7, coarsened to fit
max_px samples per side), orient "view" (camera forward up, matches game_input bearings) or "north" (+z up).
Metadata: origin, yaw used, cell, scale, objects_drawn, path {found, length, ...}.

## game_move_to

`game_move_to(instance: str | None=None, position: list[float] | None=None, id: int | None=None, stop_distance: float | None=None, timeout: float=120, sprint: bool=True, engage: bool | str=True, wait: bool=True, wait_timeout: float=300, status: bool=False, stop: bool=False)`

Walk the actor to `position` [x,y,z] or to an online object `id` (followed while it moves) along the AI-map
path, like a player: the engine turns the camera toward the next path corner and holds the forward key each input
frame (same injected input as game_input), so it respects MCP pause / game_step (no progress while paused; stepped
frames advance it). No teleporting. sprint (default on): tap sprint_toggle while running straight; False = run.
stop_distance (m, horizontal): default 1 (1.5 for objects). timeout: seconds of input time (game time while MCP
time control is used). Doors (ph_door logic, cabinet doors ignored): a shut door within 2.4 m ahead on the path is
opened like a player: step to a spot in front of it outside its swing, look at it until the crosshair pick is the
door within use range (2 m), press use once, wait until it is open, walk through the doorway. Open doors are
walked through without use (a press would close them). A door that stays shut (locked without on_use, use had no
effect after 2 presses, pick missed) -> replan around it, else state blocked_door. Stuck (no progress for 1 s
while holding forward): open a shut door within 2 m, else tap use (none next to an open door; facing an NPC it may
open a talk -> aborted dialog), then jump, then replan, then give up; turning in place 3 s without progress ->
replan. Paths keep ~0.3 m off the AI-map edges at corners. Short look-ahead rays see props/vehicles that are not on
the AI map (not creatures or doors) and replan around them. The target snaps to an AI node on its own floor.
engage (default True): every 200 ms while walking (not at doors) look for a visible hostile creature within 50 m
(capped at the weapon's fire distance): monsters hostile to the actor and stalkers whose relation to the actor is
enemy, never neutral/friendly; whoever attacks the actor first, then the nearest. Walking pauses and the weapon in
hands shoots it until dead (game_shoot logic, aims at a visible bone; lost from sight 1.5 s -> next target), then
the path is replanned and the walk goes on (fight time does not count toward timeout). Nothing in hands -> the
best firearm with ammo (slot 2/3, loaded first) is drawn like its slot key (result drew). No weapon / out of ammo /
jam that reload does not clear -> engaging stops for this move (engage.stopped) and it walks on;
engage="required" makes that state engage_failed instead. engage=False: never shoots. Low health is only
reported (health).
Result: state reached / stuck (stuck_at, blocker = what is ahead) / timeout / no_path (reason) / unreachable
(reason target_floor_off_mesh: no AI node on the target's floor, at once; target_off_mesh: walked to the nearest
node `goal` but the target is beyond stop_distance; wrong_floor: under/over the target after a replan) /
blocked_door (reason locked, no_effect,
pick_missed; blocked_door = its id) / aborted (reason: dialog, actor_dead, level_change, keys_released =
game_input_release or supervisor disconnect, target_gone, replaced, stopped), doors [{id, name, section, position,
before, after (states as in game_objects), action (none, use, opened, avoided), presses, reason, pick (what the
crosshair hit instead), door (the game_objects door facts)}], blocked_door,
distance_left, position, path_length, travelled, elapsed_ms, frames, waypoint/waypoints, goal / goal_on_mesh,
blocker {what object/creature/level_geometry, id, name, section, material, distance}, recovery (steps tried: use,
jump, replan, replan_turning, skip_corner, door, avoid:<id>, ...), tick_us {avg, max}, engage {mode, stopped},
fights [{id, name, section, shots, hits, killed, ms, end (shoot state, e.g. target_dead, not_visible,
los_blocked, timeout, aborted:<reason>)}], fight_ms, health (actor). Keys are released at the
end. wait=False returns at once ({id, state: running}); status=True polls the current/last move, stop=True aborts
it. With game_nav overlay on, the path is drawn.

## game_hunt

`game_hunt(instance: str | None=None, id: int | None=None, task: str | None=None, story_id: str | None=None, timeout: float=600, sprint: bool=True, engage_range: float=100, area_radius: float=10, clear: bool | None=None, clear_radius: float=50)`

Hunt one creature `id`, or the target of quest `task` (task id from game_tasks; or a `story_id`), like a player
on foot. A creature online: game_move_to toward it (replanned as it moves), every 200 ms shooting any visible
hostile within engage_range (100 m, capped by the weapon's fire distance) and switching to the hunted creature
whenever it is visible in range (also a neutral one: hunting means killing it); ends when it dies. Offline (beyond
the online radius): walk to its server position until it comes online. On another level: the game-graph route's
level changer on this level is entered with game_changelevel (no walking to the changer), then it goes on there,
hop by hop. An area target (squad, smart terrain) is walked to (within area_radius m of its centre / squad
commander) and ends arrived_area with hostiles around it (los = visible from the actor now), or with clear (default
True for task, else False) goes on as game_clear_area there (squad: its members; else hostiles within clear_radius
of the centre) and ends like it (cleared / unreachable / timeout ..., kills, remaining, hunts); any other object
(item, stash) ends arrived. Nothing in hands -> a firearm is drawn (steps[].drew). Ladders are not AI-map links: a target reachable only by a ladder ends unreachable
(ladder {id, name, lower, upper, note} when one is near).
Result: state target_dead / arrived_area / arrived / unreachable (reason: no_route, off_mesh,
target_floor_off_mesh, ...) / target_offline / target_gone / timeout / engage_failed (no weapon / ammo / jam) /
aborted (actor_dead, dialog, keys_released, ...) / stuck / no_target; target {id, kind, level, position, alive,
online, name}, task {id, title, stage}, fights [{id, name, section, shots, hits, killed, ms, end}] (end
aborted:hunt_target = left for the hunted one), levels [{from, to, changer, load_s}], steps (each walk: state,
reason, travelled, hunt {id, name, seen, alive, position}), hostiles / hostiles_visible (areas), elapsed_s.
timeout: wall seconds for the whole hunt.

## game_clear_area

`game_clear_area(instance: str | None=None, position: list[float] | None=None, radius: float=50, id: int | None=None, story_id: str | None=None, ids: list[int] | None=None, engage_range: float=100, timeout: float=600, sprint: bool=True)`

Kill every hostile in an area, or every creature in `ids`, one at a time like a player: the next target is a
visible one first (los from the camera), then the nearest; it is hunted as in game_hunt (walk toward it, shoot it
whenever visible within engage_range; any other visible hostile is shot meanwhile, the current target preferred).
Area = `position` [x,y,z] + radius (hostile = living stalker/monster whose relation to the actor is enemy; a far
area is walked to first), or `id` / `story_id` of a smart terrain / restrictor (hostiles within radius of it) or
squad (its members, hostile or not), reached like game_hunt (also on another level); a creature id = ids [id].
ids: those creatures (neutral too), on any level. Nothing in hands -> the best firearm with ammo (slot 2/3, loaded
first) is drawn like its slot key (hunts[].drew). Result: state cleared (none alive left) / unreachable (every
remaining target failed: unreachable, stuck, blocked_door, target_offline) / timeout / engage_failed (no weapon /
ammo) / aborted / no_target; kills (targets dead in our fights or hunts), remaining [{id, name, position, los,
reason}], hunts [{id, name, state, reason, drew, elapsed_s}], fights (game_hunt format), levels, elapsed_s.
timeout: wall seconds for all of it.

## game_use

`game_use(instance: str | None=None, id: int | None=None, approach: bool=True, stand: float=1.1, wait_timeout: float=15)`

Use an object like a player (talk to an NPC, take an item, open/close a door, open a box): turn the camera
onto it (living NPC: chest; else the centre, then points above/below) until the crosshair pick is that object
within use range (2 m), press use once and report. approach (default True): when the first try fails
out_of_range / pick_missed, or a non-door use had no_effect (e.g. a script that acts only within 1.5 m), walk
(game_move_to, no sprint/engage) to a spot `stand` m (1.1; doors 1.5) from the object's centre on the actor's side
and try once more. Far objects work too, but a long walk is better done with game_move_to first.
Result: state used (reason dialog_opened = talk/loot/trade window, taken, door_toggled, no_effect, gone) / failed
(reason out_of_range, pick_missed: pick = what the crosshair hit instead, e.g. "blocked by level geometry") /
timeout; use {id, name, section, position, before/after (doors), presses, pick, door}; actor (position),
distance (eye to the aim point, m), distance_xz (feet to the object's centre), center, pick_range (crosshair pick
distance at the press or when out of range); approached (bool), approach {state, reason, travelled, first
{state, reason, distance, distance_xz, pick_range of the first try}}.

## game_shoot

`game_shoot(instance: str | None=None, id: int | None=None, part: str='chest', shots: int=1, until_dead: bool=False, burst: int=3, max_seconds: float=30, fire: bool=True, ads: bool=False, wait: bool=True, wait_timeout: float | None=None, status: bool=False, stop: bool=False)`

Shoot creature `id` with the weapon in hands like a player: every input frame the engine turns the camera so
the weapon's fire ray points at `part` ("chest", "head" or a bone name; the bone's collision shape centre, else
the bbox centre) and re-aims after each recoil kick. Every 100 ms the eye's line of sight to candidate points is
checked (the requested part, head, chest, pelvis, limbs, bbox centre): it aims at the requested one while visible,
else at a visible one (target.bone, bone_switches); none visible for 300 ms -> not_visible (blocker = what hides
the requested part; a target never seen is not turned toward). The fire action is pressed only while that fire ray (the
ray bullets leave along before spread) hits the target first. Real ballistics: spread, recoil, ammo, jams and fire
modes stay the game's, so misses at range are normal. Automatic fire: bursts of `burst` rounds, released when the
aim drifts off; single/burst modes: one press per shot. Empty magazine -> normal reload if spare ammo; jam ->
reload (unjams in the engine). ads: aim down sights while shooting. fire=False: track only (keep aiming until
max_seconds / stop; use game_input meanwhile). Movement stays yours (game_move_to). Honours MCP pause/game_step.
Shot count: `shots` rounds fired (counted in the engine's fire path, so g_infinite_mag counts too), or until_dead. For the outcome only, game_kill(by_actor=True) is quicker.
Result: state done / target_dead / target_gone / not_visible (blocker) / los_blocked (the barrel's line of fire
blocked 1.5 s; blocker: what, id, name, material, distance) / out_of_range (weapon fire distance) / out_of_ammo / jammed / no_weapon (reason: nothing in hands,
knife, binoculars, not a firearm, grenade launcher mode) / timeout / aborted (dialog, actor_dead, level_change,
keys_released = game_input_release or disconnect, replaced by game_move_to/game_use, stopped); target {id, name,
section, part, bone, visible, bone_switches, position, distance, health_before, health, killer_id, killed_by_actor}, shots, hits and
hit_power and hit_bones (the actor's hits on it as the engine registered them), other_hits/other_ids (anything
else the actor hit), weapon, ammo {mag_before, total_before, mag, total}, reloads, jams, aim_error_deg,
elapsed_ms, frames. wait=False returns at once; status=True polls, stop=True aborts.

## game_guard

`game_guard(instance: str | None=None, on: bool | None=None)`

Idle guard (on by default, again for every new supervisor connection): while no game_move_to / game_use /
game_shoot runs and the agent sends no input, the actor shoots visible hostile creatures within 50 m with the weapon
in hands (same target rules and shooting as game_move_to engage). Paused while any UI is open (inventory, PDA,
talk, menus, console), while MCP-paused, loading, dead, in a vehicle, while game_input steps run or MCP-held keys
are down, and for 1 s after any of these; a running guard fight is aborted by any of them (so game_talk,
game_ui_click, game_use never fight the guard for the camera). Fights are `guard_fight` game events (phase start /
end, see game_events). on=True/False switches it; no argument = status. Result: on, state (off, watching,
fighting, resuming, no_weapon (reason), or the pause reason: ui, driver, input, mcp_paused, loading, actor_dead,
vehicle, no_actor), fighting {id, name, shots, ms}, fights (count), last_fight, tick_us {avg, max}.

## game_input

`game_input(steps: list, instance: str | None=None, wait: bool=True, timeout: float=120, image_size: list[int] | None=None)`

Play keyboard/mouse input like a human, timed per frame by the engine (works silent/minimized, no focus).

Input goes where real input goes (level, main menu, dialogs, inventory, PDA, console). steps is a timed script:
a list runs in SEQUENCE; a list nested in it runs in PARALLEL (a list inside that is a sequence again, ...).
Steps (ms = duration; one verb per step):
  {"action": "forward", "ms": 2000}  hold a game action (bind name) for 2 s; without ms = tap (1 frame);
                                    "state": "down" / "up" = press / release only (stays held until released).
  {"key": "ESCAPE"}                 same for a raw key (W, ESCAPE/ESC, RETURN/ENTER, UP, F1, SPACE, LSHIFT,
                                    MOUSE1..8 / LMB / RMB ...); with ms / state like actions.
  {"text": "hello"}                 type text (key taps with shift, current keyboard layout) into an edit box.
  {"look": {"yaw": 90, "pitch": -10}, "ms": 300}  turn the actor camera by degrees (+ = right / up), smoothed.
  {"look_at": [x, y, z], "ms": 300} / {"look_dir": [dx, dy, dz]}  aim the camera at a world point / direction.
  {"mouse": {"dx": 40, "dy": 0}, "ms": 200}  raw mouse move in pixels (UI cursor; in game scaled by sensitivity).
  {"wheel": 1}                      mouse wheel notches (+ = up).   {"wait": 500}  pause 500 ms.
UI cursor steps (menus, inventory, dialogs; never moves the real Windows cursor). Points [x, y] are pixels of the
last game_screenshot image (or of image_size [w, h] / a step's "image": [w, h]); "space": "ui" = UI units
(1024x768), "norm" = 0..1. Before any screenshot the default image is 1024 px wide (game_screenshot's default).
  {"cursor": [x, y], "ms": 200}     move the cursor there (glide over ms; hover highlights follow).
  {"click": [x, y], "button": "left"|"right"|"middle", "double": true, "hover": 400}  move + click
                                    (double-click; hover = ms to rest before clicking, main menu needs ~400).
  {"drag": {"from": [x, y], "to": [x, y]}, "ms": 300}  press at from, glide to to, release (drag and drop).
  {"wheel": -3, "at": [x, y]}       scroll at a point.   {"cursor": "release"}  hand the cursor back (-showwindow).
Example "walk 2 s while turning 90 deg, then use":
  [[{"action": "forward", "ms": 2000}, {"look": {"yaw": 90}, "ms": 2000}], {"action": "use"}]
Actions: forward back lstrafe rstrafe jump crouch accel sprint_toggle llookout rlookout wpn_fire wpn_zoom
wpn_reload use inventory active_jobs (PDA) wpn_1..6 quick_use_1..4 torch night_vision show_detector drop quit
(ESC/menu) ... aliases: fire aim zoom reload pda sprint walk menu strafe_left/right lean_left/right. Unknown
names fail with the full list; actions resolve to their bound key.
wait=True returns when done: {id, done, elapsed_ms, frames, held (keys still down), warning?}; wait=False returns
{id} at once. steps=[] returns the status: running sequences and held keys. Keys are released on
game_input_release, when the supervisor disconnects, and on timeout (seconds).

## game_input_release

`game_input_release(instance: str | None=None)`

Release every key/button held by game_input and cancel running input sequences: {released, cancelled}.

## game_screenshot

`game_screenshot(instance: str | None=None, max_width: int=1024, format: str='jpeg', quality: int=70, save_path: str | None=None, timeout: float=10)`

Capture what a player would see (3D view + HUD/UI/menus) and return it as an image plus JSON metadata.

The frame is the presented back buffer of the next rendered frame (works minimized/silent and with -showwindow).
max_width: downscale the returned image to this width (aspect kept; 0 = full size). format: "jpeg" (default,
quality 1..100) or "png". Default (1024 px JPEG q70) is ~50-200 KB. save_path: also write the FULL-resolution
frame to this file (.jpg/.jpeg = JPEG q95, anything else PNG); relative paths go to .mcp-run/<id>/screenshots/.
Metadata: width/height (returned image), orig_width/orig_height, frame and level when captured (level null in the
main menu), readback_frame, bytes (returned image), saved_path/saved_bytes, capture/encode/save/total ms, coords
(pixel x,y of this image are the default coordinates of game_input cursor/click/drag steps and of game_ui px rects).
Fails cleanly while a level is loading (the loading screen is not capturable) and with HDR10 output on.

## game_screenshot_burst

`game_screenshot_burst(instance: str | None=None, every: float=0.5, duration: float=10, max_width: int=1024, format: str='jpeg', quality: int=70, folder: str | None=None, start_on_log: str | None=None, log_since: int | None=None, ignore_case: bool=False, start_timeout: float=60, inline_every: int=0)`

Capture a series of frames (cutscenes, animations, sliding) to files; returns paths + timestamps, not images.

Captures at t = 0, every, 2*every, ... up to duration s (inclusive) into folder (relative -> .mcp-run/<id>/
screenshots/<folder>; default burst_<date>_<time>), files `<i>_<t>s.jpg|png`, at max_width/format/quality like
game_screenshot. One capture takes ~3 rendered frames (~0.1 s at 30 fps), so every below that is not reachable:
a capture that overruns its slot starts the next one at once (no catch-up), `skipped_slots` counts the misses.
start_on_log: regex (re.search; ignore_case) on log lines with seq > log_since (default: lines arriving after
this call; pass a log_tail last_seq taken before the action to also catch a line printed meanwhile); waits up to
start_timeout s, then t = 0 at the match (`trigger` {seq, line, latency_ms from the line's arrival}).
Result JSON: folder, frames [{i, t (s since start), frame, path, ms} or error], count, achieved_every (mean gap),
max_gap, capture_ms_avg. inline_every=k also returns every k-th frame as an inline image (0 = none; view the
files with an image reader instead).

## game_time

`game_time(instance: str | None=None, pause: bool | None=None, sim_speed: float | None=None, clock_rate: float | None=None, advance: str | float | None=None)`

Freeze / resume the game and set its speed. No arguments = report only.

pause=True: MCP pause - world, AI, physics, scripts' updates and sounds stop; frames keep running, so
game_screenshot / game_state / game_objects / lua_exec / console_exec keep working on the frozen world. game_input
steps do not advance while MCP-paused (they run during game_step frames). Independent of the game's own pauses:
the ESC menu over an MCP pause keeps it after closing; the game resuming on purpose (pause key, console `load`)
ends it. Loads/level changes end it (game_teleport / game_load re-pause afterwards themselves). Resumes by itself
when the supervisor disconnects. sim_speed: simulation speed (engine time_factor: 0.25 = slow motion, 4 = fast;
physics/AI get unreliable far from 1, sound pitch follows; restored on disconnect). clock_rate: game clock rate
= in-game seconds per simulation second (level time factor; Anomaly default 6 from alife.ltx; surges/psi storms
use their own and restore it; back to the default after a level change or load; not restored on disconnect;
clock_rate_before is returned). advance: jump the game clock forward like fast travel / sleep (minutes as a
number, or "2h30m" / "1d" / "45m"; 1 min .. 30 days): alife + sky time, surge/psi-storm managers told
(time_forwarded, so no emission right after the jump; ones that were due show "recently occurred" PDA news),
weather re-rolled; NPCs/tasks just see the later time. Returns advance {minutes, old_game_time, new_game_time
(as in game_tasks received), old/new_game_time_ms}. Result: mcp_paused, applied (in effect), paused (any
pause), main_menu, frame, time_global / time_async (simulation ms), time_factor (= sim_speed), game_time_ms,
clock_rate, step_id / step_done / steps_left / step_dt_ms.

## game_step

`game_step(instance: str | None=None, frames: int | None=None, ms: float | None=None, dt_ms: int=20, input: list | None=None, screenshot: bool=False, state: bool=True, max_width: int=1024, format: str='jpeg', quality: int=70, save_path: str | None=None, timeout: float | None=None)`

Advance a paused game by a fixed amount of simulation time, then stay paused: the agent loop
(look -> think -> act -> step). Pauses first if needed (see game_time).

frames: number of frames, or ms: simulation time (frames = ms / dt_ms, rounded). Every step frame advances the
simulation by exactly dt_ms (default 20 = 50 fps), independent of wall time and sim_speed, so runs are
reproducible. input: game_input steps queued before stepping; their ms are simulation ms here (a tap needs 2 step
frames, a hold of 500 ms needs 500 ms of steps; unfinished input continues on the next step). screenshot=True
adds the frozen frame after the step (max_width/format/quality/save_path as game_screenshot); state=True adds
game_state. Returns JSON: frames, sim_ms, before/after {frame, time_global, game_time_ms}, time_global_delta,
game_time_delta_ms, cancelled (main menu opened / resumed meanwhile), input (status), state, wall_ms.

## game_ui

`game_ui(instance: str | None=None, view: str='interactive', root: str='top', text: str | None=None, name: str | None=None, kind: list[str] | None=None, limit: int=80, image_size: list[int] | None=None, include_clipped: bool=False)`

What is on screen, structured: the visible windows of the top input-owning dialog, to click by meaning.

view: "interactive" (default: buttons, check boxes, tabs, edits, track bars, combos, list items, item cells,
talk answer options, menu_item = main/ESC menu entries), "text" (every visible text: labels, phrases, descriptions), "tree" (all visible windows
with depth d), "all" (flat, everything). root: "top" (main menu dialog when the menu is up, else the game's top
dialog), "menu", "game", "hud" (in-game HUD), "all" (menu + game + hud).
text: filter, case-insensitive exact / glob / substring of the shown text; name: window name (exact or glob);
kind: list of kinds. limit caps the nodes (count = all matches, omitted = the rest).
Per node: path (stable id while the UI is unchanged; use with game_ui_click(path=)), kind, text (as shown,
colour codes stripped), name, rect [l,t,r,b] and center [x,y] in pixels of the last game_screenshot image
(or image_size [w,h]; image_size in the result), state when set: checked, active (tab), pushed, selected, focus
(edit), value (track bar), scroll [pos,min,max], enabled false, hover (cursor over it), clip ("partial";
windows scrolled out of view are skipped: clipped_out counts them), item {section, name, id, count, container}
for inventory/trade cells. class / script_class for plain windows and script windows.
talk (talk window open): phrases (last 6 {speaker, text}) and options [{index (on-screen number), id (phrase id,
dialog id in the topic list), text, path, center, clip "out" when scrolled out}]; the full view is game_talk.
cursor {x, y (UI units), px, visible, mcp_owned}. dialogs: [{root, class, script_class}]. During a load:
loading true and no nodes. text also matches a widget's label (the text next to a check box) and an item
cell's item name / section. 3D PDA (default): its UI is drawn onto the device, so px rects do not match the
screenshot (game_ui_click still hits, it works in UI space) and its widgets stay enabled:false until the
first click on the PDA (a game_input {"key": "LMB"} focuses it).

## game_ui_click

`game_ui_click(instance: str | None=None, text: str | None=None, name: str | None=None, path: str | None=None, option: int | None=None, kind: list[str] | None=None, button: str='left', double: bool=False, root: str='top', nth: int | None=None, settle_ms: int=300, image_size: list[int] | None=None)`

Click a visible, enabled widget by meaning: moves the UI cursor to its centre and clicks through the normal
mouse path (hover, focus, callbacks as for a human). Pick by text (shown text, case-insensitive exact or glob
like "new*"), name (window name), path (from game_ui), or option (talk window answer option, 1-based as listed
in game_ui talk.options; scrolled out -> use game_talk). A text inside a button / list item / option clicks that widget. Ambiguous -> error
listing the candidates (use nth or path); no match -> error with the visible texts.
button: left / right / middle; double: double-click (e.g. use/equip an inventory item).
Returns clicked (node), at_ui / at_px, and after settle_ms the resulting mode and ui dialog (game_state).

## game_talk

`game_talk(instance: str | None=None, id: str | None=None, n: int | None=None, text: str | None=None, line_chars: int=2000, log_last: int | None=None, timeout: float=3)`

The NPC conversation, complete (all options and log lines, also those scrolled out of view), and choosing an
option like a player. Open the talk first: game_use(id) on the NPC.

No choice: {open, mode (talk | trade | upgrade | other window over the talk), partner {id, name, section,
character, profile, community, trade, upgrade}, topic (true = topic list, options are dialogs), dialog (current
dialog id), options [{n (on-screen number), key (number key, options 1..10), id (phrase id; in the topic list
the dialog id), dialog, text (never cut), final (ESC/use picks it), ends (the dialog ends after it or the
partner's reply), actions (script function names run when said), give_info / disable_info, clip "out"/"partial"
(scrolled out of the list)}], log [{i, speaker, text, actor | info (task/money notes)}] (log_last: only the last N),
log_count}. open false = no talk window.
Choose by id (phrase id from options; ids are the game's dialog XML ids, the same in vanilla and mods), n or
text (exact / glob / substring): presses the option's number key, or scrolls the list and clicks it (n > 10), so
preconditions, scripts and sounds run as for a player; waits until the window updated (timeout s). The game
ignores a choice while the option list is being rebuilt, so it first waits for that (pending) and repeats a press
that changed nothing within 1 s once (chose.retried). Returns chose {n, id, dialog, text, via}, new_lines (said since the choice), then the new view (log omitted unless
log_last). The dialog closing or switching to trade -> open false / mode trade (+ game_mode, dialog_window).
line_chars: log lines longer than this are cut (truncated = full length); 0 = no limit. Option text is never cut.

## game_perf

`game_perf(instance: str | None=None, seconds: float=5)`

Measure the game's main thread over a window of `seconds` (0.1..120) and return per-frame averages.

fps, frame_ms avg/p50/p95/p99/max/min, hitches (frames > max(2*p50, 33.3 ms)). Loading frames are excluded.
cap: capped/capped_by (r__framelimit, the menu/pause refresh cap, vsync), limiter_ms = busy-wait in the frame
limiter. main.busy_ms = frame time minus limiter and inactive sleep (the load to compare; busy_fps = 1000/busy).
main.phases (ms, pct of frame, lua_ms inside): before_frame, frame_move (seqFrame: level/actor/UI/scripts,
includes scheduler_rt), pre_render (particles, matrices, task submit), limiter, render (seqRender submit),
present (End/Present: GPU or vsync wait), wait_tasks (waiting for worker tasks: scheduler, GC), inactive_sleep,
other (message pump, rest). main.lua_ms = Lua entered via luabind calls/resumes plus explicit lua_gc steps on the
main thread; tasks.* = same for worker tasks (GameThread: deferred scheduler, seqFrameMT; parallel Lua GC).
Not covered: Lua run by raw lua_pcall (script loading, console run_string without a level, lua_exec itself),
and GC triggered implicitly by allocation (it counts as time of the Lua call that allocated).
memory: working set, private bytes, Lua heap (MB) and its growth per frame (lower bound), engine alloc calls.

## game_lua_profile

`game_lua_profile(instance: str | None=None, seconds: float=5, top: int=30, mode: str='time')`

Profile the game's Lua over `seconds` (0.1..120): WHICH function / script / callback / entry point costs time.

All ms are per frame; pct = % of that thread's Lua time, frame_pct = % of the average frame. profile.threads.main
and .worker (worker = GameThread tasks; only present if any ran) each have:
- entries (EXACT): outermost engine->Lua calls (luabind functor/binder method/script thread resume) by the
  called function: ms, calls_per_frame, max_ms (worst single call). E.g. bind_stalker.script actor update.
- functions (SAMPLED): self_ms (own code + the C/engine functions it calls), total_ms (inclusive), samples,
  gc_pct (share of self samples inside GC = allocation pressure). functions_by_total = call-tree view.
- files: self time per script. callbacks: RegisterScriptCallback dispatch (axr_main make_callback) per callback
  name with its listener functions (inclusive ms), e.g. actor_on_update -> my_mod.script:12 (on_update).
- vmstate_pct of samples (jit/interp/c/gc/compiler); sampler_ms = profiler's own time (excluded from lua_ms).
Sampling: LuaJIT timer ~1-2 ms, only ticks inside a luabind entry count; scaled to the exact Lua time, so short
windows / cheap functions are noisy (check samples). worst_frames: the 5 slowest frames with their top exact
entries and sampled functions (hitch attribution). game_perf_lua is game_perf's Lua time for the same window
(includes the sampler's overhead); gc = explicit GC steps and Lua heap. Fn ids are script:linedefined.
mode: "time" (default, ~1-3 % overhead), "calls" (+ exact calls_per_frame per function via a call hook, JIT
off for the window: slow, times inflated), "alloc" (jit.allocprof allocation KB/frame per function and file,
byte-sampled, JIT off; no time sampling). The JIT is restored after the window. Survives level changes
(lua_state_closes counts re-arms). Not covered: Lua run by raw lua_pcall (script loading, lua_exec).

## game_spawnable

`game_spawnable(instance: str | None=None, kind: str | None=None, pattern: str | None=None, faction: str | None=None, limit: int=200)`

Spawnable sections from the game's data (Anomaly's debug spawner lists, so mods' items are included).

No filter -> categories with counts, kind aliases and squad factions. kind: alias (item, weapon, ammo, outfit,
artefact, monster, stalker, npc, squad, monster_squad, stalker_squad) or a category key / glob (e.g. items_food,
weapons_rifle, squads_stalker). pattern: glob on the section name (no wildcard = substring). faction: squads of
that faction (e.g. bandit, duty, monster_predatory_day). limit: max sections listed (default 200).

## game_spawn

`game_spawn(section: str, count: int=1, position: list[float] | None=None, distance: float | None=None, relation: str | None=None, instance: str | None=None, wait: float=3)`

Spawn count (1..50) objects of a spawnable section (see game_spawnable): items on the ground, monsters,
stalkers, squads (alife squad sections: members are created and the squad joins the simulation like a natural
one), anomalies, vehicles, physics objects. Saves/loads like natural objects.

Placement: position [x, y, z], else distance m ahead of the camera (default item 2, monster/stalker 15,
squad 25, others 4-8), snapped to the nearest AI-map node (within 30 m; never inside geometry); count > 1 spreads
them on a ring. relation (squads/stalkers): enemy | neutral | friend towards the actor (default: faction's).
Returns alife ids at once; waits up to `wait` s for the game objects (online true/false per object; squads far
from the actor stay offline). Every spawn is tracked for game_release(spawned=True). Kill with game_kill.

## game_give

`game_give(section: str, count: int=1, rounds: int | None=None, equip: bool=False, remove: bool=False, instance: str | None=None)`

Give the actor count (1..100) inventory items of section (see game_spawnable kind=item). Ammo: count = full
boxes, or rounds = exact number of rounds (split into full boxes + a remainder). equip: move the (first) item to
its slot (weapons become active; outfits/helmets are worn). remove: take up to count items of section from the
actor instead (count 0 = all). Any item section of system_ini works (quest items too). Returns the item ids once
they are in the inventory (in_inventory, inventory_count; note on a 5 s timeout). Money / health: game_actor.

## game_actor

`game_actor(heal: bool=False, health: float | None=None, stamina: float | None=None, radiation: float | None=None, satiety: float | None=None, psy_health: float | None=None, bleeding: float | None=None, money: int | None=None, instance: str | None=None)`

Set actor stats (0..1): health, stamina, radiation, satiety, psy_health; bleeding 0 stops bleeding; money =
exact amount (RU). heal = health/stamina/psy 1, radiation 0, bleeding stopped (explicit values win). Returns before
and after. Other toggles are console cvars (console_exec): "g_god on" (god mode), "g_unlimitedammo on", "g_infinite_mag on", "g_norecoil on";
faction: game_teleport(faction=...).

## game_kill

`game_kill(ids: list[int], by_actor: bool=False, instance: str | None=None)`

Kill creatures by id through the engine's kill path (death callbacks, corpses, squad bookkeeping run as usual).
A squad id kills its online members. by_actor: the actor is the killer (counts as the actor's kill: goodwill,
stats); default the victim itself. Offline (alife-only) creatures cannot be killed; release them instead.

## game_release

`game_release(ids: list[int] | None=None, spawned: bool=False, instance: str | None=None)`

Remove objects from the game (alife_release: squads with their members, NPCs out of their squads, items also
from inventories). ids: any object ids; spawned: everything game_spawn created in this game process that still
exists (ids are checked against the spawned section, so reused ids are skipped).

## game_weather

`game_weather(weather: str | None=None, time: str | None=None, advance_hours: float | None=None, instance: str | None=None)`

Weather and time of day. No arguments -> current state plus the valid weathers: cycles {cycle: [presets]}
(e.g. clear, cloudy, rainy, stormy, foggy) and other weather files. weather: a cycle (random preset of it) or a
preset, applied at once through level_weathers (holds for a full weather period); another weather file is set
directly (level_weathers replaces it at the next game hour). time: 'HH:MM' or hour, reached by advancing the game
clock (never backwards); advance_hours: advance further. Returns before/after (weather, cycle, preset, time, day).
Surges/psi-storms play weather effects that override the weather while they run. Time factor: not here.

## game_emission

`game_emission(kind: str='surge', action: str='status', instance: str | None=None)`

Emission (kind=surge) or psi-storm (kind=psi_storm) through Anomaly's surge_manager / psi_storm_manager (the
debug tools' calls, so the scripted effects, sounds, hits and their Lua load are the real thing). action: start,
stop (ends it at once), status. Start may be skipped by the manager (underground level, disabled in options,
level exceptions); notes say why. Returns before/after: started, elapsed_s, duration_s (surge), weather_fx,
time_factor. game_state.world.surge / psi_storm report the same started flag.

## game_wait

`game_wait(until: str | dict | list, instance: str | None=None, timeout_s: float=30, fail_on: list | None=None, since: int | None=None, log_since: int | None=None, poll_ms: int=50)`

Block until a condition holds (or at once if it already does), instead of sleeping and looking.

until: a condition. Strings: "in_control" (level loaded, actor alive, first update done, no load - what
game_teleport waits for), "loaded" (no load in progress), "main_menu", "hung", "crash", or an event type / glob
(next such event, e.g. "actor_died", "task_*"). Objects (exactly one main key each):
{"in_control": "l01_escape"} (on that level; a new game is in control on fake_start first) | {"mode": "dialog"} or a list of modes (game_state mode: in_game,
dialog, dead, paused, main_menu, in_game_menu, console, tutorial, loading) | {"dialog": "talk"} (a dialog whose
kind/class/script_class/script_mode/menu_mode matches the glob is open; "open": false = none is) |
{"event": "task_given", "where": {"id": "my_task"}} (data fields; strings are globs) |
{"log": "regex", "ignore_case": true, "since": seq} (a log line with seq > since; default log_since) |
{"sim_ms": 5000} (simulation time; follows game_time sim_speed and game_step) | {"frames": 100} |
{"game_clock": "21:00"} (game clock reaches HH:MM, next day if already past) |
{"state": "actor.health", "lt": 0.5} (any game_state path, sections inventory./tasks./stats. fetched as needed;
list elements by index or id: "tasks.<task id>.stage") |
{"has_item": "wpn_ak74*"} (inventory count; ops apply, default >= 1) |
{"object": 1234, "is": "dead"} (exists, gone, alive, dead, online, offline; default exists) |
{"object": 1234, "within": 5, "of": [x,y,z] | "actor" | id} | {"object": 1234, "field": "health", "lt": 0.3} |
{"count": {"kinds": ["monster"], "radius": 50, "alive": true, "hostile_only": true}, "eq": 0} (game_objects
filter: kinds, pattern, radius, alive, hostile_only, visible_only, position, offline) |
{"lua": "db.actor:money() > 5000"} (expression or chunk with return; truthy = met; value = tostring; a Lua
error aborts the wait with failed_on lua_error) |
{"all": [...]} / {"any": [...]} / {"not": {...}}; a plain list = all.
Ops for state/has_item/object/count: eq, ne, lt, le, gt, ge, in (list), match (glob), exists (bool),
within (metres, with of); no op = truthy.
fail_on: conditions that abort with met false and failed_on, e.g. ["script_error", "actor_died", "hung"]. A crash
or game exit always ends the wait at once (failed_on crash/exit with the crash record).
since: event seq; event conditions only match events with seq > since (default: the newest when the call starts).
Chain waits with the returned event_seq (the matched event's seq) so nothing is missed. log_since / log_seq: the
same for log lines. Race: by default only lines written after the call starts count, so a line printed by an
action issued just before the wait is missed. Take the marker first: m = log_tail(n=1).last_seq (or a previous
log_seq), act, then game_wait({"log": ...}, log_since=m) (or the condition's own since) - lines already buffered
after m match at once. timeout_s (30, max 3600): a timeout is a normal result: met false, timed_out true,
conditions with the last observed values. poll_ms (50): state/object/Lua conditions are polled (one batched
engine round trip, one frame); events and log lines are pushed and wake the wait at once. During a level load
polled conditions read "loading" and the wait goes on. sim_ms / game_clock while MCP-paused (no game_step
running) is refused: time would never advance.
Result: met, failed_on, timed_out, elapsed_s, frames, sim_ms, game_s, polls, mode, conditions [{cond, met, value,
distance?}] (the evidence), event_seq, log_seq.
