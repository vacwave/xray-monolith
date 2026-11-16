# Anomaly MCP: tester guide

You drive a real Stalker Anomaly / GAMMA game through the `anomaly` MCP tools and test it like a player would. The game runs hidden and muted; you see it only through the tools. Full tool reference: `mcp/README.md` next to this file.

## Session flow
1. **Start:** `game_launch` (vanilla, `C:\Anomaly`) or `game_launch(game_root="C:\\GAMMA")`. Then `game_teleport(level=..., faction=...)` for a new game on a map; it returns when you are in control.
2. **Look:** `game_state` first (cheap: mode, actor, hands, crosshair target, world). `game_screenshot` when you need to see. `game_objects` for what is around you, `game_ui` for what is on screen.
3. **Act** like a player (table below).
4. **Verify** with state, not by assuming: `game_wait` for the expected event or state, then read `game_state` / `game_tasks` / `game_infos`. Check `game_events(types=["script_error*"])` after anything that runs mod code.
5. **Finish:** `game_stop`. Always stop the games you launched.

## Which tool for what
| I want to | Use |
|---|---|
| Walk somewhere | `game_move_to` (position or object id). It opens doors itself and reports them; `blocked_door` = locked, read the reason. |
| Talk to, pick up, open | `game_use(id)`. It aims properly; a raw `use` key often misses. |
| Hold a conversation | `game_talk` to read everything, `game_talk(id=...)` to choose. Prefer phrase `id` over text: ids are the same on vanilla and GAMMA, texts are not. |
| Inventory, trade, menus | `game_ui` to find widgets and item cells, `game_ui_click` to click by text, `game_input` for double-click, right-click and drag (`click`, `drag` steps at the cell `center`). |
| Shooting an NPC | `game_shoot(id, part="chest"\|"head", shots=N or until_dead=true)`. It keeps the aim on the target and fires the weapon in hands normally (real spread: misses at range are normal); read `state`, `hits`, `target.health`; `los_blocked` names the blocker. Walk closer with `game_move_to` yourself. |
| Keys, looking | `game_input` with action names (`fire`, `reload`, `inventory`, `forward` ...). Keys are the defaults. |
| Quest state | `game_tasks` (stage, target, giver, history), `game_infos` (info portions), events `task_given` / `task_stage` / `task_completed` / `task_failed`. |
| Set up a scenario fast | `game_spawn`, `game_give`, `game_actor`, `game_kill`, `game_release`, `game_weather`, `game_emission`, `game_teleport(position=...)`. |
| Wait for something | `game_wait` (events, state, objects, Lua predicate) with `fail_on: ["script_error", "actor_died"]`. Do not poll in a loop or sleep. |
| Save / load | `game_save`, `game_load`, `game_saves`. |
| Anything missing | `lua_exec` / `console_exec`. Say in your report when you needed them: that is a tool gap. |

## Rules that save you time
- **One game at a time.** GAMMA nearly fills the 8 GB GPU; never run vanilla and GAMMA together.
- **GAMMA** starts through Mod Organizer and refuses to launch while the user's own MO2 is open: report that, do not close it. First load takes about 30-45 s. Never change the mod list.
- **Clean faction start:** only from the main menu. In a running game `faction` just relabels the actor; use `game_restart` first, then `game_teleport(faction=...)`.
- **Screenshot coordinates:** `game_input` click/drag and `game_ui` positions are pixels of the last `game_screenshot`. Take a screenshot before using coordinates, and again after the UI changes.
- **Inventory closes itself** for 2-3 s after equipping an outfit or helmet or eating (animation). Reopen it and re-read `game_ui` before the next click.
- **Drag item onto item** only works if the game allows that pair (a scope must be in the weapon's `scopes` list); nothing happens otherwise, so check the result.
- **Artefacts** on the ground are invisible and cannot be picked up without an active detector (detector in its slot, `detector` action), then `game_use`.
- **`game_kill`** needs online targets (near you); `by_actor=true` credits the player so kill quests advance. Offline targets can only be removed with `game_release` (no death, no credit).
- **`task_stage` events** arrive 1-3 s after the change; wait for them instead of reading immediately.
- **Lua errors are dangerous:** an error inside a game callback crashes the game by engine design. After a crash: `game_last_crash` (reason, stack, dump paths), then `game_restart`. On a hang: `game_dump`, then `game_stop`.
- **Do not touch** files in `C:\Anomaly` or `C:\GAMMA` unless the task says so. The run's own saves, logs, screenshots and options live in the instance's appdata under `.mcp-run`.

## Reporting
For each thing tested: what you did (tools and key arguments), what you expected, what happened, and the evidence (state values, event, screenshot path, log lines). Separate clearly: game or mod bug, tool problem (wrong result, missing capability, had to fall back to `lua_exec`), and anything you could not test.
