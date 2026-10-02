# STALKER Anomaly Modded Exes — vacwave MT fork

Personal fork of [themrdemonized/xray-monolith](https://github.com/themrdemonized/xray-monolith) (the Anomaly 1.5.3 "Modded Exes"), based on its **multithreaded (MT)** variant.

**For installation, features, options, Lua API docs and everything else, see the original repository:**
**https://github.com/themrdemonized/xray-monolith**

Everything below is specific to this fork.

## Downloads

Builds of this fork (MT only) are on the [Releases](https://github.com/vacwave/xray-monolith/releases) page. Install them the same way as the original Modded Exes.

## Added gameplay features

All of these affect the player only. NPCs are unchanged.

- **Carry weight rework** ([#1](https://github.com/vacwave/xray-monolith/pull/1)): from 33% of carry weight, movement and turn speed drop gradually. From 60%, walking stamina drain rises progressively. Movement gets weight-scaled inertia (smoothed start and stop) that ramps up steeply past full carry weight. Console: `g_actor_overweight_rework 0/1` (0 = vanilla).
- **Mid-air turning** ([#6](https://github.com/vacwave/xray-monolith/pull/6)): while airborne you can steer slightly toward your movement input, keeping your speed. Turning is reduced when heavily loaded. Follows `g_actor_overweight_rework`.
- **Smooth steps** ([#28](https://github.com/vacwave/xray-monolith/pull/28)): walking over small rocks, kerbs and thin slabs steps up smoothly instead of bumping or jerking the camera. Console: `g_smooth_steps 0/1` (1 = on).
- **No slope drift** ([#2](https://github.com/vacwave/xray-monolith/pull/2)): movement on slopes no longer drifts downhill away from where you are walking.

## Critical fixes

Besides many smaller bug fixes and performance improvements, this fork fixes:

- **Crashes**
  - Use-after-free in HUD sound aliases, and crashes on hits with no hitter ([#11](https://github.com/vacwave/xray-monolith/pull/11)).
  - Stalker crashes on a non-weapon best item and on destroyed cached items ([#21](https://github.com/vacwave/xray-monolith/pull/21)), plus dangling object pointers in actor and monster code ([#7](https://github.com/vacwave/xray-monolith/pull/7)).
  - Crashes from Lua memory calls on dead monsters and A-Life patrol dead ends ([#32](https://github.com/vacwave/xray-monolith/pull/32)), and null derefs in script inventory calls ([#22](https://github.com/vacwave/xray-monolith/pull/22)).
  - Engine out-of-bounds reads ([#13](https://github.com/vacwave/xray-monolith/pull/13)), and a heap race in memory usage stats ([#27](https://github.com/vacwave/xray-monolith/pull/27)).
  - Game crash on any log line over 4095 characters, e.g. a long Lua `log()` ([#29](https://github.com/vacwave/xray-monolith/pull/29)).
  - Azazel mode: crash after dying and on killing NPCs afterwards ([#52](https://github.com/vacwave/xray-monolith/pull/52)).
- **Memory and resources**
  - Video memory leaks every frame with 3D scopes, and on screenshots ([#29](https://github.com/vacwave/xray-monolith/pull/29)).
  - `xr_string` move constructor copying instead of moving ([#25](https://github.com/vacwave/xray-monolith/pull/25)).
- **Performance**
  - Large FPS drops near campfires, anomalies and lights when the SSS volumetric shaders aren't installed ([#51](https://github.com/vacwave/xray-monolith/pull/51)).
  - Per-frame allocations, lookups and redundant work removed across the renderer, scheduler, HUD, AI and spatial queries ([#9](https://github.com/vacwave/xray-monolith/pull/9), [#12](https://github.com/vacwave/xray-monolith/pull/12), [#14](https://github.com/vacwave/xray-monolith/pull/14), [#24](https://github.com/vacwave/xray-monolith/pull/24), [#31](https://github.com/vacwave/xray-monolith/pull/31)).

The full list is in the [pull requests](https://github.com/vacwave/xray-monolith/pulls?q=is%3Apr).

## Building

Same as the original: `src/engine-vs2022.sln` with Visual Studio 2022. Run `git submodule update --init` first. This fork's branch is `all-in-one-vs2022-wpo-mt-vac`.
