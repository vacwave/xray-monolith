# DLSS branch STATE
Scope: branch `dev-mt-mcp-dlss` (clone `dev-mt-mcp-dlss/`). All DLSS/FSR notes live in `notes/dlss/` only; do not touch `notes/STATE.md` / `MAP.md` (kept identical to `dev-mt-mcp`).
Files: `STATE.md` (this, ≤20 lines, overwrite) · `HANDOFF.md` (full handoff for a fresh session: open work, suspects, rules) · `BRIEFING.md` (summary for the user) · `PLAN.md` (goals D1-D13) · `pipeline.md` · `shaders.md` · `sdk.md` · `bridge.md` · `options.md`.

RESUMED 2026-11-19 02:25 (unattended). Fresh session: follow `HANDOFF.md` (old dev agents cannot be messaged; spawn fresh ones).
Running: dev in `wt-dlss-a` = finish the user-ordered sight fixes: (1) thermal sights show no heat image on GAMMA, DLSS on AND off (A/B against the pre-DLSS exe `C:\Anomaly\bin` first: regression or not), (2) 3DSS lens band while strafing under DLSS. `wt-dlss-b` idle. Nothing unmerged; both worktrees at the head `03a5d202`.
Done (merged): D1 `28c655cb` · D2 `52c804e2` · D3 `02b2874f` · D7 `0f3f3b45` · D8 `f0cc4f7a` + `115cf528` · D10 `84c8950e` · D11 `e17b4262` · D4 partial `ceeeadff` · D6 `ccabc56d` · D12 `c8a39779` · D4 `1cc5ff7b` · D13 `c0f62d67` · D5 `2dccad3b` · empty-DLSS-output recovery `33b6a82b` · no-`renderer`-line startup fix `f203d4a1` + `c50dceb9` · 3DSS lens mask for DLSS `1938ad7a` (merge `03a5d202`).
Next: merge → D9 soak → wrap-up.
Then D9 soak → wrap-up (final build, pack all gamedata into the 00 db0, delete `01_dlss_b_test.db0`, deploy to `C:\Anomaly\bin_dlss`, remove worktrees).
Open (user): one heap-corruption exit code (0xC0000374) at shutdown after a start without any config, seen once, no dump · NGX restart per `vid_restart` hung one GAMMA launch (dump in `dev-mt-mcp\.mcp-run\gamma-1\dumps\`) · 2 unexplained one-off GAMMA frames (magenta, red; 0 of 252 in the re-run) · D12 UI on presented frames unseen.
Decisions (reasons in BRIEFING / HANDOFF): no push (vendor SDK files, user decides) · bridge forces borderless · forward/HUD pass stays before the upscale · `set_xform_world` previous-matrix fix kept · no SSS = upscaler unavailable · `r4_dlss_preset default` = M (K gives black raw output on RTX 2080 + D3D11 even on a fresh NGX instance; opt-in) · no DLSS bias mask · Reflex caps the frame rate when a cap is set · tests mainly on `C:\AnomalyMO2`, GAMMA short and exclusive.
SDK OK: DLSS SDK 310.9.16, FidelityFX SDK 2.4.0, NvAPI R620 (local/private use). Test GPU: RTX 2080 SUPER 8 GB.
Tester gotchas: pass `instance` explicitly (default can be another agent's game) · mouse wheel over the options list changes sliders.
