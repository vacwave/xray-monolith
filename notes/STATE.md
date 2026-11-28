# STATE (clean rebase 2026-11-28)
Workflow: agents coder / developer / reader / investigator / playtest; root CLAUDE.md. Max 2 subagents at once (nested count); investigator effort medium. Push / PR only when decided with the user; user opens upstream PRs. gh works (read-only upstream).

## Last goal: CLEAN1 done 2026-11-28 (user): both forks = upstream/dev-mt 7e27ed3d2 + MCP only, to see what dev-mt still needs fixed.
- dev-mt-mcp HEAD e6b0f000d = 7e27ed3d2 + 2e9aa9e43 "Add MCP layer" + notes. All local fixes/opts dropped (perf commits, axr_main AX1, SH1, LR1d detector, shutdown precache_light fix, missing-parent save fatal, leg IK, sound ogg, sky/envmap). gamedata = upstream. Built clean 11-28 08:53.
- Kept mixed MCP+fix hunks: log.cpp empty-queue guard (FlushLogSync needs it), r__screenshot.cpp + r4 DoAsyncScreenshot fix (screenshot tool needs it), xr_ioc_cmd.h null-tokens guard.
- dev-mt-trace HEAD 9082040ef = 7e27ed3d2 + trace MCP commit (cherry of 67e2472f5). Dropped axr_main 7ac18bef/a0fe497d; #892/#893 now upstream. Built clean 11-28 08:47.
- Backups (local): dev-mt-mcp-old-2026-11-28 (c08b9989b + ae786c03f notes/user.ltx), dev-mt-trace-old-2026-11-28 (a0fe497d). Old fixes recoverable from there.
- origin/dev-mt-mcp diverged (ahead 98 / behind 158): pushing needs force; decide with the user. Pre-clean deploys kept as *.pre_clean.
- G1+G2 DONE 11-28: shader pin fix = upstream PR #899 (vacwave:dev-3dss-scopefix 4d84b6760 on upstream dev aa535f4c4, folder D:\projects\xray-monolith-mcp\dev-3dss-scopefix). Local 595ad827d/74ac9cbc5 reverted, PR commit cherry-picked: dev-mt-mcp 1be1c4c37, dev-mt-trace 6b15b7ef9 (only diff: no comment). ADS hitches 11 -> 0 (perfwork.md).
- Deployed 11-28 10:40: bin_mcp AnomalyDX11AVX.exe = 1be1c4c37 (old *.pre_pr899.exe), AnomalyDX11AVX_trace.exe = 6b15b7ef9 (old *_trace.pre_g1.exe). User override: C:\Anomaly\bin\AnomalyDX11AVX.exe = dev-mt-3dss-scopefix build 3fe34d561 (no MCP; dev build kept as .exe.dev_scopefix); the original exe there was lost (no backup, *.orig 11-26 existed).
- G3 DONE 11-28: AX1 = upstream PR #901 (vacwave:dev-lua-callback 54a1c1679+ed107dd17 on dev aa535f4c4; dev-mt has the same axr_main). Cherry-picked: dev-mt-mcp 2f8dc6e21+1595c618a, dev-mt-trace 4919dd4d4+6fd651e1f. A/B 3x3: 0.1% 42->53, 1% 77->85 (perfwork.md). Installed db0 = .ax1_B = repo gamedata (verified identical); base kept as .pre_ax1.
- PRs open (user, assume accepted): #899 dev scopefix, #900 dev-mt scopefix (3fe34d561 == our MT commits), #901 dev+dev-mt Lua callbacks. Folders dev-3dss-scopefix, dev-mt-3dss-scopefix, dev-lua-callback.
- Next: crash-fix round 2 (C7), re-apply other dropped fixes one goal at a time.

## Repos
- Upstream: our #887-#893 merged; #856/#857 open. Record: notes/upstream.md. Fork PRs #115-#133 = reference only.
- Stale local branches: perf-bundle-1, pr-* (on origin). Delete only on the user's word.
- dev-mt-mcp-dlss: be743bae, synced 11-22, behind upstream (VS2022 sln). D:\projects\dev-mt-dlss clone gone; re-clone before porting.
- MCP bug: with `log_timestamps 1`, lua_exec / console_exec stall main 2 s (mcp_server.cpp). Not fixed.
