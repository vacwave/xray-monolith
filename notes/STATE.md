# STATE (fresh session 2026-11-28)
Workflow: root CLAUDE.md; agents coder / developer / reader / investigator / playtest, max 2 at once. Push / PR only when decided with the user; the user opens upstream PRs. gh works (read-only upstream).

## Now
- dev-mt-mcp = upstream/dev-mt 7e27ed3d2 + MCP (2e9aa9e43) + local #899 (1be1c4c37) + #901 (2f8dc6e21, 1595c618a) + notes. Upstream dev-mt has not moved since.
- dev-mt-trace = 7e27ed3d2 + trace MCP + #899 (6b15b7ef9) + #901 (4919dd4d4, 6fd651e1f). trace.md.
- Deployed C:\Anomaly\bin_mcp: AnomalyDX11AVX.exe = 1be1c4c37, AnomalyDX11AVX_trace.exe = 6b15b7ef9; db0 = repo gamedata incl. AX1. C:\Anomaly\bin\AnomalyDX11AVX.exe = user's scopefix build 3fe34d561 (no MCP; original lost).
- Upstream: #899 #900 #901 #856 #857 open; everything else of ours merged (upstream.md).
- All dropped fixes with status and re-apply notes: fixes.md.

## Queue
1. Crash-fix round 2 (C7): pick from fixes.md "stability" (precache_light shutdown, missing-parent save fatal) + open C3 C4 C6.
2. Re-apply dropped perf fixes one goal at a time (fixes.md; clean ones first). Upstream-worthy ones go as PRs per upstream.md bucketing.

## Repo housekeeping
- Fork PRs #114-#133 closed 11-28. All old pr-*, pr/*, perf-bundle-1 and *-old-2026-11-28 branches deleted (user); commits only in perf-baseline\old-fixes-2026-11-28.bundle (fixes.md).
- Kept: upstream PR branches (dev-*, dev-mt-*). Folders dev-3dss-scopefix, dev-mt-3dss-scopefix, dev-lua-callback = open upstream PR clones (remove after merge).
- origin/dev-mt-mcp force-pushed 11-28 (user) = local dev-mt-mcp (old tip 8f64f89d7 is in the bundle).
- dev-mt-mcp-dlss be743bae, behind upstream (VS2022 sln). D:\projects\dev-mt-dlss clone gone; re-clone before porting.
- MCP bug: with `log_timestamps 1`, lua_exec / console_exec stall main 2 s (mcp_server.cpp). Not fixed.
