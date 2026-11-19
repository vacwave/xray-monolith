# DLSS branch STATE
Scope: branch `dev-mt-mcp-dlss` (clone `dev-mt-mcp-dlss/`). All DLSS/FSR notes live in `notes/dlss/` only; do not touch `notes/STATE.md` / `MAP.md` (kept identical to `dev-mt-mcp`).
Files: `STATE.md` (this, ≤20 lines, overwrite) · `HANDOFF.md` (full handoff for a fresh session: open work, suspects, rules) · `BRIEFING.md` (summary for the user) · `PLAN.md` (goals D1-D13) · `pipeline.md` · `shaders.md` · `sdk.md` · `bridge.md` · `options.md`.

RESUMED 2026-11-19 02:25 (unattended). Fresh session: follow `HANDOFF.md` (old dev agents cannot be messaged; spawn fresh ones).
Running: D5 dev in `wt-dlss-a`: D5 committed as `2dccad3b` on `dlss-a`, NOT merged; last verification session running (3DSS ACOG dark / blurred screen under DLSS M seen on a test build; `-ltx fgdlss.ltx` crash at load, null `rt_Generic_2` in `phase_ssfx_volumetric_blur`). Clean → merge; not clean → park D5 on `dlss-a`. `wt-dlss-b` idle at `dc0ed10b`.
Done (merged): D1 `28c655cb` · D2 `52c804e2` · D3 `02b2874f` · D7 `0f3f3b45` · D8 `f0cc4f7a` + `115cf528` · D10 `84c8950e` · D11 `e17b4262` · D4 partial `ceeeadff` · D6 `ccabc56d` · D12 `c8a39779` · D4 `1cc5ff7b` · D13 `c0f62d67` (merge `dc0ed10b`).
Next: merge D5 → D9 soak.
Then D9 soak → wrap-up (final build, pack all gamedata into the 00 db0, delete `01_dlss_b_test.db0`, deploy to `C:\Anomaly\bin_dlss`, remove worktrees).
Open (user): NGX restart per `vid_restart` hung one GAMMA launch (dump in `dev-mt-mcp\.mcp-run\gamma-1\dumps\`) · 2 unexplained one-off GAMMA frames (magenta, red; 0 of 252 in the re-run) · D12 UI on presented frames unseen.
Decisions (reasons in BRIEFING / HANDOFF): no push (vendor SDK files, user decides) · bridge forces borderless · forward/HUD pass stays before the upscale · `set_xform_world` previous-matrix fix kept · no SSS = upscaler unavailable · `r4_dlss_preset default` = M (K gives black raw output on RTX 2080 + D3D11 even on a fresh NGX instance; opt-in) · no DLSS bias mask · Reflex caps the frame rate when a cap is set · tests mainly on `C:\AnomalyMO2`, GAMMA short and exclusive.
SDK OK: DLSS SDK 310.9.16, FidelityFX SDK 2.4.0, NvAPI R620 (local/private use). Test GPU: RTX 2080 SUPER 8 GB.
Tester gotchas: pass `instance` explicitly (default can be another agent's game) · mouse wheel over the options list changes sliders.
