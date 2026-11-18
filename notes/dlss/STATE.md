# DLSS branch STATE
Scope: branch `dev-mt-mcp-dlss` (clone `dev-mt-mcp-dlss/`). All DLSS/FSR notes live in `notes/dlss/` only; do not touch `notes/STATE.md` / `MAP.md` (kept identical to `dev-mt-mcp`).
Files: `STATE.md` (this, ≤20 lines, overwrite) · `BRIEFING.md` (morning summary for the user) · `HANDOFF.md` (run rules) · `PLAN.md` (goals D1-D9) · `pipeline.md` · `shaders.md` · `sdk.md` · `bridge.md`.

PAUSED 2026-11-19 00:53 by the user (usage limit); resume 02:25 (session cron, one-shot; if the session died, resume by hand: both devs left `notes\plan.md` in their worktrees with uncommitted work).
Status: overnight unattended run (started 2026-11-18). Worktrees `wt-dlss-a` (`dlss-a`), `wt-dlss-b` (`dlss-b`), both started their current goal from `ae2ddcdd`.
Running: Dev A = D4 completion (partial commit `ceeeadff` merged; BLOCKER: intermittent dark/white whole frames with DLSS, K and M; coordinator's cause = stale RCache/D3D state after NGX evaluate → ClearState + Invalidate after it, plus guard on `output_rts_swapped`; re-briefed once, 2 sessions; if it fails → park D4 sign-off, DLSS stays experimental) · then D13 (overlays after upscale) → D5 · Dev B = D6 (options menu; its test gamedata goes into a separate `C:\Anomaly\db\mods\01_dlss_b_test.db0` → delete at the final build, after packing everything into the 00 db0).
Queue (user added D10-D12 + D4/D6 extras on 2026-11-19, see PLAN "Added by the user"): Dev A D3 → D4 → D5 · Dev B D11 → D10 (sharpening, after D3 merges) → D6 (menu) → D12 (UI texture, park if big) · then D9.
Done (merged): D7 `0f3f3b45` · D1 `28c655cb` · D8 phase 1 `f0cc4f7a` · D2 `52c804e2` · D8 phase 2 `115cf528` · D3 `02b2874f` (preset K broken → `default` = M for now) · D11 `e17b4262` · D10 `84c8950e` (head `28331279`+).
Parked: -
User orders at start: machine is ours alone; usage limit → favour track A, continue B after; vanilla `C:\Anomaly` is the default test game (vanilla games may run in parallel), GAMMA only where needed (one at a time, never next to another game); settings edits allowed; chat output minimal, summary goes to `BRIEFING.md`.
Decisions taken during the run (reasons in BRIEFING):
- No push tonight (SDK licence note stands; user decides).
- Bridge on → exclusive fullscreen refused (borderless + log line).
- Forward/HUD pass stays before the upscale slot.
- `set_xform_world` previous-matrix fix accepted (changes today's frame slightly for SSS users).
- No SSS (vanilla) → upscaler / FG interpolation "unavailable"; DLSS + FG tests need GAMMA, serialized.
- User (during the run): SSS + MCM now enabled in the vanilla MO2 profile `C:\AnomalyMO2` → DLSS / FG tests run there (parallel OK), GAMMA only for one short final check per goal.
- Gamedata db0 in `C:\Anomaly\db\mods` owned by Dev A; backup `.bak_pre_dlss` exists.
For the user in the morning: `BRIEFING.md`.
SDK OK: YES for DLSS SDK + AMD FSR SDK (local/private). Test GPU: RTX 2080 SUPER 8 GB.
