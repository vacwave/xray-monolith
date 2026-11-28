# Fixes register (written 2026-11-28 after the clean rebase on upstream/dev-mt 7e27ed3d2)
Every fix we made before the rebase, with where it is now. All old branches (dev-mt-mcp-old-2026-11-28, old origin/dev-mt-mcp 8f64f89d7, perf-bundle-1, dev-mt-trace-old, pr-*, pr/*) were deleted 11-28 (user). Only copy of the commits: `D:\projects\xray-monolith-mcp\perf-baseline\old-fixes-2026-11-28.bundle` (`git bundle list-heads <file>`, then `git fetch <file> refs/keep/old-dev-mt-mcp-backup:tmp` and cherry-pick the hash below). Measurements per ID: `perf-baseline\results.md` and `perfwork.md`.
"apply" = `git apply --check` of the old commit on dev-mt-mcp 80e2589d1: clean = cherry-pick should work, conflict = redo by hand.
Re-apply one goal at a time; after a fix goes upstream or is re-applied, update its line here.

## In upstream now (nothing to do)
- #887 RN1 MT rain dupe; #888 LD1 bump wait; #889 X1-X4 XML load (648ef8c5f 54827953b 553c7aede 565188276); #890 C1 C2 C5 PS1 PS2 crash fixes (4c61629cb physics block allocator, 1d1895307 sound emitter UAF, fad01f7fb ScriptGlow dtor, 704f92b7e zone/stalker update before actor, 7f267b3da agent manager lifetime); #892 ST4 LuaJIT GC udata walk (upstream version; ours = 57e7096f3 + dd4c52261 prefetch, the prefetch part may not be in it: check before re-applying); #893 ST2 CActor_OnChangedSlot actor only (4c4bbd03a); 8c37e2d6c leg IK root callback (= our fork #113, upstream code identical).

## Upstream PR open, applied locally (drop the local copy if he merges something different)
- #899 dev / #900 dev-mt G1 3DSS ADS texture reload: local 1be1c4c37. Supersedes SH1 c08b9989b (VS/PS pin; not re-applied).
- #901 AX1 axr_main callbacks sort once + lazy reorder: local 2f8dc6e21 + 1595c618a (= old 7ac18befd + 03d2b8bd2).
- #856 dev / #857 dev-mt A-Life scheduled_time_limit: NOT applied locally (branches origin/dev-alifeperf, dev-mt-alifeperf).

## Kept inside the MCP commit (mixed hunks, must stay)
- log.cpp empty logger-queue pop guard (part of 698808a25); r__screenshot.cpp + R4 DoAsyncScreenshot fix; xr_ioc_cmd.h null-tokens guard.

## Dropped: stability / crash (re-apply first)
- 698808a25 shutdown heap corruption: release precache light before teardown (quit within 60 precache frames after an in-level load; engine.md). Conflict (only the log.cpp half survived). Upstream bug.
- a60c8aeb7 clear fatal for saves with missing parent objects (hunk inside the game_save MCP commit; redo by hand).
- 9657a2ebf + 3f7704963 LR1d cross-thread Lua VM entry detector (debug aid, logging.md LR1). Conflict. Only if LR1 recurs.
- Open, never fixed: C3 C4 C6 (logging.md), PS3 PS4, SE1-SE10 (perfwork.md "Stability"), LR1 (parked). C7 = next crash round (scope not written down yet).

## Dropped: perf, apply clean
- Load: P1 30ababf69 CLevelGraph::vertex far-node skip (superseded by bfc8b476c, see conflicts); L1 076e840d0 fsgame rescan skip; L2 566947d2d unlocalizer regex once; L7 c8ef8dc87 FindExInfoBasic; LD3 de51beae6 restrictor border z-skip; LD5 601171129 patrol path sort once; LD6 8bd846ab4 line_exist one lookup; LD7 4918244f6 collision tree centres once; ST5 f9759b1b1 tutorials xml behind load screen.
- Frame: R1 914e55b67 grass sector test once; R2 d0c85d895 sampler slots changed-only; R5 3c4e7b710 CB dirty only on change; R7 0caae14fd vertex layout memo; R8 fc9960438 static capture; R9 abb9921e9 state cache compare; R10 081befc23 rain normalize visible only; V8 1c4e0d21e light xform skip; V9 d401d3a8d frustum force-inline; V10 67b9533b6 set_Textures clear to highest slot; U1 11b611248 pickup scan clip 25 m; U2 0d616b2af sky/envmap surface unchanged; F1 de78e126e script storage no _G scan; F2 c2ba1cc20 spatial query by ref; F4 270f7104d GetCustomStatic intern; F6 0845ecb0c auto_load missing names; F7 ff652626d pickup virtual cast; ST3 48bc276ff HUD hand cycle map.
- Lua/luabind: U3 992b64194 get_class_rep cache; U4 9ad819895 in-place small values; F8 a71489a54 ctor back reference only for wrapped classes.
- Sound (user 11-26: keep local, no PR): ST10 1c34d1443 lazy ogg open; V1v e734cc141 vorbisfile backward step.

## Dropped: perf, conflict (redo by hand on the new base)
- R3 41f943ca5 physics debug draw skip (Level.cpp); R4 a35a406da CB slot reassign + bound list; R6 3334c5193 sampler anisotropy variants; V1 55ad6d78f set_Constants handler list; V2 d28c44918 skinning one upload; V3 5fe55b9c8 static light constant names; F5 d2975cfd8 luabind pcall error handler reuse; F10 28b8e532d xrServer ID_to_entity flat array; bfc8b476c CLevelGraph::vertex row search; dd4c52261 GC prefetch (vs #892).

## Older fork PRs #88-#108 (11-14/15, base dev-mt-perftest, never in dev-mt-mcp unless noted; bundle refs refs/keep/pr-<name>)
- Covered elsewhere: lua-1 object scan (= F1), lua-3 pcall errfunc (= F5), load-1 fs rescan (= L1), load-2 unlocalizer (= L2), load-5 XML single parse (= X1, upstream #889), cpu-2 custom static (= F4).
- Only on these branches: lua-2 resolve script functions in one namespace walk; load-3 fewer per-line allocs in LTX parse; load-4 no section copies in DLTX evaluate (= candidate I1); load-6 FS init stops parent walk at first registered folder; load-7 cube textures keep full mip chain; load-8 unaligned BC6H/BC7 loaded uncompressed instead of re-encode; load-9 request only remaining mips with reduced lod; cpu-1 frame limiter sleeps instead of spinning in menus/pause; cpu-3 xalloc default construct/destroy so containers move; cpu-4 ID_Cycle_Safe name lookup cache (= candidate PR107); alife-1 skip saved objects whose parent failed to load (stability, pairs with the missing-parent save fatal); lua-4 lua_parallel_gc_pause cvar (GC pause tuning: rejected).
- Bundles #97 load-fixes, #98 lua-fixes, #108 cpu-fixes = the same commits combined.

## Never adopt again (user decisions)
- E1 exist() cache (rejected 11-26); LT1 mem_usage HeapWalk (noise); L4, V4-V7, U5, U8, S2 (results.md "Rejected"); build-HUD-data-only-for-actor (11-26, no gain).
