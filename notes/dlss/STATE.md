# DLSS branch STATE
Scope: branch `dev-mt-mcp-dlss` (clone `dev-mt-mcp-dlss/`). All DLSS/FSR notes live in `notes/dlss/` only; do not touch `notes/STATE.md` / `MAP.md` (kept identical to `dev-mt-mcp`).
Files: `STATE.md` (this, ≤20 lines, overwrite) · `HANDOFF.md` (run rules) · `PLAN.md` (decisions + goals D1-D9) · `pipeline.md` · `shaders.md` · `sdk.md`.

Status 2026-11-18 evening: overnight unattended run STARTED. Worktrees `wt-dlss-a` (branch `dlss-a`) and `wt-dlss-b` (`dlss-b`) from c3c6aa64.
Running: Dev A = D2 (temporal inputs) · Dev B = D8 phase 1 (SDK, FG swapchain pass-through, HUD-less image; phase 2 after D1 + D2 merge). Morning summary: `BRIEFING.md`.
Queue: Dev A D1 → D2 → D3 → D4 → D5 → D6 · Dev B D7 → D8 (needs D1 + D2 merged) · then D9.
Done: D7 `0f3f3b45` · D1 `28c655cb` (branch head after merges `1339888a`+; D1+D7 together first built by the D2 dev).
Parked: -
User orders at start (2026-11-18): machine is ours alone; usage limit → favour track A, continue B after; vanilla `C:\Anomaly` is the default test game (cheap, vanilla games may run in parallel), GAMMA only where the Test line needs it (one at a time, never next to another game); settings edits allowed (added `mcp__anomaly__*`, WebFetch/WebSearch, `C:\AnomalyMO2`, temp dir to `.claude/settings.json`).
Decisions taken during the run:
- Push: user now allows push/PR on this branch, but the SDK licence note ("no push until licences are added") still stands once D3/D8 add vendor files → coordinator does NOT push tonight; user decides in the morning.
- Bridge on → exclusive fullscreen refused (forced borderless + log line), done in D8 phase 1.
- Pre-existing `rs_screenmode windowed` crash in hidden MCP runs (`CHW::UpdateViews`): not fixed, reported in BRIEFING.
For the user in the morning: see `BRIEFING.md`.
SDK OK: YES for the DLSS SDK and the AMD FSR SDK (local/private use).
Test GPU: RTX 2080 SUPER 8 GB (DLSS upscaling yes: preset K default, M/L heavy; DLSS frame generation no; FSR FG 3.1.x expected to run).
