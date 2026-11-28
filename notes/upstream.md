# Upstream (themrdemonized/xray-monolith): maintainer profile, 2026-11-25 (record updated 2026-11-26)
Source: upstream git history (239 merged PRs since 2026-01) + `gh` reads of PR bodies/comments. Read-only upstream: never comment, open or touch PRs/issues there.

## Written rules
- No CONTRIBUTING, no PR/issue templates. README: fork, build, PR. Feature docs in `docs/*.md` (DXML, axr_main, shader_bus, spawn_overlays, env_mods).
- New engine Lua exports must be documented in `gamedata/scripts/lua_help_ex.script` (engine exports only; script bridges not) (#535).
- Changelog in README is written by the maintainer: `contributor: <PR title> (<link>)` under a date, grouped "Main and MT" / "MT". PR title = the changelog line: write it as one plain user-facing sentence.

## Branches and CI
- `dev` = main (non-MT), `dev-mt` = MT. `retarget-pr.yml` forces PR bases: dev/dev-mt kept, `all-in-one-vs2022-wpo-mt` -> dev-mt, anything else -> dev. He merges dev into dev-mt often.
- Fix in shared code -> PR on `dev` (reaches dev-mt by merge); MT-only code -> `dev-mt`. Pairs (#856 dev / #857 dev-mt) are tolerated but redundant.
- CI `msbuild.yml` builds every PR, all DX8-DX11 (+AVX) configs, packs `00_modded_exes_gamedata.db0`. Since 2026-11-22 (21bedd3e) upstream builds with VS2026: `src/engine-vs2026.sln`, runner `windows-2025-vs2026`. VS2026 is installed here (build.md).

## What gets merged
- Size since 2026-01: median 45 lines / 4 files, p75 164 / 8, p90 722 / 15. Large features are merged as series of separate PRs ("Scripted Weapon Attachments vol. 1-6", #714-#817).
- Perf fixes with evidence go in fast: #688 (WPA trace of squad-spawn hitch, both versions run side by side, 200k lookups 0 mismatches), #777 (386 -> 52 ms animation load), #793 (.seq dedupe), #122 (net_Relcase), #449 (CBuffer Map/Unmap).
- MT-safety fixes (missing lock / join / race) are accepted readily: #705, #660, #657, #576, #845 (ours).
- 3rd-party sources are fine: LuaJIT (#641 jit.profile backport, #670), luabind (#784).
- gamedata scripts are fine, incl. perf (#605 anomaly_restrictor_update) and new callbacks (#535: he asked for a script callback so other mods can use it).
- Behaviour changes ship as options, default off or old behaviour (cvars, `[alife]` ini keys, Modded Exes options).

## What gets rejected or reverted
- Architecture / maintenance burden: #876 seamless level change ("i dont want to maintain this", "total hack", script engine surviving transitions breaks mods even with an off-by-default switch); #878 level preload (13 % on plain Anomaly "not worth it for that miniscule improvement"). He floated: preparing a new Lua state in the background might be worth trying.
- Big AI-written parallel-loading bundles closed without comment: #614 (+9.4k, MT parallel level load), #616 (startup bundle, non-MT).
- Container / data-structure swaps are risky to him: #776 (vector -> hash map) closed on dev, "retarget to MT only", "changed LTX data from vector to set and it lead to strange behaviour on some PCs".
- Reverted by him (README/log): topological bone sort (OpenXRay perf), DetailManager remove m_frame_rendered, details cache optimization #462 ("needs further testing"), ModelPool critical section, "Improve wait renderer", luabind from main branch, label scaling (#535, "bugs in GAMMA"), #822 (ours, broke against scripted attachments -> redone as #827). GAMMA breakage is the main revert trigger.
- Bundles: #678 (4 PRs merged into one) closed by its author; #691 oneTBB (3 PRs combined) closed, re-opened as #800 with 2 commits, still open (audio mod reports).

## Review behaviour
- He reviews code, not benchmarks; other contributors (erepb) ask for data, e.g. on #856 whether the time limit skews which objects update. Post-merge fixes by himself are common (#705).
- Merges in batches, often days after opening; whole weekends of merges.

## Our record (checked 2026-11-26 with gh + git)
- vacwave upstream PRs: 19. Merged 17: #734, #758, #763, #766, #773, #822 (reverted by #826, redone as #827), #827, #830, #845, #862, #868, #882 (HDR fonts), #887 (MT rain dupe), #888 (MT bump wait = LD1), #889 (XML load X1-X4), #890 (crash fixes C1 C2 C5 PS1 PS2). Open: #856 / #857 (A-Life scheduled_time_limit). Nothing of ours reverted since #822.
- README changelog (last entry 2026.11.22) does not list #882-#890 yet; #766 and #862 are listed under TheLostInPlace, not vacwave.
- 2026-11-26 the maintainer merged, reverted and re-applied #778 / #779 (TheLostInPlace profiler / Optick); merged into dev-mt-mcp at cc1fe272.
- Overlaps to check before PRs: #688 is upstream (our de78e126 builds on it); #879 (spawn prefetch races, CModelPool lock) MERGED 11-25; open #800 (oneTBB scheduler), #567 / #577 (grass instancing) touch render/threading areas of our R-fixes.

## Bucketing rules (inference)
- One PR per area, roughly 50-300 lines, one commit per fix so a single fix can be reverted; PR body: per-fix table (what, why, before/after, method, behaviour check).
- Proof per fix: profile evidence + before/after numbers on plain and GAMMA + an equivalence check where a result could differ (old/new side by side, 0 mismatches).
- Shared code -> `dev`; MT-only or risky container changes -> `dev-mt` first.
- Keep out of buckets: new threading, architecture changes, anything mod-visible (behaviour changes need an option, default off). LD1 (bump spin) and GC pacing stay separate decisions.
- Gamedata script fixes (axr_main) = their own PR (all mods depend on them).
