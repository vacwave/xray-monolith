# STATE (cleanup 2026-11-26)
Workflow: agents coder / developer / reader / investigator / playtest; root CLAUDE.md. Max 2 subagents at once (nested count); investigator effort medium. Push / PR only when decided with the user; user opens upstream PRs. gh works (read-only upstream).

## Last goal: SH1 (below). Before: dev-mt-trace fork done 2026-11-27; + #893 cherry-pick 642363c4 (built, deployed bin_mcp\AnomalyDX11AVX_trace.exe). Trace lacks 7ac18bef (axr_main sort cache). Detail: notes/trace.md. Previous: E1 rejected 11-26 (notes/perfwork.md).
- Done: LR1d detector 3f770496+9657a2eb (dev-mt-mcp). AX1 03d2b8bd axr_main lazy callback reorder (A/B done in GAMMA; in dev-mt-trace as a0fe497d with 7ac18bef; notes/perfwork.md).
- SH1 done 2026-11-27 c08b9989 (pin compiled VS/PS until level unload; 3DSS shader swap 5.3 -> 0.65 ms, p99 9.61 -> 8.27 ms in GAMMA A/B; real-ADS ACOG A/B: 3DSS apply 4.2 -> 1.0 ms, p99 9.26 -> 8.51; perfwork.md). Not pushed. bin_mcp not updated (exes in C:\Anomaly\bin_sh1_off / bin_sh1_on). Open: ON present +0.2 ms/frame in all 6 ON runs, unexplained; 80 ms spike never reproduced (not the warm reload).
- LR1 parked 2026-11-27 (user): one-off GAMMA load crash, 0/300 in loops; detector 9657a2eb in dev-mt-mcp only. If it happens again: note it in logging.md (save, log, dump, any [LUA MT] line), then revisit; issue draft perf-baseline\crash_2026-11-27_luaload\ISSUE_DRAFT.md not posted (overstates cause).
- AX1 A/B done (perfwork.md): small Lua gain, burst not reproduced. C:\Anomaly\db\mods db0 = trace ON pack (= pre_trace_ab + axr_main 7ac18bef+03d2b8bd); dev-mt-trace a0fe497d has both.
- Crash-fix round 2 still open (C7 strongest; candidates.md "Stability").

## Repos (2026-11-26)
- dev-mt-mcp HEAD e734cc14: upstream/dev-mt fully merged (cc1fe272; upstream unchanged since). 83 ahead of origin/dev-mt-mcp, not pushed. Local-only work: merge + 9 commits now identical to upstream #889 / #890 + ST10 1c34d144 + V1v e734cc14 (no PR, user). Uncommitted: mcp/templates/user.ltx, notes/.
- Upstream: #887-#890 merged (11-25/26), #856/#857 open; our full record in notes/upstream.md. Fork PRs #115-#133 open = reference only (contents already in dev-mt-mcp).
- Stale local branches: perf-bundle-1 (11-21, unpushed), pr-* (all on origin). Delete only on the user's word.
- dev-mt-mcp-dlss: be743bae = origin, synced to upstream 11-22 (69 commits behind upstream/dev-mt now; VS2022 sln). Its shared notes/ copy is stale by design. Untracked empty file alife_simulator_script.cpp in its root.
- D:\projects\dev-mt-dlss local clone is gone; origin/dev-mt-dlss 49861e0c (synced 11-22). Re-clone before porting DLSS fixes.

## Deploy
- C:\Anomaly\bin_mcp\AnomalyDX11AVX.exe built 11-26 10:55 (= e734cc14). C:\Anomaly\bin holds a crashfixes build + *.pre_* backups; db\mods has 5 old db0 backups. Old A/B folders: bin_e1_off/on (E1), bin_lt1, bin_slot_off/on, bin_xsnd_base/new.
- MCP bug: with `log_timestamps 1`, lua_exec / console_exec stall main 2 s (mcp_server.cpp:86 / 569). Not fixed.
