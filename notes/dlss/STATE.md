# DLSS branch STATE
Scope: branch `dev-mt-mcp-dlss` (clone `dev-mt-mcp-dlss/`). All DLSS/FSR notes live in `notes/dlss/` only; do not touch `notes/STATE.md` / `MAP.md` (kept identical to `dev-mt-mcp`).
Files: `STATE.md` (this, ≤20 lines, overwrite) · `BRIEFING.md` (morning summary for the user) · `HANDOFF.md` (run rules) · `PLAN.md` (goals D1-D9) · `pipeline.md` · `shaders.md` · `sdk.md` · `bridge.md`.

Status: overnight unattended run in progress (started 2026-11-18). Worktrees `wt-dlss-a` (`dlss-a`), `wt-dlss-b` (`dlss-b`), both started their current goal from `ae2ddcdd`.
Running: Dev A = D3 (DLSS upscaling) · Dev B = D8 phase 2 (FG inputs + interpolation; adds 2 call lines in `r4_R_render.cpp`).
Queue: Dev A D3 → D4 → D5 → D6 · Dev B D8 phase 2 → (free) · then D9.
Done (merged): D7 `0f3f3b45` · D1 `28c655cb` · D8 phase 1 `f0cc4f7a` · D2 `52c804e2`.
Parked: -
User orders at start: machine is ours alone; usage limit → favour track A, continue B after; vanilla `C:\Anomaly` is the default test game (vanilla games may run in parallel), GAMMA only where needed (one at a time, never next to another game); settings edits allowed; chat output minimal, summary goes to `BRIEFING.md`.
Decisions taken during the run (reasons in BRIEFING):
- No push tonight (SDK licence note stands; user decides).
- Bridge on → exclusive fullscreen refused (borderless + log line).
- Forward/HUD pass stays before the upscale slot.
- `set_xform_world` previous-matrix fix accepted (changes today's frame slightly for SSS users).
- No SSS (vanilla) → upscaler / FG interpolation "unavailable"; DLSS + FG tests need GAMMA, serialized.
- Gamedata db0 in `C:\Anomaly\db\mods` owned by Dev A; backup `.bak_pre_dlss` exists.
For the user in the morning: `BRIEFING.md`.
SDK OK: YES for DLSS SDK + AMD FSR SDK (local/private). Test GPU: RTX 2080 SUPER 8 GB.
