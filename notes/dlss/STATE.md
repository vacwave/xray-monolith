# DLSS branch STATE
Scope: branch `dev-mt-mcp-dlss` (clone `dev-mt-mcp-dlss/`). All DLSS/FSR notes live in `notes/dlss/` only; do not touch `notes/STATE.md` / `MAP.md` (kept identical to `dev-mt-mcp`).
Files: `STATE.md` (this, ≤20 lines, overwrite) · `HANDOFF.md` (full handoff for a fresh session: open work, suspects, rules) · `BRIEFING.md` (summary for the user) · `PLAN.md` (goals D1-D13) · `pipeline.md` · `shaders.md` · `sdk.md` · `bridge.md`.

PAUSED 2026-11-19 00:53 by the user (usage limit); continue from 02:25 Helsinki time. Fresh session: follow `HANDOFF.md`.
Worktrees `wt-dlss-a` (`dlss-a`) and `wt-dlss-b` (`dlss-b`) hold UNCOMMITTED work + a `notes\plan.md` each: do not reset them. No game running.
Done (merged): D1 `28c655cb` · D2 `52c804e2` · D3 `02b2874f` · D7 `0f3f3b45` · D8 `f0cc4f7a` + `115cf528` · D10 `84c8950e` · D11 `e17b4262` · D4 partial `ceeeadff` (merge `4e933260`).
Next, Dev A: D4 blocker (DLSS dark / white frames; comes back after the second `vid_restart` = feature release / recreate; state-cache clear alone did not fix it) → effect walk → D13 → D5.
Next, Dev B: finish D6 (status lines empty, Reflex "not NVIDIA" false report) → D12.
Then D9 soak → wrap-up (final build, pack all gamedata into the 00 db0, delete `01_dlss_b_test.db0`, `C:\Anomaly\bin_dlss`, remove worktrees).
Parked: -
Decisions (reasons in BRIEFING / HANDOFF): no push (vendor SDK files, user decides) · bridge forces borderless · forward/HUD pass stays before the upscale · `set_xform_world` previous-matrix fix kept · no SSS = upscaler unavailable · `r4_dlss_preset default` = M until K is proven clean · no DLSS bias mask · Reflex caps the frame rate when a cap is set · tests mainly on `C:\AnomalyMO2`, GAMMA short and exclusive.
SDK OK: DLSS SDK 310.9.16, FidelityFX SDK 2.4.0, NvAPI R620 (local/private use). Test GPU: RTX 2080 SUPER 8 GB.
