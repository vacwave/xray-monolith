# STATE (clean rebase 2026-11-28)
Workflow: agents coder / developer / reader / investigator / playtest; root CLAUDE.md. Max 2 subagents at once (nested count); investigator effort medium. Push / PR only when decided with the user; user opens upstream PRs. gh works (read-only upstream).

## Last goal: CLEAN1 done 2026-11-28 (user): both forks = upstream/dev-mt 7e27ed3d2 + MCP only, to see what dev-mt still needs fixed.
- dev-mt-mcp HEAD e6b0f000d = 7e27ed3d2 + 2e9aa9e43 "Add MCP layer" + notes. All local fixes/opts dropped (perf commits, axr_main AX1, SH1, LR1d detector, shutdown precache_light fix, missing-parent save fatal, leg IK, sound ogg, sky/envmap). gamedata = upstream. Built clean 11-28 08:53.
- Kept mixed MCP+fix hunks: log.cpp empty-queue guard (FlushLogSync needs it), r__screenshot.cpp + r4 DoAsyncScreenshot fix (screenshot tool needs it), xr_ioc_cmd.h null-tokens guard.
- dev-mt-trace HEAD 9082040ef = 7e27ed3d2 + trace MCP commit (cherry of 67e2472f5). Dropped axr_main 7ac18bef/a0fe497d; #892/#893 now upstream. Built clean 11-28 08:47.
- Backups (local): dev-mt-mcp-old-2026-11-28 (c08b9989b + ae786c03f notes/user.ltx), dev-mt-trace-old-2026-11-28 (a0fe497d). Old fixes recoverable from there.
- origin/dev-mt-mcp diverged (ahead 98 / behind 158): pushing needs force; decide with the user.
- Deployed 11-28 08:55: bin_mcp AnomalyDX11AVX.exe (= e6b0f000d) and AnomalyDX11AVX_trace.exe (= 9082040ef), + pdbs; db\mods 00_modded_exes_gamedata.db0 = upstream gamedata. Old files kept as *.pre_clean.
- Next: compare plain dev-mt vs issues (crash-fix round 2, C7 strongest; candidates.md "Stability"); re-apply fixes one goal at a time.

## Repos
- Upstream: our #887-#893 merged; #856/#857 open. Record: notes/upstream.md. Fork PRs #115-#133 = reference only.
- Stale local branches: perf-bundle-1, pr-* (on origin). Delete only on the user's word.
- dev-mt-mcp-dlss: be743bae, synced 11-22, behind upstream (VS2022 sln). D:\projects\dev-mt-dlss clone gone; re-clone before porting.
- MCP bug: with `log_timestamps 1`, lua_exec / console_exec stall main 2 s (mcp_server.cpp). Not fixed.
