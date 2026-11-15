# Game state
- Actor: `g_actor` / `Actor()` xrGame/Actor_Network.cpp:58,60 (decl Actor.h:924); null in main menu.
- Health `CEntity::GetfHealth()` Entity.h:38; inventory `CInventoryOwner::inventory()` InventoryOwner.h:187.
- Level name `g_pGameLevel->name()` / level_script.cpp:616; game time `CLevel::GetGameTime()` Level.h:331; camera `Device.vCameraPosition`.
- Unpacked vanilla gamedata (scripts, configs) is in `C:\Anomaly\tools\_unpacked\` — read game scripts there.
- New game with a faction = `ui_mm_faction_select.script`: `OnStartGame` writes `axr_main.config` `[character_creation]` (new_game_faction/loadout/map/money/story_mode/...) and `main_menu:StartGame()` runs `start server(all/single/alife/new) client(localhost)` + `main_menu off`; its `on_game_load` applies them on the fake_start load, `actor_on_first_update` `ChangeLevel`s to `new_game_map` (section in `plugins\new_game_start_locations.ltx`: lvid/gvid/x/y/z). No new_game_map → stays on fake_start.
- Faction kit: `items\settings\new_game_loadouts.ltx` `[<f>_loadout]` lines `sec = false,count,points[,eco]` are preselected (incl. `device_pda_1` from `[shared]`); money `[<f>_money] money`. Factions = sections `<f>_start_locations`.
- Level list: `game_maps_single.ltx` `[level_maps_single]` (readable from the main menu). In game: `game_graph():vertex(g):level_id()` → `alife():level_name(id)`; `valid_vertex_id(g)` (no vertex_count in Lua); `SIMBOARD.smarts[id].smrt` = every se_smart_terrain (`m_game_vertex_id`, `props`).
- `ChangeLevel(pos, lvid, gvid, VEC_ZERO)` (_g.script) level change works from `lua.exec`, autosaves first; `db.actor.afterFirstUpdate` = true after the actor binder's first update on the new level.
- `db.actor:set_actor_position(p)` has no checks: a point with `level.vertex_id(p) == 4294967295` (no AI node) drops the actor through the world; level loads are ~6-12 s with a warm shader cache.
