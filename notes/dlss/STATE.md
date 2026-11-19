# DLSS branch STATE
Scope: branch `dev-mt-mcp-dlss` (clone `dev-mt-mcp-dlss/`). All DLSS/FSR notes live in `notes/dlss/` only; do not touch `notes/STATE.md` / `MAP.md` (kept identical to `dev-mt-mcp`).
Files: `STATE.md` (this, â‰¤20 lines, overwrite) Â· `HANDOFF.md` (full handoff for a fresh session: open work, suspects, rules) Â· `BRIEFING.md` (summary for the user) Â· `PLAN.md` (goals D1-D13) Â· `pipeline.md` Â· `shaders.md` Â· `sdk.md` Â· `bridge.md` Â· `options.md`.

RESUMED 2026-11-19 02:25 (unattended). Fresh session: follow `HANDOFF.md` (old dev agents cannot be messaged; spawn fresh ones).
Running: Dev A = merging the branch head into `dlss-a` (conflict in `r4_dlss.h` with D6's `init_failed()`), then D13, in `wt-dlss-a` · Dev B = D12 in `wt-dlss-b`. D4 done on `dlss-a` as `1cc5ff7b`, NOT yet on the main branch.
Done (merged): D1 `28c655cb` Â· D2 `52c804e2` Â· D3 `02b2874f` Â· D7 `0f3f3b45` Â· D8 `f0cc4f7a` + `115cf528` Â· D10 `84c8950e` Â· D11 `e17b4262` Â· D4 partial `ceeeadff` Â· D6 `ccabc56d` (merge `f66628b0`).
Next, track A: fast-forward main to `dlss-a` when Dev A reports → D5. Track B: D12 (park if it grows) → merge.
Then D9 soak â†’ wrap-up (final build, pack all gamedata into the 00 db0, delete `01_dlss_b_test.db0`, deploy to `C:\Anomaly\bin_dlss`, remove worktrees).
Parked: -
Decisions (reasons in BRIEFING / HANDOFF): no push (vendor SDK files, user decides) Â· bridge forces borderless Â· forward/HUD pass stays before the upscale Â· `set_xform_world` previous-matrix fix kept Â· no SSS = upscaler unavailable Â· `r4_dlss_preset default` = M (K gives black raw output on RTX 2080 + D3D11 even on a fresh NGX instance; opt-in) Â· no DLSS bias mask Â· Reflex caps the frame rate when a cap is set Â· tests mainly on `C:\AnomalyMO2`, GAMMA short and exclusive.
SDK OK: DLSS SDK 310.9.16, FidelityFX SDK 2.4.0, NvAPI R620 (local/private use). Test GPU: RTX 2080 SUPER 8 GB.
Tester gotchas: pass `instance` explicitly (default can be another agent's game) Â· mouse wheel over the options list changes sliders.
