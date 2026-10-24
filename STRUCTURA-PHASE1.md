# Structura Phase 1 — rendering rewrite, first increment

Target unchanged: Bedrock **1.26.52.03 RC2**, LeviLauncher Android, `arm64-v8a`, native `.so`.
Baseline for this increment: the Phase 0 tree (commit `c387ddb`).

This is an implementation increment, not an audit. It does two things in real source:

1. **Splits rendering into four separately testable components**, each engine-free.
2. **Makes chunk mesh generation resumable and bounded in work units**, replacing a time budget that
   could only be consulted after a whole chunk finished.

It deliberately stops before additional backends or advanced techniques, as instructed.

---

## 1. Implementation diff

`STRUCTURA-PHASE1.patch` — **84 644 bytes**, applies with `patch -p1` from the Structura root on top
of the Phase 0 tree. Verified: `--dry-run` clean, real apply, and the result is **byte-identical** to
the tree that was compiled and tested (`diff -r` reports no differences).

```
  scripts/host_test.sh                   +31     -1
  src/Hologram.cpp                       +186    -41
  src/render/HoloBackend.hpp             +111    -0
  src/render/HoloGeometry.hpp            +147    -0
  src/render/HoloLod.hpp                 +167    -0
  src/render/HoloMeshState.hpp           +101    -0
  tests/render/backend_test.cpp          +165    -0
  tests/render/geometry_test.cpp         +222    -0
  tests/render/lod_test.cpp              +217    -0
  tests/render/meshstate_test.cpp        +192    -0
  ------------------------------------------------------------
  total:                                +1539    -42      (10 files: 6 new, 2 modified, 2 script)
```

## 2. Files changed

**New components** (`src/render/`, all in `namespace lm::holo::*` to match the project convention):

| File | Responsibility | Engine deps |
|---|---|---|
| `HoloGeometry.hpp` | Geometry generation bookkeeping: window addressing, exposure test, resumable `BuildCursor`, vertex/block budget | none |
| `HoloMeshState.hpp` | Cached mesh storage + invalidation lifecycle (`SlotState`) | none |
| `HoloLod.hpp` | Distance visibility + per-frame workload policy (`Scheduler`, `Need`, `Vis`) | none |
| `HoloBackend.hpp` | Backend selection from resolved capabilities + submission budget | none |

Every one is header-only and includes nothing but `<cstdint>`/`<cstddef>`, so they compile and run on
the host with no game, no device and no NDK. That is the point of the split: the logic that decides
*what to build, when to stop, and what is safe to draw* is now testable, instead of being buried in a
775-line lambda that cannot be compiled off-device.

**Modified:**

- `src/Hologram.cpp` (+186/−41) — the components are wired into `drawStructure`; no public interface,
  setting or hook changed.
- `scripts/host_test.sh` (+31/−1) — registers the four new suites **and** the three Phase 0 suites
  (`hookchain_phase0`, `perf_test`, `offsets_test`), which Phase 0 had left out of the project runner.
  Adds one mutation check and extends the existing purity rule to the new headers.

**Deliberately untouched:** structure loading/parsing, placement + EasyPlace, hologram styles and
block visibility, other-mod compatibility, all settings and public APIs, the panel and floating UI,
`HookChain.hpp`, and both hook detours.

## 3. New architecture, and how the first backend works

Four components, matching the four responsibilities that were previously interleaved:

```
        ┌──────────────────────┐   which blocks, when to stop
        │ HoloGeometry         │   BuildCursor (resumable), exposure test, block/vertex budget
        └──────────┬───────────┘
                   ▼
        ┌──────────────────────┐   what exists, what is safe to draw
        │ HoloMeshState        │   SlotState: hasPublished / building / invalid{World,View}
        └──────────┬───────────┘
                   ▼
        ┌──────────────────────┐   how much work this frame, in what order
        │ HoloLod              │   Scheduler: blocks/verts/starts/resumes/viewBlocks/time
        └──────────┬───────────┘
                   ▼
        ┌──────────────────────┐   how geometry reaches the screen
        │ HoloBackend          │   decide(capabilities, requested) -> {Mesh | Wire} + reason
        └──────────────────────┘
```

No scene graph, no ECS, no job scheduler, no plugin system.

**The first backend is `Mesh`** — the combined custom mesh path, which is the existing textured path
now selected explicitly rather than implicitly. It uses only what the source already verified:

- `sTessInWorld` tessellates a block into the engine's tessellator;
- `sTessEnd` closes the tessellator into a mesh object, wrapped in the existing `MeshBlob`
  (`alignas(16) uint8_t b[0x400]`, unchanged);
- `drawMesh` positions it via the existing `sMatPush`/`sMatPop` matrix path and submits it with
  `sMeshRender`;
- `sMeshDestroy` releases it, through the existing ≥4-frame graveyard.

Geometry is produced **per chunk and submitted per mesh**, not per vertex — so the per-vertex `sVertex`
path (audit P9) is not used by this backend at all. Nothing was invented: no RenderDragon shader
access, no new engine API, no new signature, no new offset.

**`Wire` is the fallback and the diagnostic mode.** It is the immediate-geometry path already in the
source (`colorChunk` → `gQuads`/`gLines`, plus the cyan edge grid), reached either when a capability
is missing or when `gForceWire` is set. When `Mesh` is unavailable the chunk is regenerated as
immediate geometry — the same branch the existing no-matrix case already used — and the cached meshes
are left intact, so switching back costs nothing.

Selection is capability-driven and never guesses:

| Condition | Result |
|---|---|
| all resolved | `Mesh`, reason `none` |
| `sTessInWorld`/`sVertex` missing | not viable — submission skipped, never a null deref |
| mesh pipeline missing | `Wire`, reason `no-mesh-pipeline` |
| materials missing | `Wire`, reason `no-materials` |
| `gForceWire` | `Wire`, reason `forced` — checked **before** the mesh pipeline, so the diagnostic mode stays reachable when the mesh path is exactly what broke |

`gForceWire` is an internal `std::atomic<bool>`; it is **not yet surfaced in the settings UI** (see §6).

## 4. Bounded work, and what happens to a partial build

### What was wrong

The previous guard was `if (any && nowMs() - t0 > 2.5) break;`, evaluated **between whole chunks**. It
bounded how many chunks were *started* per frame and never the cost of any single one. One chunk is a
20³ window fill plus up to 16³ `sTessInWorld` calls; the guard could only notice afterwards. Camera
movement made it worse: the cull-band check set `dirty` on chunks the camera passed, so those rebuilds
competed with real edits for `kMaxRebuildsPerFrame = 1`.

### What replaced it

**Work is now counted in blocks and vertices**, and the budget is consulted **between y-slabs inside
the tessellation loop** — not between chunks. A slab is at most 16×16 blocks, so a frame can overshoot
its ceiling by no more than one slab.

`HoloLod::Scheduler` ceilings per frame: `blocks = 384`, `verts = 16384`, `starts = 1`, `resumes = 1`,
`viewBlocks = 96`, `us = 1500`. Wall clock is kept only as a secondary stop, because a time budget can
never be enforced mid-work — it can only be read after the fact.

Slabs run outermost so the check has somewhere to live. Which axis is outermost does not change what
geometry is produced, only the order, so this is output-neutral.

**Camera movement is no longer a world edit.** The cull-band invalidation now records
`InvalidView` instead of `dirty`. A chunk whose only problem is a stale cull band:

- keeps drawing the mesh it already has,
- draws on a separate, smaller allowance (`viewBlocks = 96` of the 384),
- and is never counted as a world rebuild.

This is the direct answer to "avoid rebuilding on camera move": the rebuild still happens when it is
genuinely needed, but it can no longer crowd out real geometry changes, and the hysteresis threshold
(`kCullRebuild = 8`) is now an explicit, tested function (`viewStaleOnAxis`) rather than an inline
comparison.

### Partial builds

The invariant is enforced by `HoloMeshState::SlotState`, which separates two things that were one
flag:

- `hasPublished` — a complete mesh set is installed and drawable;
- `building` — a build is in flight, accumulating into staging.

Consequences, each pinned by a test:

1. **A build never disturbs the published mesh.** `beginBuild` sets `building` and touches nothing
   else, so a chunk on screen keeps drawing its old mesh for the entire rebuild.
2. **A partial build is never published.** `commit` is the only way `hasPublished` becomes true and it
   refuses unless a build is in flight. A build that ran out of budget stays in `building`; its
   staging survives and the chunk simply is not drawable yet if it never had a mesh.
3. **A second build on the same chunk is refused**, so the first one's staging cannot be orphaned.
4. **`abortBuild` keeps the previous mesh** — abandoning a build is not losing a chunk's geometry.
5. **Invalidation never stops drawing.** `invalidate` only sets the mask.
6. **A stalled build cannot wedge a chunk.** A build in flight for `kBuildStallFrames = 120` frames is
   presumed abandoned and released; the published mesh survives. The frame comparison is unsigned
   subtraction, so counter wraparound cannot make a fresh build look stalled.

**Why stopping between frames is safe here.** `texCut` is called at every exit, including the partial
one. It turns whatever the tessellator holds into a *complete* `MeshBlob` appended to staging and then
calls `sTessClear` with `begun = false`. So the engine never inherits Structura's half-filled vertices
across a frame boundary — this was verified in the source before the design was committed to, not
assumed.

**One real constraint found and handled.** The `sBTSetCache` prefill happens only in the window phase,
and `texFrameEnd` resets the BlockTessellator cache at the end of every frame. A build that spans
frames therefore cannot rely on that cache. Multi-frame builds are gated on
`sGetBlockHooked && tf->region != 0` — when the `getBlock` hook can answer for the window from
`gWinBlk` (re-published every frame of the build, not just the first), the window is still correct.
When it cannot, the chunk is built in one pass exactly as before. This is a deliberate conditional
degradation rather than a guess about engine state surviving a frame boundary.

### Approach-freeze items respected

- Hook-chain call-through and LeviLauncher mod compatibility are unchanged; no hook file was touched.
- **No timeout that skips the original engine call** was introduced. The unbounded chain wait from
  Phase 0 is still unbounded, for the reason recorded there.
- The panel and floating UI were not redesigned; no UI file was touched.
- All settings and public APIs are unchanged. The only new symbol with external reach is the internal
  `gForceWire` atomic.

## 5. Test and build results

**Project runner** — `bash scripts/host_test.sh`, exit **0**, end to end:

```
compat_check: OK
hookchain: 20 passed, 0 failed
hookchain_phase0: 18 passed, 0 failed
perf: 64 passed, 0 failed
offsets: 11 passed, 0 failed (binary validation PENDING - no libminecraftpe.so)
geometry: 217 passed, 0 failed          <- new
meshstate: 87 passed, 0 failed          <- new
lod: 76 passed, 0 failed                <- new
backend: 47 passed, 0 failed            <- new
meshstate mutation: detected (meshstate: 85 passed, 2 failed)   <- new
document_cb (1.21.5 client): 7 passed, 0 failed
beproxy: 3307 ok, 0 failed
error_vision: 568 passed, 0 failed      (+2 mutations detected)
cyan: 326 passed, 0 failed              (+2 mutations detected)
ep: 28 passed, 0 failed                 (+3 mutations detected)
paste: 1365 passed, 0 failed            (+2 mutations detected)
static rules (ep): OK
static rules (cyan/paste): OK
```

**427 new assertions** across the four suites. All 10 pre-existing suites pass **unmodified**.

The mutation check is real, not decorative: it removes the `if (!s.building) return false;` guard from
`commit()`, and the suite fails with 2 failures. Getting it to work required changing the new tests to
non-relative includes — with `#include "../../src/render/…"`, `-I` cannot redirect the header and the
mutant silently passed. That was caught by running the mutation rather than assuming it worked.

Build flags: `g++ -std=c++20 -O1 -g -fsanitize=address,undefined -pthread`.

**`Hologram.cpp`** syntax-checks clean: `g++ -std=c++20 -fsyntax-only -Isrc -I<plstub>` → **0 errors**.
With `-Wall -Wextra` it produces **4 warnings, the same 4 the pristine baseline produces** (verified by
running the identical command against the unmodified tree), so no new warning was introduced.

### What was NOT verified — stated plainly

- **No `.so` was built.** This environment has no `cmake`, `ninja`, `clang`, `xmake`, `ndk-build` or
  `adb`, and no NDK. `Hologram.cpp` and `main.cpp` cannot be compiled against the real
  `vendor/preloader-android` headers here; they were syntax-checked against hand-written stubs
  (`pl/Logger.hpp`, `pl/memory/Hook.hpp`, `pl/memory/Signature.hpp`, `EGL/egl.h`). That catches type
  and name errors — it proves nothing about linking or codegen.
- **No performance number is claimed.** No frame time, no build time, no device measurement. The
  ceilings above are design parameters chosen to be conservative, not measured optima.
- The runtime suite (`host_test`, `paste_roundtrip_test`) is **skipped**, not passing: it needs
  `build/block_pack.zip` plus `zlib`, and `zlib.h` is absent with no working `apt`.
- `offsets_test` still reports **binary validation PENDING** — there is no `libminecraftpe.so` here.

## 6. Remaining blockers needing device validation

1. **The whole increment is unrun on a device.** Nothing here has executed inside Bedrock. The
   components are host-tested; the wiring in `drawStructure` is syntax-checked only.
2. **Multi-frame builds depend on the `getBlock` hook path.** The gating condition is
   `sGetBlockHooked && tf->region != 0`. On a device it must be confirmed that (a) the hook is
   installed in the configurations that matter, and (b) answering the window from `gWinBlk` across a
   frame boundary produces identical geometry to the prefilled-cache path. If (b) fails, the fix is to
   complete those chunks in one pass — the gate already exists, it just needs tightening.
3. **The budget ceilings need measuring, not guessing.** `blocks = 384`, `verts = 16384`,
   `viewBlocks = 96`, `us = 1500` are unvalidated. They should be tuned against the Phase 0
   `[perf:phase]` line, in particular `HoloBuildMaxUs`, on the T8 scenario (approach + circle) that
   originally froze.
4. **`gForceWire` has no UI.** The wireframe diagnostic mode is reachable only from code. Wiring it to
   a settings toggle was left out to avoid touching the panel, which was explicitly out of scope.
5. **The offsets are still unvalidated against the binary** (carried over from Phase 0). Phase 1
   added no new offsets.
6. **Slab-outermost iteration is unverified visually.** It is output-neutral by construction — same
   blocks, same geometry, different production order, which can shift where a mesh is split — but that
   reasoning has not been confirmed against a rendered hologram.

### Recommended device sequence

1. Load on 1.26.52.03 RC2, confirm the hologram renders identically with `Mesh` selected.
2. Force `Wire` and confirm the diagnostic path renders, then switch back.
3. Run T8 (approach + circle) and read `[perf:phase]`; compare `HoloBuildMaxUs` against Phase 0.
4. Tune the ceilings from what the counters show, not from intuition.

---

**Stopping here for review**, as instructed, before any further backend or advanced technique.
