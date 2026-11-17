# Anomaly MCP: tester guide

You drive a real Stalker Anomaly / GAMMA game through the `anomaly` MCP tools and test it like a player would. The game runs hidden and muted; you see it only through the tools. Each tool's own description has its arguments and result fields; `mcp/README.md` next to this file is the full reference.

## Session flow
1. **Start:** `game_launch`. Then `game_teleport(level=..., faction=...)` for a new game on a map, or `game_load(name)` for a save (`game_saves(import_from=...)` copies one in first). Both return when you are in control.
2. **Look:** `game_state` first (cheap: mode, actor, hands, crosshair target, world). `game_screenshot` when you need to see. `game_objects` for what is around you, `game_ui` for what is on screen.
3. **Act** like a player (tables below).
4. **Verify** with state, not by assuming: `game_wait` for the expected event or state, then read `game_state` / `game_tasks` / `game_infos`. Check `game_events(types=["script_error"])` after anything that runs mod code.
5. **Finish:** `game_stop`. Always stop the games you launched.

## Which game `game_launch` starts
| Call | Game |
|---|---|
| `game_launch()` | Anomaly with the tester's own mods (Mod Organizer in `C:\AnomalyMO2`, its active profile). The default. |
| `game_launch(game_root="C:\\Anomaly")` | Plain vanilla Anomaly, no mods. |
| `game_launch(game_root="C:\\GAMMA")` | GAMMA. First load takes 30-45 s. |

Cheats are **on by default**: god mode, infinite magazine, no recoil (and unlimited reserve ammo). Testing damage, death, ammo use or recoil? Launch with `god=False`, `infinite_mag=False`, `norecoil=False` or `cheats=False` (all three off).

## Which tool for what
### Look
| I want to | Use |
|---|---|
| Know where I am and what the game is doing | `game_state` (add `sections=["inventory"]`, `"tasks"`, `"stats"` when needed). |
| See the screen | `game_screenshot`. |
| Record a cutscene, animation or anything that moves | `game_screenshot_burst` (frames to files every N s; `start_on_log` starts it on a log line). |
| Find NPCs, monsters, items, doors, anomalies nearby | `game_objects` (filter by `kinds`, `pattern`, `hostile_only`; `summary=true` for counts). One object in full: `game_object(id)`. |
| Know what is on screen in a menu, inventory, trade | `game_ui`. |
| Know where I can walk, or how to get somewhere | `game_nav` (`at`, `around`, `path`, `info` for level exits), `game_nav_map` (top-down picture). |

### Move
| I want to | Use |
|---|---|
| Walk somewhere | `game_move_to` (position or object id). It opens doors itself and reports them; `blocked_door` = locked, read the reason. It also shoots hostiles it sees on the way (`engage=False` to forbid). |
| Go to the next level | `game_changelevel("<level name>")`. No need to walk to the changer. A locked changer returns `LevelLocked`. |
| Jump anywhere, or start a new game | `game_teleport` (`level`, `position`, `faction`). Not how a player travels: use it for setup only. |

### Interact
| I want to | Use |
|---|---|
| Talk to, pick up, open | `game_use(id)`. It walks close, aims and presses use; a raw `use` key often misses. |
| Hold a conversation | `game_talk` to read everything, `game_talk(id=...)` to choose. Prefer phrase `id` over text: ids are the same on vanilla and GAMMA, texts are not. |
| Inventory, trade, menus | `game_ui` to find widgets and item cells, `game_ui_click` to click by text (`double=true` to equip or use an item, `button="right"` for its menu), `game_input` for drag (`drag` step between cell `center`s). |
| Keys, looking, anything else | `game_input` with action names (`fire`, `reload`, `inventory`, `forward` ...) or raw keys. Keys are the defaults. `game_input_release` lets go of everything. |

### Fight
| I want to | Use |
|---|---|
| Shoot one target I can see | `game_shoot(id, part="chest"\|"head", shots=N or until_dead=true)`. Real spread, so misses at range are normal; it does not walk. Read `state`, `hits`, `target.health`; `los_blocked` / `not_visible` name the blocker. |
| Go and kill a creature or a quest's target | `game_hunt(id=...)` or `game_hunt(task="<task id>")`. Walks there (also across levels), fights on the way, ends `target_dead` or `cleared`. |
| Kill every hostile in a place | `game_clear_area(position, radius)`, or `id` of a squad / smart terrain, or a list of `ids`. |
| Not be killed while I read or think | Nothing: `game_guard` is on by default and shoots visible hostiles within 50 m while you are idle. |
| Stop the actor from shooting on its own | `game_guard(on=False)`, and `engage=False` on `game_move_to`. Do this when the test needs enemies alive or neutral NPCs untouched after a relation change. |
| Just make something dead | `game_kill(ids, by_actor=true)` (credits the player, so kill quests advance). |

### Quests and events
| I want to | Use |
|---|---|
| Quest state | `game_tasks` (stage, target, giver, history; `id="esc_*"` to filter). |
| Story flags | `game_infos` (`has`, `match`; `give` / `remove` to change them). |
| What happened | `game_events`: `task_*`, `info_*`, `death`, `hit`, `combat`, `relation`, `guard_fight`, `level_loaded`, `game_saved`, `actor_died`, `script_error`. |
| Wait for something | `game_wait` (event, state, object, log line, Lua predicate) with `fail_on=["script_error", "actor_died"]`. Do not poll in a loop or sleep. |

### Set up a scenario
| I want to | Use |
|---|---|
| Spawn items, monsters, NPCs, squads | `game_spawnable` to find the section, then `game_spawn` (`relation="enemy"` for hostile squads). Clean up with `game_release(spawned=true)`. |
| Items, money, health | `game_give` (`equip=true` puts it in its slot), `game_actor` (`heal`, `money`, `radiation` ...). |
| Weather, time of day, emission | `game_weather`, `game_emission`; `game_time(advance="2h")` jumps the clock like sleeping. |
| Freeze, slow down or step the game | `game_time(pause=true)` / `sim_speed`; `game_step` advances a paused game by exact frames (reproducible timing). |
| Keep or restore a situation | `game_save`, `game_load`, `game_saves`. |

### When something goes wrong
| I want to | Use |
|---|---|
| Read the engine log | `log_tail` (`pattern` = regex). To see only what an action printed: take `last_seq` before it, pass it as `since` after. |
| Script errors | `game_events(types=["script_error"])`. |
| Crash | `game_last_crash` (reason, stack, dump paths), then `game_restart`. |
| Hang (`game_status` says `hung`) | `game_dump`, then `game_stop`. |
| Performance | `game_perf` (fps, frame phases), `game_lua_profile` (which script or callback costs time). Uncap first: `console_exec("r__framelimit 0")`, the test config caps at 30 fps. |
| Anything missing | `lua_exec` / `console_exec`. Say in your report when you needed them: that is a tool gap. |

## Rules that save you time
- **One game at a time** unless told otherwise. GAMMA nearly fills the 8 GB GPU; never run another game next to it.
- **Mod Organizer launches** (the default and GAMMA) are refused while the user's own MO2 is open: report that, do not close it. Never change the mod list.
- **Clean faction start:** only from the main menu. In a running game `faction` just relabels the actor; use `game_restart` first, then `game_teleport(faction=...)`.
- **Screenshot coordinates:** `game_input` click/drag and `game_ui` positions are pixels of the last `game_screenshot`. Take a screenshot before using coordinates, and again after the UI changes.
- **Inventory closes itself** for 2-3 s after equipping an outfit or helmet or eating (animation). Reopen it and re-read `game_ui` before the next click.
- **Drag item onto item** only works if the game allows that pair (a scope must be in the weapon's `scopes` list); nothing happens otherwise, so check the result.
- **Artefacts** on the ground are invisible and cannot be picked up without an active detector (detector in its slot, `show_detector` action), then `game_use`.
- **Ladders** are not on the walk map: a target reachable only by ladder ends `unreachable`. Climb with `game_input` or use `game_teleport(position=...)`.
- **`game_kill`** needs online targets (near you). Offline targets can only be removed with `game_release` (no death, no credit).
- **`task_stage` events** arrive 1-3 s after the change; wait for them instead of reading immediately.
- **Lua errors are dangerous:** an error inside a game callback crashes the game by engine design. Test risky `lua_exec` code on a saved game.
- **Do not touch** files in `C:\Anomaly`, `C:\AnomalyMO2` or `C:\GAMMA` unless the task says so. The run's own saves, logs, screenshots and options live in the instance's appdata under `.mcp-run`.

## Reporting
For each thing tested: what you did (tools and key arguments), what you expected, what happened, and the evidence (state values, event, screenshot path, log lines). Separate clearly: game or mod bug, tool problem (wrong result, missing capability, had to fall back to `lua_exec`), and anything you could not test.
