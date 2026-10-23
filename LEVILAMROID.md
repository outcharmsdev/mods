# LeviLamroid — source audit

Audit of the `AGENTS.md` pasted into this session against the actual source at
[spygamingog/LeviLamroid](https://github.com/spygamingog/LeviLamroid), cloned at
commit `f988a23` ("feat(1.0.4-beta): complete 1.26.45.1 reverse engineering and
multi-version golden RVAs"). 759 files, 23 MB (12 MB of it `data/*.json`, 9.5 MB
`include/`).

Every line below was checked with `grep`/`sed` against the checkout. Nothing here
is taken from the document on trust.

## Verified correct

| Doc claim | Evidence |
|---|---|
| §3.2 AArch64 trampoline `LDR X16,#8; BR X16; <64-bit addr>`, icache flush | `src/memory/Hook.cpp:108` `inlineHookAArch64`, `:143` and `:157` emit `0x58000050` (`ldr x16, #8`), `:164-165` `__builtin___clear_cache` |
| §3.3 `glClear` GOT RVAs `0x132893D8` / `0x124C9688` | `src/mcpe/Offsets.cpp:54` and `:435`, exact match |
| §3.1 base discovery via `dl_iterate_phdr` → `PT_NOTE` → `NT_GNU_BUILD_ID`, then `/proc/self/maps`, then `/proc/self/cmdline` | `src/mcpe/Offsets.cpp:802` `autoDetectVersion()`, `:805` `dl_iterate_phdr`, `:812` `PT_NOTE`, `:822` `NT_GNU_BUILD_ID`, `:857` maps, `:881` cmdline |
| §2 ImGui "v1.91.x" | `include/imgui/imgui.h:31` `IMGUI_VERSION "1.91.8"`, `:32` `19180` |
| §4 fallback exports `PLMod_Load`, `LeviMod_Load`, `ll_mod_load` | `src/LeviLamroid.cpp:118`, `:140`, `:145` (plus `PLMod_Enable/Disable/Unload`, `ll_mod_enable/disable/unload`, which the doc omits) |
| §2 `FUNCTIONS.md`, `template/`, `include/ll/api/`, `include/pl/`, `include/mc/` | all present |

## Wrong, or materially incomplete

### §3.1 Build IDs — the match is a prefix, and two versions are missing

The doc presents two full 40-char SHA-1s as *the* mapping. `src/mcpe/Offsets.cpp:841-852`
actually does `buildId.starts_with(...)` on **8-char prefixes**, and covers
**four** versions:

| Prefix | Version | In the doc? |
|---|---|---|
| `b480c79a` | 1.26.44.3 | **no** |
| `56de9eed` | 1.26.52.3 | yes |
| `868e275c` | 1.26.45.1 | yes |
| `86895c02` | 1.26.45.1 | **no** (a second accepted build for the same version) |
| `712509dc` | 1.26.51.1 | **no** |

Consequence: someone following §5 to add a new version will add one full SHA-1
and may not realise a variant build needs a second prefix, or that 1.26.44.3 and
1.26.51.1 are already supported.

### §5 step 4 — `sVersionTable` does not exist

`grep -rn sVersionTable` over the repo returns nothing. The real symbols in
`src/mcpe/Offsets.cpp` are:

```
:14   static const EngineOffsets kOffsets_1_26_52_3
:212  static const EngineOffsets kOffsets_1_26_44_3
:404  static const EngineOffsets kOffsets_1_26_45_1
:582  static const EngineOffsets kOffsets_1_26_51_1
:773  static std::string      sDetectedVersion
:774  static EngineOffsets    sActiveOffsets
```

Adding a version means adding a `kOffsets_<ver>` table **and** wiring it into the
`setTargetVersion()` dispatch at `:790-796` **and** the prefix branch at
`:841-852`. The doc describes one edit where three are required.

### §4 `ModRegistration` — wrong field count and wrong function type

The doc shows four `pl::mod::LifecycleFunc` members. `include/pl/Mod.hpp:177-188`
has:

```cpp
using LifecycleFunction = bool (*)(void *instance, ModContext &context);

struct ModRegistration {
  void *instance{};        // <-- first field, absent from the doc
  LifecycleFunction load{};
  LifecycleFunction enable{};
  LifecycleFunction disable{};
  LifecycleFunction unload{};
};
```

Five fields, with `instance` **first**, and each callback takes `(void* instance,
ModContext&)` — not a bare `void(*)(ModContext&)`. This matters more than usual
here, because the doc's own `[!CRITICAL]` block warns that a malformed
registration causes `SIGBUS (BUS_ADRALN)`. Building one to the documented shape
puts `load` where the host reads `instance`.

Separately, the repo declares the same exported symbol two different ways:

* `include/pl/Mod.hpp:319` and the `PL_REGISTER_MOD` macro at `:326` —
  `::pl::mod::ModRegistration *PLGetModRegistration()`
* `include/ll/api/mod/RegisterHelper.h:44` and `src/LeviLamroid.cpp:112` —
  `const void* PLGetModRegistration()`, over an anonymous
  `{void* inst; bool(*load)(void*, void*); ...}`

Those two are ABI-compatible (`ModContext&` is passed as a pointer, so
`bool(*)(void*, ModContext&)` and `bool(*)(void*, void*)` have the same calling
convention), so this is a type-level divergence rather than a crash — but the
doc presents only one shape and does not mention the second path exists.

### §2 code map — stale

| Doc says | Actually |
|---|---|
| `data/` has 2 offset JSONs | **3**: also `offsets_1.26.51.1_arm64.json`; and `kOffsets_1_26_44_3` has no JSON at all |
| `src/hooks/` = "Core, UI, and Render" | **8** files: Block, Command, Core, Item, Recipe, Render, Resource, UI |
| — | omits `src/mcpe/Symbols.cpp`, `src/mcpe/Symbols_1_26_52_3.inl`, `src/resource/{AddonIngestion.cpp,AddonIngestion.h,ResourceManager.cpp}` |

### §7 `re_studio` — not published by this maintainer

`universal-modder/tools/re_studio/main.py` is not in this repo (there is no
`tools/` directory), and `spygamingog` has no `universal-modder` repository —
a GitHub search for that name returns unrelated projects. The three commands in
§7 cannot be run or verified from anything public.

### Style: absolute local paths

§5 step 4 links `file:///home/devroom/LeviLamroid/src/mcpe/Offsets.cpp`. The
skill's own rule (`AGENTS.md` §0) is that deliverables be machine-agnostic —
placeholders, not local absolute paths.

## Licensing

`LICENSE` and `COPYING.LESSER` are byte-identical (md5 `3000208d…`) and are the
**LGPL-3.0** text; `COPYING.txt` is GPL-3.0. `NOTICE.md` states LGPL-3.0 and
credits LiteLDev/LeviLamina. So this is LGPL-3.0 — different from both other
trees in this workspace (the skill is GPL-3.0; `BedrockTools-main`'s `LICENSE`
file is MIT while its README claims GPL-3.0).

## Methodological conflict with the installed skill

This is a genuine fork in the road, not a wording difference.

| | BedrockTools / skill | LeviLamroid |
|---|---|---|
| Locating game functions | **byte signatures** resolved at runtime (`pl::memory::resolveSignature`) | **golden RVA tables** per version + Build-ID detection |
| Failure mode on a new build | loud — the scan returns `MISSING` | **silent** — the RVA resolves to the wrong function |
| Hook engine | `pl::memory::hook` / Gloss | own `inlineHookAArch64` + GOT patching |
| API surface | `pl::` + `bedrocktools::` | `ll::api` (LeviLamina parity) + ImGui UI |
| Target versions | `levimod.json` → `1.26.5X.X` | 1.26.44.3 / 45.1 / 51.1 / 52.3 |

The skill's `references/version-porting.md` and `scripts/offset_check.py` exist
specifically because hard-coded displacements fail silently — the exact risk the
RVA-table design takes on. Both designs are defensible; they should not be mixed
without deciding which one wins for a given deliverable.

The workspace `AGENTS.md` currently says:

> **Method source**: use `BedrockTools-main` … Other mod projects in the workspace
> may be used only as build-environment references, never as the methodology
> source for deliverables.

Adopting this document as the workspace `AGENTS.md` would reverse that rule.

## Not verified

* Nothing was compiled (no NDK/CMake/xmake in this sandbox) and nothing was run
  on a device, so no RVA, hook, or Build ID was checked against a real
  `libminecraftpe.so`. The audit above is source-level only.
* The `data/*.json` tables (12 MB) were not cross-checked against
  `EngineOffsets`.
