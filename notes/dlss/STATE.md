# DLSS branch STATE
Scope: branch `dev-mt-mcp-dlss` (clone `dev-mt-mcp-dlss/`, local only, NOT pushed: user 2026-11-18 "not yet"). All DLSS/FSR notes live in `notes/dlss/` only; do not touch `notes/STATE.md` / `MAP.md` (kept identical to `dev-mt-mcp`).
Files: `STATE.md` (this, ≤20 lines, overwrite) · `HANDOFF.md` (how to run the overnight session) · `PLAN.md` (decisions + goals D1-D9 with substeps and tests) · `pipeline.md` · `shaders.md` · `sdk.md`.

Status 2026-11-18: plan APPROVED by the user (D9 soak cut to ~10 min). Nothing coded. Next session = unattended overnight run with 2 devs: follow `HANDOFF.md`.
SDK OK: YES (user 2026-11-18) for both the DLSS SDK and the AMD FSR SDK. Local and private use only for now; if it ever goes public the user adds the proper licences first, so still no push.
Queue: Dev A D1 → D2 → D3 → D4 → D5 → D6 · Dev B D7 → D8 · then D9.
Done: -
Parked: -
Decisions taken during the run: -
For the user in the morning: -
Test GPU: RTX 2080 SUPER 8 GB (DLSS upscaling yes: preset K default, M/L heavy; DLSS frame generation no; FSR FG 3.1.x expected to run).
