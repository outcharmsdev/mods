# Structura Phase 0 — Rendering Diagnostics and Hook Safety

**Status: Phase 0 complete. Stopped here as instructed — Phase 1 not started.**

Reviewable diff: `STRUCTURA-PHASE0.patch` (applies with `patch -p1` from the Structura root).
Pre-edit checklist derived from the source: `STRUCTURA-PHASE0-CHECKLIST.md`.

---

## 1. Changed files

| File | +/− | What |
|---|---|---|
| `src/Perf.hpp` | +67 −2 | 20 new counters appended before `CounterCount`; `countMax()`; frame-time ring + `noteFrameUs()`; `takePhaseLine()` declaration |
| `src/Perf.cpp` | +97 −2 | Ring storage, nearest-rank percentiles, `takePhaseLine()`; `takeLine()` output untouched |
| `src/HookChain.hpp` | +67 −1 | `chainNext()` yields after `kSpinBeforeYield` and records the wait in `SpinStats`; documented why the wait cannot be bounded |
| `src/Game.hpp` | +12 −0 | The five raw Tessellator/BlockTessellator literals centralised, values unchanged |
| `src/Hologram.cpp` | +166 −30 | Phase timers, per-reason invalidation counters, vertex-validation counters, `texVerts` fix, offset names, spin drain + rate-limited warning |
| `src/main.cpp` | +4 −0 | Emit the phase line after the existing perf line |
| `tests/perf_test.cpp` | +244 new | Counter semantics, gating, ring bounds, percentiles, log-field regression |
| `tests/compat/hookchain_phase0_test.cpp` | +148 new | Wait behaviour, call-through invariant, no caching |
| `tests/compat/offsets_test.cpp` | +64 new | `static_assert` pinning every centralised and pre-existing offset |

## 2. Every change explained

**Diagnostics (`Perf.hpp/.cpp`).** 20 counters appended *after* the last existing
one, so no existing index moves — asserted in `perf_test.cpp` (`RenderHook==0`,
`HoloCpuUs==10`, `SvHits==22`, `HoloHookUs==23`, `ChainSpinMaxUs==CounterCount-1`).
All new instrumentation goes through the existing `count()` gate, so with the log
off the cost is one relaxed atomic load and nothing else.

Timing uses `monoUs()` (`CLOCK_MONOTONIC`) via an RAII `UsScope` that mirrors the
existing `CpuScope` in `drawStructure`: no allocation, no formatting, inert when
perf is off. Where a span is a triple-nested loop (`buildChunk`'s window fill and
tessellation), timing is an explicit `t0`/`count()` pair rather than a
brace-wrapped scope, to avoid any chance of mis-balancing the loop body.

`takeLine()`'s `snprintf` and field list are **byte-for-byte unchanged**. The new
metrics go to a second `[perf:phase]` line formatted from the same window
`takeLine()` just consumed, so the two lines can never describe different frame
ranges.

**Percentiles.** Fixed 512-slot ring, `noteFrameUs()` writes one sample per
rendered frame. Sampling interval, capacity, percentile method and reset
behaviour are documented at the declaration in `Perf.hpp`. Nearest-rank
(`ceil(P/100·N)`, clamped to `[1,N]`) on a stack-sorted snapshot — no allocation.
Zero and >60 s samples are rejected at the door, so a percentile is never
computed over a non-frame. Max-type counters (`HoloBuildMaxUs`,
`ChainSpinMaxUs`) use a CAS loop and are reported with a `_max` suffix so a
maximum is never mistaken for an average.

**Hook chain (`HookChain.hpp`).** See §7 — this is the substantive finding.

**Offsets (`Game.hpp`).** Five literals moved to named constants, values
transcribed unchanged and pinned by `static_assert`.

**`texVerts` fix (`Hologram.cpp:2244`).** Was storing `nMeshes` — a mesh count —
into a field named `texVerts`. Now stores `tf->verts`, the real per-frame
textured vertex total. Verified before changing: a full-tree grep finds **no
reader** of `texVerts`, so no consumer depended on the old value, and the mesh
count is still reported as `gOut.meshesDrawn`. Field not renamed, not repurposed.

## 3. Original behaviour preserved

Nothing in the render path changed meaning:

- Hologram geometry, appearance, textured-vs-coloured selection, chunk sizes,
  tessellation, LOD/distance policy, build budgets, invalidation rules,
  EasyPlace, structure parsing, public API — **untouched**. Every edit is an
  added counter, an added timer, or a literal replaced by a constant of identical
  value.
- Hook priorities unchanged (`HookPriority::Normal` at both sites), install order
  unchanged, `install()` idempotence (`sRenderHooked`) unchanged.
- `chainNext()` still returns a valid pointer on every call and still re-reads the
  slot per call. `chainPeek()` untouched.
- The existing `[perf]` log line is unchanged, so any tooling parsing it still works.
- `setEnabled()` semantics unchanged: it still returns early when the value is
  unchanged. That is load-bearing — `uiLoop` re-asserts the same value every pass
  and must not clear the window. Covered by `testEnableIsIdempotentAndOnlyResetsOnTransition`.

The only runtime differences are the added counters and one `sched_yield()` on a
path that was previously a pure busy spin.

## 4. Tests executed and actual results

`g++ -std=c++20 -O1 -g -fsanitize=address,undefined -pthread -Isrc`:

| Test | Result |
|---|---|
| `compat/hookchain_test.cpp` (existing) | **20 passed, 0 failed** |
| `compat/hookchain_phase0_test.cpp` (new) | **18 passed, 0 failed** |
| `compat/resolver_rules_test.cpp` (existing) | **26 passed, 0 failed** (fixture SKIPPED: no reference library) |
| `compat/document_cb_test.cpp` (existing) | **7 passed, 0 failed** |
| `compat/offsets_test.cpp` (new) | **11 passed, 0 failed** |
| `perf_test.cpp` (new) | **64 passed, 0 failed** |
| `cyan_test.cpp` (existing) | **326 passed, 0 failed** |
| `beproxy_test.cpp` (existing) | **3307 ok, 0 failed** |
| `error_vision_test.cpp` (existing) | **568 passed, 0 failed** |
| `ep_test.cpp` (existing) | **28 passed, 0 failed** |

**10/10 green.** All 7 pre-existing host tests still pass unmodified — in
particular `hookchain_test`'s `testInstallWindow` and `testConcurrentRewrite`,
which constrain the hook-chain change.

**Not built, and why:** `host_test.cpp`, `paste_test.cpp`, `paste_roundtrip_test.cpp`
need `zlib.h`, absent here (`apt-get` cannot reach the Debian mirrors from this
sandbox, and there is no root). All three cover `.mcstructure`/`.litematic`/NBT
parsing and touch nothing Phase 0 modified.

One real bug was found by the tests rather than by reading: the first
`perf_test` run failed 4 checks because a preceding test left 1061 samples in the
ring and `setEnabled(true)` does not reset it when already enabled. The
production behaviour is correct; the test was wrong. Fixed in the test, and the
reset semantics are now asserted explicitly.

## 5. Build results and environment limits

**The Android library was NOT compiled.** No Android NDK, `cmake`, `ninja`,
`clang` or `ndk-build` exists in this sandbox, and Structura's own
`CMakeLists.txt:9` hard-fails without the Android toolchain. `vendor/preloader-android`
is also absent, so `pl/*` headers are unavailable.

What was verified instead, and its limits:

- **Host tests** (above) exercise `Perf.hpp/.cpp`, `HookChain.hpp` and `Game.hpp`
  for real, compiled and run under ASan+UBSan.
- **`Hologram.cpp` and `main.cpp` were syntax-checked only** (`g++ -fsyntax-only`)
  against minimal hand-written stubs for `pl/Logger.hpp`, `pl/memory/Hook.hpp`,
  `pl/memory/Signature.hpp` and `EGL/egl.h`. That catches type and name errors —
  it caught two real ones during the work — but it is **not** a compile of the
  translation unit against the real SDK, and it proves nothing about linking or
  codegen. Treat `Hologram.cpp` as unverified beyond syntax.
- No `libminecraftpe.so`, so **no offset, signature or ABI claim was checked
  against the binary**.

## 6. Sample diagnostics line

Generated by running the actual `takeLine()`/`takePhaseLine()` code against a
simulated ~55 fps session (not written by hand):

```
[perf] frame_id=55 fps=54.9 frame_time_ms=18.22 render_hook_calls=1.00 player_update_calls=0.00 gui_draw_calls=0.000 gui_commands=0.00 allocations_this_frame=0.382 layout_rebuilds=0.000 texture_rebuilds=0.000 mesh_rebuilds=0.382 world_queries=560.000 gpu_flushes=0 swap_calls=0 blocking_waits=0 holo_cpu_ms=2.580 holo_compares=560 holo_chunk_builds=0.127 holo_imm_quads=12 | safe_reads_per_s=64770

[perf:phase] hook_us=3453.5 cmp_us=620.0 window_us=52.2 tess_us=222.7 mesh_us=16.5 submit_us=260.0 imm_us=95.0 build_max_us=2290 spin_max_us=38 | rebuilds_per_s=7.0 inv_cmp_per_s=2.0 inv_cull_per_s=3.0 inv_alpha_per_s=0.0 inv_style_per_s=0.0 inv_filter_per_s=0.0 | imm_quads_per_s=659 imm_lines_per_s=18663 mesh_submits_per_s=2250.5 verts_per_s=93972 valid_verts_per_s=2878 bad_verts_per_s=0 getblock_calls_per_s=100449 chain_waits_per_s=1.0 | frame_ms p50=18.58 p95=19.10 p99=96.40 max=96.40 samples=55
```

The values are synthetic; the formatting is the real code's output. Note what the
second line makes visible that the first did not: `p50=18.58` looks healthy while
`p99=96.40` shows a 5× spike — exactly the class of problem a mean hides.

**Measured vs estimated.** Timings are measured. `HoloTessUs` is measured but
*inclusive*: it covers `texBlock` per block, which is where tessellation and
vertex validation are interleaved. They are deliberately not timed apart —
separating them needs a clock read per block, and at up to 16³ blocks per chunk
the instrumentation would cost more than the work measured. The exact validation
*outcome* is counted instead (`valid_verts_per_s` / `bad_verts_per_s`), so a
validation problem is still visible. `HoloVerts` counts the immediate path from
our own buffers; cached-mesh vertices are **not** included, because they live
inside the engine's mesh and are not readable without an unvalidated offset. That
is stated in the code rather than padded with a guess.

## 7. Remaining hook-chain safety concerns

**Established, not assumed.** `HookChain.hpp:2-13` documents the model, attributed
to preloader-android `5d108da`: GlossHook patches the target *before*
`rebuildChain()` stores the slot, so a detour can run while its slot is null; and
`rebuildChain()` rewrites every slot whenever any mod hooks or unhooks that
target. Hence: read the slot every call, never cache.

**Why the wait was not replaced with a timeout.** Both spins run *inside* a
detour, so the hook provably exists and the slot will be filled. There is no
fallback: the slot holds either the next mod's detour or the engine trampoline,
and neither is recoverable from anywhere else. The alternatives to waiting are
(a) call a null/stale pointer — crash or silent corruption, or (b) return without
calling through — which skips the engine's own `RenderLevel`/`getBlock` **and
every other mod in the chain**. Both are worse than waiting, so neither was done.
A timeout with an early return would have been the wrong fix, and the brief
warned against exactly that.

**What Phase 0 did change:**
1. `sched_yield()` after `kSpinBeforeYield` (64) attempts. The old loop was a pure
   busy spin, which on a big.LITTLE arm64 device can starve the very thread that
   is about to store the slot. Semantics unchanged — it still waits, still returns
   a valid pointer.
2. Every wait that actually spins is counted, and the longest is recorded
   (`chain_waits_per_s`, `spin_max_us`).
3. A rate-limited warning (≤1/s) fires when a wait exceeds 5 ms — far beyond the
   documented microsecond window, so it cannot trigger during a normal install.

**Unresolved, reported rather than papered over:** if `rebuildChain()` ever fails
to store the slot at all, this still waits forever. Detecting that needs a
timeout, and a timeout has no safe action on expiry. The correct fix belongs in
the hook framework — store the slot *before* patching the target — not here.

Also unchanged by design: hook priorities, install order, and the per-call slot
read (requirement 7 — the chain design requires it, and
`testSlotRereadEveryCall` now pins it).

## 8. Still requires on-device validation

1. **The five centralised offsets** (`Tess_mVertexRun` 0x10, `Tess_mColorRun` 0x70,
   `Tess_mVertexCount` 0x290, `Tess_mOffset` 0x1cc, `BlockTess_mRenderFlag` 0x78)
   against 1.26.52.03 RC2. Centralisation preserved the values; it did not
   validate them. `offsets_test` pins them against drift, not against the game.
2. **A real Android build.** Nothing here was compiled with the NDK. The
   `Hologram.cpp`/`main.cpp` edits are syntax-checked against stubs only.
3. **Actual perf numbers.** Every figure in §6 is synthetic. The counters exist to
   be read on a device; no baseline or improvement is claimed.
4. **`spin_max_us` under real mod coexistence.** Whether the install window ever
   approaches the 5 ms warning threshold, and whether another mod's
   hook/unhook churn produces measurable waits, can only be observed on a device
   with the target mod set installed.
5. **Instrumentation overhead.** The timers are cheap by construction and free
   when the log is off, but the actual per-frame cost was not measured — there is
   nothing here to measure it on.
6. **The three zlib-blocked tests**, once a toolchain with zlib is available.

---

### Phase 1 not started

`buildChunk` is still not resumable; no renderer interface, no LOD, no greedy
meshing, no backend change. Awaiting review and explicit approval.
