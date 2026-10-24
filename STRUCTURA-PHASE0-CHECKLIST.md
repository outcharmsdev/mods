# Structura Phase 0 — Implementation Checklist (derived from the actual source)

Produced **before** any edit, as required. Every line below was read off
`Structura-1.21.10` source; line numbers refer to that source.

## A. Existing diagnostics infrastructure (what to extend, not replace)

* `src/Perf.hpp:10-36` — `enum Counter : int` ending in `CounterCount`.
  23 real counters today. **New entries must be appended before `CounterCount`**
  so existing indices and the existing log line keep their meaning.
* `src/Perf.hpp:38-44` — `gOn` (`atomic_bool`), `gCnt[CounterCount]`
  (`atomic<uint64_t>`), `count()` = relaxed `fetch_add` gated on `gOn`.
  This gating is what makes instrumentation free when the log is off; new
  instrumentation must keep it.
* `src/Perf.cpp:30-74` — `takeLine()`. 1 s cadence (`:33`), diff against
  `gLast[]`, `snprintf` into a caller buffer. `frame_time_ms` at `:52` is
  `secs * 1000 / d[RenderHook]` — a **mean derived from the call count**, not a
  per-frame measurement. This is the gap the percentile work fills.
* `src/main.cpp:864` `uiLoop()` → `:889` `perf::takeLine(line, sizeof(line))`.
  `uiLoop` is a **detached `std::thread`** (`src/main.cpp:254`).
  ⇒ counters are written on the render thread and read on the UI thread.
  Anything new must be safe across that boundary without blocking either.
* Existing RAII timing precedent: `Hologram.cpp:1395-1406` (`CpuScope`),
  `clock_gettime(CLOCK_MONOTONIC)`, no allocation, no formatting in scope.
  New scopes must follow this shape.

## B. Hook-chain lifecycle (established, not assumed)

* `src/HookChain.hpp:2-13` states the model, attributed to preloader-android
  `5d108da src/pl/memory/Hook.cpp`:
  * GlossHook patches the target **before** `rebuildChain()` stores the slot, so
    a detour can run while its slot is still `nullptr` (install window).
  * `rebuildChain()` rewrites **every** slot of a target whenever any mod hooks
    or unhooks it — a slot holds "the next detour", not a fixed address.
  * ⇒ a detour must load its slot **on every call**; a cached copy would skip a
    later mod or point at a removed one.
* `chainPeek` (`:17-19`) = one acquire load, may return null.
  `chainNext` (`:22-27`) = unbounded `while (!(f = load(...))) {}`.
* Spins to address: `Hologram.cpp:788` (`getBlockDetour`),
  `Hologram.cpp:2361` (`renderLevelDetour`), and `chainNext` itself, reached via
  `getBlockViaChain` (`Hologram.cpp:796-798`).
* Slot population: `Hologram.cpp:2751` (RenderLevel) and `:2769` (getBlock),
  both `pl::memory::hook(..., HookPriority::Normal)`. Install is idempotent via
  `sRenderHooked` (`:2680`, set `:2758`) and `sGetBlockHooked` (`:2774`).

**Conclusion reached before editing:** both spins run *inside* a detour, so the
hook provably exists and the slot will be filled. There is **no valid fallback
pointer** — inventing one would mean calling an unverified address, and
returning early would skip the engine's own `RenderLevel` / `getBlock`, changing
game behaviour. So a timeout-and-return is **not** a safe fix here. What is safe
and is being done instead: stop the spin from burning a core (`sched_yield`
after a bounded number of attempts), and make a long wait *observable*
(counters + a rate-limited warning). The residual race is documented rather than
papered over.

## C. Raw engine offsets found (verified by reading each use)

| Offset | Meaning as used | Use sites |
|---|---|---|
| `t + 0x10` | Tessellator vertex-run pointer pair | `Hologram.cpp:967`, `:972` |
| `t + 0x70` | Tessellator colour-run pointer pair | `Hologram.cpp:967`, `:972` |
| `t + 0x290` | Tessellator vertex count | `Hologram.cpp:947`, `:1001`, `:1073` |
| `t + 0x1cc` | Tessellator offset (`float[3]`) | `Hologram.cpp:931`, `:2468` |
| `f.bt + 0x78` | BlockTessellator render flag | `Hologram.cpp:969` |

Contrast: the *named* offsets already live centrally in `lm::off`
(`src/Game.hpp:152-172`). The five above are bare literals at their use sites.
`Game.hpp` is host-includible (verified: it compiles standalone on the host), so
centralised values can be pinned by `static_assert` in a host test.

**Binary validation is PENDING** — no `libminecraftpe.so` exists in this
sandbox, so none of these five can be checked against the target build. They are
transcribed unchanged; centralising them must not alter any value.

## D. Counter-correctness defect (confirmed in code)

* `src/State.hpp:202` declares `texVerts`.
* `Hologram.cpp:2162` stores **`nMeshes`** into it — a mesh count.
* Its three neighbours are correct: `texChunks`←`ready` (`:2159`),
  `texBlocks`←`pending` (`:2160`), `texZero`←`tf->zero` (`:2161`).
* Consumers: **none**. A full-tree grep for `texVerts` returns only the
  declaration and that one store. So correcting the stored value cannot break a
  reader; the mesh count is already reported separately as `gOut.meshesDrawn`
  (`:2158`).
* Correct value available at that point: `tf->verts`, accumulated at
  `Hologram.cpp:1071` and reset per frame by `texFrameSetup`. `tf` is non-null
  there: the `!texOn` path returns early at `:1848`, and `texOn` requires `tf`
  (`:1492`).

## E. Invalidation reasons to count separately

Each is a distinct `dirty = true` site, so they can be attributed exactly:

| Reason | Site | Scope |
|---|---|---|
| Compare found a kind change | `Hologram.cpp:1633` (`markDirty`, called `:1687`) | up to 8 chunks |
| Camera left the cull band | `Hologram.cpp:2049` | 1 chunk |
| Alpha / tint changed | `Hologram.cpp:1533` | all chunks |
| Holo style changed | `Hologram.cpp:2502` | all chunks |
| Filter box moved | `Hologram.cpp:1551` | chunks overlapping the slab |

Not an invalidation but rebuild-forcing: `freeAllChunks()` on signature change
(`:1531`), and initial `dirty = true` at construction (`:1329`).

## F. Test baseline actually executed on this host

Built with `g++ -std=c++20 -O1 -g -fsanitize=address,undefined -pthread -Isrc`:

| Test | Result |
|---|---|
| `tests/compat/hookchain_test.cpp` | 20 passed, 0 failed |
| `tests/compat/resolver_rules_test.cpp` | 26 passed, 0 failed (fixture SKIPPED: no reference library) |
| `tests/compat/document_cb_test.cpp` | 7 passed, 0 failed |
| `tests/cyan_test.cpp` | 326 passed, 0 failed |
| `tests/beproxy_test.cpp` | 3307 ok, 0 failed |
| `tests/error_vision_test.cpp` | 568 passed, 0 failed |
| `tests/ep_test.cpp` | 28 passed, 0 failed |
| `tests/host_test.cpp`, `tests/paste_test.cpp`, `tests/paste_roundtrip_test.cpp` | **not built** — need `zlib.h`, absent here |

`hookchain_test.cpp` is the test that constrains the hook-chain change:
`testInstallWindow` (`:61-73`) asserts a call arriving during the install window
**waits** and then reaches the game, and `testConcurrentRewrite` (`:124-146`)
asserts every call reaches the game exactly once under 2000 hook/unhook cycles.
Any Phase 0 change must keep both green.

There is **no** existing test for perf counters — one is added in Phase 0.

## G. Constraints carried into the edit

* No allocation and no string formatting inside the render path.
* No blocking on the render thread; no lock taken there.
* Keep `count()`'s `gOn` gate so instrumentation is free when the log is off.
* Preserve every existing counter's index and meaning.
* Do not change hook priorities, install order, chunk sizes, budgets, LOD,
  invalidation rules, geometry or appearance.
* Do not make `buildChunk` resumable, add LOD, add greedy meshing, introduce a
  renderer interface, or change backends — those are Phase 1+.
