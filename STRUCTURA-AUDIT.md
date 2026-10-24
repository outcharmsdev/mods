# Structura — Rendering Architecture Audit and Redesign Plan

**Phase:** source audit only. No source was modified, no hook or offset was changed,
nothing was built or packaged.

**Evidence base:** `Structura-1.21.10-source.txt` (125 files, 48 160 lines of
non-third-party code), read from a scratch split of the supplied archive. Every
line reference below is to a file in that archive.

**Hard limits on this report, stated up front:**

* Nothing was compiled. There is no Android NDK, CMake or Ninja in this sandbox.
* Nothing ran on a device. No frame time, draw-call count or freeze was measured.
* No `libminecraftpe.so` was available, so **no signature, offset, calling
  convention or ABI claim in this report was checked against the binary.** They
  are read off the source's own tables and comments.
* Therefore: every *performance* statement below is a code-grounded hypothesis
  with a stated confidence, never a measurement. Section 9 says how to turn each
  one into a measurement.

Where the source's own documentation is relevant it is cited
(`docs/HOLOGRAM-1.21.6.md`, `docs/COMPAT-AUDIT-1.21.5.md`), and where the source
contradicts its own documentation that is called out.

---

## 1. Map of the current rendering pipeline

### 1.1 Hooks actually installed

`lm::holo::install()` (`src/Hologram.cpp:2679`) installs exactly **two** hooks,
both through `pl::memory::hook(..., HookPriority::Normal)`:

| Target | Detour | Site |
|---|---|---|
| `RenderLevel` (by signature) | `renderLevelDetour` | `Hologram.cpp:2751` |
| `BlockSource::getBlock` (by signature) | `getBlockDetour` | `Hologram.cpp:2769` |

All the other named symbols — `TessellatorBegin`, `TessellatorColor`,
`TessellatorVertex`, `RenderMeshImmediately`, `TessellateInWorld`,
`GetBlockTessellator`, `BTResetCache`, `BTSetBlockInCache`, `BlockRenderFlag`,
`TessellatorEndMesh`, `MeshRender`, `MeshDestroy`, `MatrixStackPush`,
`MatrixRefPop`, `TessellatorClear` — are **resolved and called directly, not
hooked** (`Hologram.cpp:2694-2716`). That distinction matters: Structura is a
*caller* of the engine's tessellation, not an interceptor of it.

All 10 function names listed in the brief exist in the source. `submitMesh`,
`flushQuads`, `flushLines`, `drawStructure` and `renderLevelDetour` are
Structura's own; the `Tessellator*` / `RenderMesh*` / `TessellateInWorld` names
are signatures for engine functions (`src/Game.hpp`, `namespace lm::sig`).

### 1.2 Per-frame call graph (render thread)

```
renderLevelDetour(self, screenCtx, fro)                    Hologram.cpp:2359
├─ spin-wait on gRenderLevelOrig, then call original       :2361-2364
├─ tess = *(screenCtx + 0xB8)      [off::ScreenContext_mTessellator]
│  lrp  = *(self      + 0x420)     [off::LevelRenderer_mLevelRendererPlayer]
│  cam  = *(lrp       + 0x628)     [off::LevelRendererPlayer_mCamPos]     :2380-2385
├─ validateWorld() -> BlockSource region                   :2394
├─ "work" gate: return early if nothing to draw            :2405-2421
├─ ensureAirAndNames(region); captureStep(region)          :2432-2433
├─ ++gFrame; reapMeshes(false)                             :2461-2462
├─ TessOffsetGuard  saves/restores tess+0x1cc              :2468-2470
├─ colorHolder save/restore                                :2477
├─ texFrameSetup() -> materials from LevelRendererPlayer   :2526-2537
├─ drawStructure(cam, region, &tf)                         :2541
├─ flushLines(t, screenCtx, lineMat, cam)                  :2634
├─ flushQuads(t, screenCtx, faceMat, cam, twoSided)        :2636
└─ Error Vision: flushQuads(gEvQuads) + flushLines(gEvLines) :2642-2643
```

`drawStructure` (`Hologram.cpp:1391`) internally:

```
drawStructure
├─ try_lock model mutex; on failure reuse a static snapshot :1425-1446
├─ signature hash over structure+placement+settings+epochs;
│  on change -> freeAllChunks()                             :1514-1531
├─ COMPARISON (world vs schematic)                          :1646-1709
│   ├─ near sweep: 13^3 box around the player, 560 cells/frame
│   └─ far  sweep: whole visible box, 3000 cells/frame
│       each cell -> blockAt() -> getBlockViaChain() -> compareOne()
│       on kind change -> markDirty() (up to 8 chunks)      :1687, 1625
├─ build the chunk list, sort by squared camera distance    :2033-2055
├─ BUILD, under budget                                      :2062-2081
│   budget: kMaxBuildsPerFrame=2, kMaxRebuildsPerFrame=1, 2.5 ms + debtMs
│   └─ buildChunk(qx,qy,qz)                                 :1857-2029
│       ├─ window fill: 20^3 = 8000 positions               :1885-1924
│       │    worldToLocal + palette lookup + sBTSetCache
│       ├─ block loop over the 16^3 chunk                   :1930-2005
│       │    kindOf -> 6-neighbour exposure test
│       │    -> texBlock()                                  :963
│       │         sBTReset flag; sTessInWorld(...)  <== the game tessellates
│       │         read back vertex/colour runs; validate finiteness + area
│       │         optional squeeze into a beproxy box
│       │         texCut() at threshold -> new MeshBlob(0x400)  :944-957
│       └─ freeChunk(old); cm.meshes.swap(fresh)            :2018-2027
├─ DRAW, far -> near                                        :2096-2126
│   per MeshBlob -> drawMesh -> sMeshRender  (cap kMaxMeshes=6000)
│   per "special" block -> emitColored -> gQuads/gLines     (per frame!)
└─ cyan grid edges -> gLines (cap 4096 lines/frame)         :2128-2146
```

### 1.3 The `getBlock` window

`getBlockDetour` (`Hologram.cpp:774`) is a second, independent mechanism. While
`buildChunk` runs, `WinScope` (`:802-810`, engaged at `:1928`) turns on a
20³ = 8000-entry table `gWinBlk` (`:766`). Inside that scope, and only for the
matching `BlockSource` and the matching thread (`pthread_equal`, `:776`), the
detour answers `getBlock` from the table instead of the world.

Purpose, verified from the surrounding code: the game's `BlockTessellator`
culls a face when the neighbour is opaque. Feeding it the *hologram's* blocks as
neighbours is what removes interior faces from the hologram mesh. Structura is
borrowing the engine's own culling rather than reimplementing it.

### 1.4 Two rendering paths, not one

This is the single most important structural fact for the redesign:

| Path | Geometry | Cached? | Cost profile |
|---|---|---|---|
| **Textured** (`texOn`) | `MeshBlob` per chunk section, cut at ~2048 verts | **yes**, per chunk, invalidated by `dirty` | cost concentrated in `buildChunk` |
| **Immediate** (`emitColored`) | `gQuads` / `gLines`, caps 40 000 / 60 000 | **no** — rebuilt every frame | cost is per-frame and unbounded by caching |

Blocks reach the immediate path when `texBlock` returns ≤ 0 vertices or invalid
geometry (`:1977-2005`), i.e. block entities without a stand-in, doors with no
tessellator geometry, and unresolved variants. They also reach it whenever
`texOn` is false at all (`:1846-1852`, the `colorChunk` fallback).

---

## 2. Source-grounded explanation of the current bottlenecks

Confidence key: **C** = confirmed present in the code; **S** = strongly
suspected as a performance cause; **U** = unverified.

### P1 — Immediate-mode geometry is regenerated and re-uploaded every frame

* **Where:** `emitColored` (`Hologram.cpp:1790-1830`), flushed by `flushQuads`
  (`:218-235`) and `flushLines` (`:274-291`), called at `:2634`/`:2636`.
* **Path:** every non-cached block emits up to 6 face quads plus 4-6 outline
  lines into `gQuads`/`gLines`; the flush then pushes **one `sVertex` call per
  vertex** (`:226-231`, `:285-286`). With `twoSided` on, 8 vertices per quad
  instead of 4 (`:223`).
* **Impact:** at the caps this is 40 000 quads = 160 000 `sVertex` calls, plus up
  to 60 000 lines = 120 000 more, **per frame**, with no caching at all.
  `docs/HOLOGRAM-1.21.6.md` §2 says chests and signs were moved *out* of this
  path into the chunk mesh — so the population here is whatever still fails to
  tessellate. On a structure dominated by block entities or unresolvable
  variants, this path is unbounded by structure size.
* **Confidence:** **C** that the path exists and is per-frame; **S** that it is
  the dominant cost for "block textures" structures; **U** on magnitude.
* **Falsify:** log `gQuads.size()`, `gLines.size()` and `holo_imm_quads` (already
  a counter, `Perf.hpp:HoloImmQuads`) while rendering a texture-heavy structure.
  If `holo_imm_quads` is small, P1 is not the cause.

### P2 — One chunk build can overrun the frame budget without bound

* **Where:** the build budget at `Hologram.cpp:2062-2081`.
* **Path:** the 2.5 ms guard is `if (any && nowMs() - t0 > 2.5) break;` (`:2070`)
  — evaluated **after** `buildChunk` returns. Nothing bounds the cost of a single
  `buildChunk`. One call does a 20³ = 8000-position window fill (`:1885-1924`)
  plus up to 16³ = 4096 `texBlock` calls, each invoking the engine's
  `tessellateInWorld`, reading the vertex run back, and validating every vertex
  for finiteness and quad area (`:981-1000`).
* **Impact:** `kMaxBuildsPerFrame = 2` (`:1336`) permits two such chunks in one
  frame. A dense 16³ chunk is plausibly tens of milliseconds. `debtMs`
  (`:2080`) then suppresses the *next* frame's building, which converts a
  one-frame spike into a two-frame hitch — visible as stutter, and if a chunk is
  pathological, as a freeze.
* **Confidence:** **C** that the guard is post-hoc and unbounded per chunk; **S**
  that this produces the observed stutter.
* **Falsify:** instrument `buildChunk` entry/exit with a monotonic clock and log
  the maximum single-call duration. Also log `tf->verts` per build.

### P3 — Cull-rebuild churn while the camera moves

* **Where:** `kCullBand = 10.f`, `kCullRebuild = 8.f` (`Hologram.cpp:812-813`),
  applied at `:2043-2051`.
* **Path:** chunk meshes are culled against the **camera position at build
  time** (`tf->ccx/ccy/ccz`, `:1872-1874`; the traversal order at `:1929-1933`
  also depends on which side of the chunk the camera is on). When the camera has
  moved more than 8 blocks on any axis since the build and the chunk lies within
  the band, `cm.dirty = true`.
* **Impact:** walking toward or around the hologram continuously crosses that
  threshold, so chunks are re-dirtied as fast as they are rebuilt. Rebuilds are
  capped at 1/frame, so the visible symptom is a permanently rebuilding hologram
  while moving, each rebuild paying the unbounded P2 cost.
* **Confidence:** **C** on the mechanism; **S** that it is the dominant
  proximity effect.
* **Falsify:** log `HoloBuilds` (`Perf.hpp`) and the count of chunks dirtied by
  the cull test specifically (as opposed to `markDirty`) while walking a fixed
  path. If cull-dirtying dominates, P3 is confirmed.

### P4 — `markDirty` fans one comparison change out to eight chunks

* **Where:** `markDirty` (`Hologram.cpp:1625-1637`), called from `:1687`.
* **Path:** a kind change at one position marks up to 8 chunks dirty (the 27-cell
  neighbourhood minus those sharing the same chunk on that axis). The near sweep
  re-checks 560 cells per frame inside a 13³ box around the player
  (`kCmpNearR = 6`, `kCmpNear = 560`, `:1171-1172`).
* **Impact:** hand-placing or breaking blocks near the hologram produces a burst
  of dirty chunks, each then rebuilt at 1/frame under P2's cost. This is a
  *second*, independent proximity mechanism alongside P3 — and it is triggered
  by player action near the structure, which matches "freezes when the player
  approaches" if the player is also building.
* **Confidence:** **C** on the fan-out; **U** on how often kind actually changes.
* **Falsify:** count `markDirty` invocations per second while standing still
  near a fully-built structure versus while placing blocks.

### P5 — Unbounded spin on the hook-chain slot

* **Where:** `Hologram.cpp:2361` (`gRenderLevelOrig`) and `:788`
  (`gGetBlockOrig`): `while (!(orig = __atomic_load_n(&slot, __ATOMIC_ACQUIRE))) {}`.
  `chainNext` in `HookChain.hpp:24-29` is the same spin.
* **Path:** `HookChain.hpp` documents the intent precisely — `GlossHook` patches
  the target *before* `rebuildChain()` stores the slot, so a detour can run while
  its slot is momentarily null. The spin is meant to wait that window out.
* **Impact:** there is **no timeout and no bail-out**. If another mod unhooks the
  target, or the chain is rebuilt in a way that leaves the slot null for longer
  than the window, the render thread spins forever. That is a hard freeze with no
  log line, no ANR trace from Structura, and no recovery — matching the reported
  symptom exactly. These are the only unbounded loops in `Hologram.cpp`
  (verified by grepping `while (!(`).
* **Confidence:** **C** that the spin is unbounded; **U** that it is the observed
  freeze. This is a *coexistence* bug, so it should correlate with which other
  mods are loaded — a strong, cheap discriminator.
* **Falsify:** add a bounded retry (e.g. 10 000 iterations) that logs and returns
  without calling through. If freezes stop, or start producing a log line, P5 is
  the cause. **Note this must not become a dropped call** — see §8.

### P6 — A hook on the engine's hottest function

* **Where:** `Hologram.cpp:2769`, `BlockSource::getBlock`.
* **Path:** even with the window off, every `getBlock` in the process pays one
  relaxed atomic load (`gWinOn`, `:775`), and on the fall-through path one
  acquire atomic load of the slot plus an indirect call (`:788-790`).
* **Impact:** small per call, but `getBlock` is called at very high frequency by
  the engine itself. Proximity to a large hologram increases the engine's own
  `getBlock` rate (chunk meshing, entity collision, block ticking), so this cost
  scales with exactly the condition that triggers the freeze.
* **Confidence:** **C** that the hook exists; **U** on measurable cost.
* **Falsify:** compare frame time with the hologram loaded but the window never
  engaged, against a build with the `getBlock` hook not installed at all.

### P7 — Hard-coded offsets with a single tested build

* **Where:** `namespace lm::off` in `src/Game.hpp` — a flat set of constants
  (`LevelRenderer_mLevelRendererPlayer = 0x420`,
  `LevelRendererPlayer_mCamPos = 0x628`, `ScreenContext_mTessellator = 0xB8`,
  `ScreenContext_mColorHolder = 0x30`, `Actor_mStateVector = 0x208`, …) with
  **no version dimension**. `Resolver.cpp:78-81` has exactly **one** entry in
  `kTested[]`: build-id `56de9eed077631e03a31f4f58eb2f0e00071d335`, label
  "1.26.52 arm64".
* **Impact:** the tested build matches the stated target (1.26.52.03 RC2;
  `docs/COMPAT-AUDIT-1.21.5.md` records the same `56de9eed…` prefix), so this is
  **correct for the target**. But on any other build, signatures may still
  resolve while the offsets are wrong, producing silent misreads of camera
  position or the tessellator pointer rather than an error. `Resolver.cpp` gates
  *function* resolution on the tested build; nothing gates the *offsets*.
* **Confidence:** **C** on the structure. This is the failure mode the installed
  skill's `references/version-porting.md` and `scripts/offset_check.py` exist to
  catch, and Structura does not have an equivalent check for `lm::off`.
* **Falsify:** run the skill's `offset_check.py` against the target
  `libminecraftpe.so` for each offset in `lm::off`, or read the displacements
  back out of `RenderLevel`'s own disassembly.

### P8 — Transparent overdraw, optionally doubled

* **Where:** `twoSided` at `:2636`, doubling vertices per quad at `:223`;
  material `particles_blend` (`kLooks[0]`, `:594`); far→near draw order at
  `:2100`.
* **Impact:** the hologram is inherently alpha-blended, so no early-Z rejection
  is available and every covered fragment is shaded. `twoSided` doubles the
  vertex count for the immediate path. Far→near is *correct* for blending but is
  the worst order for overdraw.
* **Confidence:** **C** on the mechanism; **U** on magnitude — this is a GPU cost
  and cannot be assessed from source at all.
* **Falsify:** GPU profiler on device; or A/B `twoSided` off and measure.

### P9 — Per-vertex submission through the engine tessellator

* **Where:** `flushQuads` `:226-231`, `flushLines` `:285-286`.
* **Impact:** the immediate path has no bulk vertex upload; every vertex is a
  call into `Tessellator::vertex`. This multiplies P1.
* **Confidence:** **C**.

### Things that are already correct

Worth stating, because the redesign should not undo them:

* `install()` is idempotent (`sRenderHooked`, `:2674/2680/2758`).
* Startup uses **poll-then-settle**: `kSettleDelayMs = 5000` (`main.cpp:56`), a
  single watcher thread guarded by `mWatcherStarted.exchange` (`:252`), and
  `disable()` deliberately does *not* clear that flag (`:269-270`). This matches
  the skill's `mod-lifecycle-and-crash-safety.md` prescription, and the C4 fix in
  `docs/COMPAT-AUDIT-1.21.5.md` shows the earlier bug was already found and
  fixed.
* `install()` **hard-fails** (`InstallResult::SignatureMissing`) if any of
  `RenderLevel`, `TessellatorBegin/Color/Vertex` or `RenderMeshImmediately` is
  missing (`:2735-2747`). No silent half-rendering.
* The render thread never blocks on the model mutex — `try_to_lock` with a static
  snapshot fallback (`:1425-1446`).
* Chunk meshes are cached; scratch vectors are reused so steady-state rebuilds
  allocate nothing (`:1860-1865`).
* Mesh destruction is deferred by ≥ 4 frames through a graveyard (`:1350-1361`) —
  correct, because the GPU may still be reading them.
* `HookChain.hpp` re-reads the slot per call rather than caching it — the C2 fix.
* `HookPatch.hpp` recognises six other hookers' patch shapes so another mod's
  hook at an entry point can be detected instead of misread as a bad signature.
* GL state is saved and restored (`XrayLineScope` `:246-263`, `TessOffsetGuard`
  `:2468`, colourHolder `:2477`), and the D3 fix in the compat audit shows the
  `glIsEnabled`/`eglGetCurrentContext` guards were added deliberately.

---

## 3. Separate analysis of the proximity freeze

The brief warns not to assume one root cause. There are **four** independent
proximity-linked mechanisms in this source, and they are not mutually exclusive:

| # | Trigger | Mechanism | Bounded? |
|---|---|---|---|
| P3 | camera moves > 8 blocks on an axis | cull test dirties chunks (`:2043-2051`) | 1 rebuild/frame, but each rebuild is unbounded (P2) |
| P4 | kind changes near the player | `markDirty` fans out to 8 chunks (`:1625`) | 1 rebuild/frame, same unbounded cost |
| P5 | another mod touches the hook chain | unbounded spin on a null slot (`:2361`, `:788`) | **no bound at all** |
| P6 | engine's own `getBlock` rate rises near a large structure | per-call overhead on the hottest function | scales with engine activity |

**Which is most likely to produce a *freeze* rather than a *stutter*:** P5. A
frame-budget overrun (P2/P3/P4) produces dropped frames and stutter; it does not
produce a frozen screen, because the budget machinery keeps the loop moving. Only
P5 stops the render thread outright, and it does so silently.

**But P5 is a coexistence bug, not a proximity bug.** If the freeze reproduces
with *no other mods loaded*, P5 is excluded and P2+P3 become the leading
candidates — a pathological chunk (many block entities, many zero-vertex
fallbacks, or a large `special` population) combined with continuous cull
dirtying.

**Discriminating experiment, in order of cost:**

1. Reproduce with all other LeviLauncher mods disabled. Freeze persists → P5 out.
2. Log `HoloBuilds`, `HoloImmQuads`, `HoloCompares` and a per-`buildChunk`
   duration histogram during the approach. A single multi-hundred-ms
   `buildChunk` confirms P2.
3. Log cull-dirty events separately from `markDirty` events while walking a fixed
   path with no block changes. Cull-dirty dominating confirms P3.
4. If the freeze only occurs with other mods present, add the bounded retry from
   §8 and see whether it converts a freeze into a log line.

**One thing the source rules out:** there is no lock-order deadlock available on
the render thread. The only mutex taken there is `model().mu`, and it is taken
with `try_to_lock`. `gApiMu` and `gCapMu` are taken on other threads. A classic
deadlock is not supported by this code; a *livelock* (P5) is.

---

## 4. Comparison of the proposed rendering backends

Assessed strictly against what this source already demonstrates.

| | A Wireframe | B Combined mesh | C Hybrid | D Reuse engine | E Shader |
|---|---|---|---|---|---|
| **Already in the codebase?** | **Yes** — cyan grid edges, cached per chunk | **Yes** — the textured chunk path | Partially — near/far sweeps exist, but not for LOD | **Yes** — it *is* the textured path | **No** |
| **Evidence** | `CyanStyle.hpp`, `gridEdgesFor` at `:1794`/`:1959`, `kCyanEdgesPerChunk=16384` (4 B/edge), `kCyanLinesPerFrame=4096`, near→far draw at `:2128` | `buildChunk` `:1857`, `MeshBlob` 0x400 B, `texCut` at ~2048 verts, `kMaxMeshes=6000` | `kCmpNear/kCmpFar` `:1171`, distance sort `:2055`, per-frame caps | `TessellateInWorld`, `GetBlockTessellator`, `BTSetCache`, `BlockRenderFlag`, `MeshRender`, `MatrixStack` all resolved and called | nothing |
| **Internal-face removal** | n/a (no faces) | two layers: whole-block exposure test (`:1968-1973`) + the engine's own culling via the `getBlock` window | inherits B | engine's own | n/a |
| **Greedy meshing** | not needed | **absent** — per-block quads only | absent | not applicable (engine emits per-block) | n/a |
| **Batching** | one flush per frame, capped | one `MeshBlob` per ~2048 verts | inherits B | one `sMeshRender` per blob | n/a |
| **Caching** | **yes**, per chunk | **yes**, per chunk | yes | yes | would need its own |
| **Rebuild trigger** | chunk dirty | chunk dirty | chunk dirty | chunk dirty | n/a |
| **Version risk** | low — uses `TessellatorBegin/Vertex/Color`, already mandatory | **high** — needs `TessellateInWorld`, `BTSetCache`, `BlockRenderFlag`, `MeshRender`, `MatrixStack`, plus `tess+0x290/0x294/0x10/0x70` field offsets | high | **highest** — all of B's dependencies | unknown |
| **Failure mode if unavailable** | degrades to lines | falls back to `emitColored` (already implemented, `:2018`) | degrades to A | already handled (`texReady()`, `:156`) | unproven |
| **Verdict** | **viable fallback today** | **viable, and already the primary path** | **recommended target** | keep, but stop treating it as the only path | **experimental only** |

### On Backend D specifically

The brief says not to recommend an internal function merely because its name
looks relevant. Here the opposite is true: D is not a proposal, it is the
existing implementation, and the source shows real evidence of the work needed —
`BlockActorProxy.hpp` for blocks the tessellator will not draw, the beproxy box
squeeze at `:1005-1012`, the zero-vertex detection and `gTexNoPrefill`
degradation at `:2010-2017`, and the area/finiteness validation at `:981-1000`.
Those are the fingerprints of someone who already discovered where D breaks.
**The cost of D is not availability, it is that every block costs an engine
tessellation call at build time.** That is P2.

### On Backend E specifically

There is **no shader or GLSL hook anywhere in the source**. The only GL usage is
`XrayLineScope` toggling `GL_DEPTH_TEST` (`0x0B71`) with `glIsEnabled` guards
(`:246-263`). Nothing in this codebase establishes that shader source can be
injected into this renderer. The installed skill does document the technique
(`references/render-pipeline-hooks.md`, `references/shader-source-injection.md`),
but Structura has no such hook and no evidence of feasibility on the target
build. **E must be recorded as an experimental research direction, not a plan.**

---

## 5. Recommended initial prototype

**Backend C, implemented as A + B under a common interface — not as a new
renderer.**

Reasoning, in order of weight:

1. **A and B already exist and already coexist.** `emitColored`/cyan edges are
   the wireframe path; `buildChunk`/`MeshBlob` is the mesh path. The missing
   piece is not a renderer, it is a *policy* that decides per region which one
   to use. That is a much smaller change than any rewrite.
2. **The proximity freeze is a policy failure, not a geometry failure.** P3 and
   P4 dirty chunks without any distance-based throttle; P1 draws immediate
   geometry without any distance-based cap. A distance-driven policy addresses
   all three with one mechanism.
3. **It is measurable before it is committed.** The `lm::perf` counters already
   distinguish builds, immediate quads and compares, so an A/B is cheap.
4. **It preserves the fallback chain.** If `texReady()` is false, or a chunk
   fails to tessellate, the code already degrades to coloured boxes. A LOD layer
   slots into that existing ladder instead of replacing it.

**Concretely, the first prototype is three changes, all inside the existing
files:**

* **Bound `buildChunk` itself**, not just the loop around it: give it a vertex
  budget and a deadline, let it finish partially and resume next frame. This
  directly closes P2.
* **Throttle cull-dirtying by distance**: only re-cull chunks inside a radius,
  and re-cull the rest on a slow cadence. This directly closes P3.
* **Cap immediate geometry by distance**, so the `special` population far from
  the player is dropped before `emitColored`, not at the 40 000-quad cap. This
  directly closes P1's worst case.

Deliberately **out of scope for prototype 1**: greedy meshing, a new vertex
format, backend E, and any change to signature resolution.

---

## 6. Proposed modular architecture

Eight responsibilities, mapped onto files that already exist.

| # | Responsibility | Owner (proposed) | Existing code |
|---|---|---|---|
| 1 | Structure data + world-space transform | `Structure.hpp/.cpp`, `Transformed.cpp` | **keep as-is** |
| 2 | Schematic vs world comparison | `Progress.cpp` + the sweep in `Hologram.cpp:1646-1709` | **extract** the sweep out of `drawStructure` |
| 3 | Geometry generation | `HologramMeshGen` (new) | **move** `buildChunk` + `texBlock` here |
| 4 | Geometry cache + invalidation | `HologramCache` (new) | **move** `gChunks`, `ChunkMeshes`, graveyard, `markDirty` |
| 5 | Visibility + LOD decision | `HologramLod` (new) | **new**; consumes camera + chunk distance |
| 6 | Backend selection | `IRenderBackend` + `MeshBackend` / `WireBackend` (new) | **wrap** `drawMesh` and `emitColored`/cyan |
| 7 | Submission to the engine | `HologramSubmit` (new) | **move** `flushQuads`/`flushLines`/`submitMesh` |
| 8 | Diagnostics, profiling, errors | `Perf.*`, `Diag.*`, `Compat.*` | **keep**, extend |

`Hologram.cpp` retains `install()`, `renderLevelDetour`, and orchestration. It
should stop owning geometry.

**Why each new component earns its place** (the brief asks for this explicitly):

* **3 separated from 7** because generation is a *build-time* cost and submission
  is a *frame-time* cost, and today both live inside `drawStructure` — a
  ~775-line function (`:1391-2163`) whose `buildChunk`, `emitColored`,
  `colorChunk`, `markDirty` and `compareAt` are all nested lambdas sharing one
  capture scope. P2 cannot be fixed while the build budget lives in the same
  scope as the drawing loop.
* **5 as its own component** because three current bugs (P1, P3, P4) are all
  missing distance policy. One component, three fixes.
* **6 as an interface** because the brief's core requirement — swap the backend
  without touching the rest — is otherwise unachievable: today `texOn` is a
  boolean that changes control flow in four places (`:1846`, `:1917`, `:2541`,
  `:2616`).
* **4 as its own component** because cache invalidation currently has three
  independent triggers (signature hash `:1514`, cull test `:2044`, `markDirty`
  `:1625`) with no single owner, which is exactly how P3 and P4 came to
  interact.

**Not proposed:** a scene graph, an ECS, a job scheduler, or a plugin system.
This is one renderer with two backends; four new components is already the most
this justifies.

**EasyPlace separation:** already good. EasyPlace reaches the hologram through
`StructuraApiV1` and the `FastWorld`/`fastBegin` path (`Hologram.hpp:74-90`),
which exists precisely so an EasyPlace step does not re-take the model mutex
hundreds of times. Responsibility 2 is the shared piece; keep it shared, keep
everything else separate.

---

## 7. Files and functions likely to change

| File | Change | Risk |
|---|---|---|
| `src/Hologram.cpp` | extract `buildChunk`, `texBlock`, `texCut`, `drawMesh`, `emitColored`, `flushQuads`, `flushLines`, the chunk map and graveyard | **high** — 3242 lines, most of the coupling is here |
| `src/Hologram.hpp` | expose the new component boundaries; `api()` and `FastWorld` unchanged | low |
| `src/Perf.hpp/.cpp` | add per-phase counters (window fill / tessellate / validate / submit), draw-call count, frame-time percentiles | low |
| `src/Game.hpp` | `lm::off` gains a version dimension | **medium** — see P7 |
| `src/Resolver.cpp` | unchanged logic; add an offset-validation path | medium |
| `src/Compat.cpp/.hpp` | new component states for LOD and backend availability | low |
| `src/Diag.cpp` | surface the new counters | low |
| `src/HoloState.cpp/.hpp` | new render-stage states for partial chunk builds | low |
| `src/main.cpp` | unchanged | none |
| `src/EasyPlace.cpp` | unchanged | none |
| `src/Ui.cpp` | unchanged (settings for the new LOD knobs may be added later) | none |

**Unchanged by design:** `Structure.*`, `Transformed.*`, `Nbt.*`,
`JavaFormats.*`, `Catalog.*`, `Library.*`, `Resolver*` (except the addition
above), `BlockStates.*`, `BlockNames.*`, `BlockActorProxy.hpp`, `CyanStyle.hpp`,
`ErrorVision.hpp`, `Paste.hpp`, `PlacePlan.hpp`, `Picker.*`, `I18n.*`,
`Theme.hpp`, `UiKit.*`, `Icons.*`, all tests.

**Functions most likely to be rewritten rather than moved:** the budget loop at
`:2062-2081` (must become a resumable generator) and the cull test at
`:2043-2051` (must become distance-aware).

---

## 8. Compatibility and stability risk assessment

| Risk | Evidence | Mitigation |
|---|---|---|
| **Hook chaining on `RenderLevel`** | `pl::memory::HookPriority::Normal` at `:2751`; `HookChain.hpp` documents that `rebuildChain` rewrites every slot | already handled correctly — the slot is re-read per call. **Do not "optimise" this into a cached pointer**; that is the C2 regression. |
| **Hook on `getBlock` collides with another mod's** | `:2769`; `HookPatch.hpp` exists specifically to detect six other hookers' patch shapes | the detection exists; what is missing is a *policy* for what to do when another mod is detected. Today the window still engages. |
| **Unbounded spin (P5)** | `:788`, `:2361` | bounded retry + log + **still call through if a slot ever appears**; never return without calling the original, or the frame is dropped. |
| **Duplicate init** | guarded (`sRenderHooked`, `:2680`) | none needed |
| **Duplicate watcher** | fixed by C4 (`main.cpp:269-270`) | none needed; keep the comment, it is load-bearing |
| **Calling-convention mismatch on `RenderMesh`** | the source already branches on 4 vs 5 args (`:210-215`, logged at `:2760`) | already handled |
| **Engine field offsets** (`tess+0x10/0x70/0x290/0x294/0x1cc`, `bt+0x78`) | raw reads at `:967`, `:971`, `:1001`, `:2468`, `:969` | **no validation exists.** These are the least-protected constants in the codebase and they are not in `lm::off`, so they are not even centralised. |
| **Graphics state** | `TessOffsetGuard`, colourHolder save/restore, `XrayLineScope` with `glIsEnabled` | correct; the D3 fix history shows this was already gotten wrong once |
| **Thread affinity** | window is `pthread_equal`-gated (`:776`); render state is render-thread-only (`:1339`) | correct |
| **Memory safety** | `safeRead` via `process_vm_readv` (`Game.hpp`), `plausiblePtr`, `untag` for MTE/PAC tag bits | unusually careful for this class of mod; keep |
| **Mesh lifetime vs GPU** | graveyard, ≥ 4 frames (`:1354`) | correct, but the 4-frame constant is a guess; a fence or a longer bound would be safer |
| **Symbol unresolvable** | `install()` hard-fails for the five essentials; everything else degrades | correct |
| **Silently disabling another mod** | C1 in the compat audit removed exactly this bug (`retireOldMods` unregistering *other* mods' ids) | keep it removed; add a test |

**The two under-protected items are the engine field offsets** (raw `+0xNN`
literals scattered through `Hologram.cpp`) **and the unbounded spin.** Both
should be addressed before any rendering work, because a redesign built on
unvalidated offsets inherits the risk.

---

## 9. Reproducible benchmarking plan

`lm::perf` already provides most of the instrumentation
(`Perf.hpp`: `RenderHook`, `MeshRebuild`, `WorldQuery`, `HoloCpuUs`,
`HoloCompares`, `HoloBuilds`, `HoloImmQuads`, `SafeReads`, `SvCalls/SvHits`,
plus `takeLine()` emitting a per-second line with fps and frame_time_ms).
**Build on it; do not replace it.**

### Metrics

| Metric | Available today? |
|---|---|
| FPS, mean frame time | yes — `takeLine()` |
| Frame-time **percentiles** (p50/p95/p99) | **no** — only mean. Add a ring buffer of frame deltas. |
| Geometry-generation CPU | partial — `HoloCpuUs` covers all of `drawStructure`. **Split it**: window fill / tessellate / validate / cache swap. |
| Submission CPU | **no** — add a scope around `flushQuads`/`flushLines`/`drawMesh`. |
| Draw calls | partial — `status().meshCalls` (`:233`, `:289`, `:1096`) counts mesh submissions but is not in the perf line. **Add it.** |
| Vertex / index counts | **no** — `tf->verts` exists internally; export it. |
| Mesh rebuild frequency | yes — `HoloBuilds` |
| World queries | yes — `WorldQuery`, `HoloCompares` |
| Memory / allocation frequency | partial — `Alloc` counts specific sites only |
| Freeze duration | **no** — add a max-frame-time watermark and a stall counter |
| GPU time | **not measurable from source**; requires a device profiler |

**One existing diagnostics defect to fix first:** `Hologram.cpp:2162` does
`st.texVerts.store(nMeshes, ...)` — the field is named `texVerts` but receives a
*mesh* count (its three neighbours at `:2159-2161` store `ready`, `pending` and
`tf->zero` correctly). Any benchmark reading that field would be wrong.

### Test matrix

| # | Case | Purpose |
|---|---|---|
| T0 | Hologram loaded, nothing rendered | baseline; isolates the `getBlock` hook cost (P6) |
| T1 | Small structure (≤ 1 k blocks) | floor |
| T2 | Medium (≈ 8 k) | typical |
| T3 | Large (≥ 32 k) | scaling |
| T4 | Dense — many adjacent opaque blocks | exercises face culling and the `getBlock` window |
| T5 | Sparse — many exposed faces | worst case for geometry volume |
| T6 | Block-entity-heavy (chests, signs, beds) | worst case for the `special`/immediate path (P1) |
| T7 | Player far, static | steady state |
| T8 | **Player approaching, then circling** | P3 cull churn — the reported freeze condition |
| T9 | Player placing/breaking blocks at the boundary | P4 `markDirty` fan-out |
| T10 | Frequent structure reloads | cache invalidation + graveyard pressure |
| T11 | T8 repeated with 2-3 other hooking mods loaded | P5 coexistence |

For each: mean/p95/p99 frame time, max frame time, `HoloBuilds`,
`HoloImmQuads`, `HoloCompares`, draw calls, mesh count, `meshCalls`, and a
`buildChunk` duration histogram.

### Acceptance criteria (to be fixed *before* implementing)

No baseline value is asserted here, because none was measured. The criteria are
stated as relations so they can be filled in from T0-T11 on the real device:

1. p99 frame time in T8 ≤ 1.5 × p99 in T7.
2. Max single `buildChunk` duration ≤ 4 ms in every test.
3. `HoloImmQuads` in T6 ≤ 2 × `HoloImmQuads` in T1.
4. No frame exceeds 100 ms in any test.
5. T11 shows no freeze and produces a log line if a spin is ever bounded out.
6. Draw calls in T3 grow sub-linearly with block count.

**Do not claim an improvement without the before/after pair from the same
device, same world, same structure.**

---

## 10. Phased implementation plan

**Phase 0 — Instrumentation and safety, no behaviour change.**
Split `HoloCpuUs` into phases; export draw calls and vertex counts; add frame
percentiles and a max-frame watermark; fix the `texVerts` mislabel; centralise
the raw `+0xNN` engine offsets into `lm::off` with a validation path; add the
bounded retry to both spins (logging, still calling through).
*Accept:* T0-T11 produce a complete metrics line; no frame-time regression; the
bounded retry never fires in a solo-mod run.

**Phase 1 — Bound the work (P2).**
Make `buildChunk` resumable: vertex budget + deadline, partial completion,
resume next frame.
*Accept:* criterion 2 (max `buildChunk` ≤ 4 ms) across T1-T6; no visual
difference once the hologram finishes building.

**Phase 2 — Distance policy (P1, P3).**
Distance-aware cull throttling; distance cap on immediate geometry.
*Accept:* criteria 1 and 3; T8 shows cull-dirty events dropping with distance.

**Phase 3 — Extract the components (§6).**
Pure refactor: move generation, cache, LOD, submission out of `Hologram.cpp`.
*Accept:* all metrics within 5 % of Phase 2; all existing tests still pass
(`host_test`, `hookchain_test`, `resolver_rules_test`, `document_cb_test`,
`beproxy_test`, `ep_test`, `paste_test`, `error_vision_test`, `cyan_test`).

**Phase 4 — Backend interface + wireframe backend (A).**
`IRenderBackend`, with the existing cyan/`emitColored` path behind it as the
low-cost backend.
*Accept:* switching backends at runtime changes only rendering; T6 with the
wireframe backend meets criterion 4.

**Phase 5 — Greedy meshing inside the mesh backend (B).**
Only if Phase 1-4 measurements show vertex volume, not build cost, is the
remaining bottleneck.
*Accept:* vertex count in T5 drops materially with no visual difference.

**Phase 6 — Hybrid LOD (C).**
Distance-based backend selection per chunk.
*Accept:* criteria 1, 3 and 4 simultaneously in T8.

**Phase 7 — Backend E investigation.** Research only, explicitly gated on
establishing that shader injection is possible on the target build. Not a
committed deliverable.

---

## 11. Unresolved questions requiring binary or device work

1. **Do the 20 signatures in `lm::sig` resolve uniquely on the actual
   1.26.52.03 RC2 `libminecraftpe.so`?** The source asserts uniqueness for
   `LocalPlayerNormalTick` only. Can be answered offline with the installed
   skill: `python3 skills/scripts/verify_signatures.py <so> --sigs …`.
2. **Are the `lm::off` constants correct for that build?** Needs
   `scripts/offset_check.py` or manual disassembly (P7).
3. **Are the raw engine field offsets correct** — `tess+0x10` (vertex run),
   `+0x70` (colour run), `+0x290` (vertex count), `+0x294` (capacity), `+0x1cc`
   (offset), `bt+0x78` (render flag)? No validation exists; a wrong one is a
   silent misread or a crash.
4. **Is `RenderMesh` really 4-arg or 5-arg on the target?** The source branches
   at runtime (`:210-215`), which is safe, but the branch means one path is
   untested on any given build.
5. **How often does `getBlockDetour` actually run per frame?** Determines whether
   P6 matters. Needs an on-device counter.
6. **Does the freeze reproduce with no other mods loaded?** The single most
   informative experiment, and it separates P5 from P2/P3 (§3).
7. **What is the real distribution of `special` blocks** in a texture-heavy
   structure? Determines whether P1 or P2 dominates.
8. **Is the 4-frame mesh graveyard sufficient** for the target GPU, or can a
   mesh be destroyed while still in flight? Needs a stress test.
9. **Does the `twoSided` path double the cost** as P8 predicts? Pure GPU
   question.
10. **Is shader injection into this renderer possible at all?** Gates Backend E.
    No evidence either way in the source.

---

## Appendix: what was and was not verified

**Verified from the source** (file:line given throughout): the hook inventory,
the call graph, the two rendering paths, the comparison budgets, the build
budgets and their post-hoc check, the cull constants, the `markDirty` fan-out,
the two unbounded spins, the idempotent install, the poll-then-settle startup,
the hard-fail on missing essentials, the single tested build-id, the flat offset
table, the mesh graveyard, the GL state guards, the `getBlock` window design,
the perf counter inventory, and the `texVerts` mislabel.

**Not verified:** every performance magnitude, every ABI and calling convention,
every signature's uniqueness, every offset's correctness, and the actual cause
of the reported freeze. Those need the binary and a device.

**Not done, per the phase restriction:** no file was modified, no placeholder
was written, no hook or offset was changed, nothing was built or packaged, no
feature was removed, and no rewrite was started.
