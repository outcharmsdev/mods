# SETUP.md — this workspace

What is checked in here, what was verified, and what still needs a real machine.

Read `README.md` for what the skill is, `AGENTS.md` / `CLAUDE.md` for the
working rules, `HOOK_TUTORIAL.md` for the end-to-end pipeline. This file only
covers *this* sandbox.

## Layout

| Path | What it is | Provenance |
|---|---|---|
| `SKILL.md`, `skills/` | the `bedrock-pe-modding` skill (entry + 20 references + 12 scripts + 14 button templates) | [fangkuaichaoge/BedrockTools_skill](https://github.com/fangkuaichaoge/BedrockTools_skill), GPL-3.0 |
| `AGENTS.md`, `CLAUDE.md`, `HOOK_TUTORIAL.md`, `README.md` | the skill's repo-level guides | same |
| `LICENSE`, `NOTICE` | the skill's licence and upstream credits | same |
| `BedrockTools-main/` | the **reference mod**: 173 files, 162 `.cpp`/`.hpp`, 124 signatures, xmake build | [QYCottage/BedrockTools](https://github.com/QYCottage/BedrockTools) |
| `tools/selftest/` | synthetic-ELF self-test for the skill's toolchain | written for this workspace |

`BedrockTools-main/` is named that way on purpose: `verify_signatures.py`
defaults `--sigs` to `./BedrockTools-main/src/core/memory/Signatures.cpp`, and
`AGENTS.md` names it as the method source. Renaming it silently breaks the
default.

## Dependencies installed here

```sh
pip install --break-system-packages -r requirements.txt
```

`--break-system-packages` is needed because this image's Python is
externally-managed (PEP 668). Installed versions: Python 3.11.2, numpy 2.4.6,
capstone 5.0.7. They land in `/usr/local/lib/python3.11/dist-packages`, which is
outside the workspace, so **they do not persist in a workspace snapshot** — run
the command again in a fresh sandbox.

## Verification

```sh
python3 tools/selftest/run_selftest.py
```

There is no `libminecraftpe.so` in a clean sandbox and shipping one is not an
option, so `make_fake_so.py` hand-builds a 29 KB AArch64 ELF with the structure
the tools actually read: real ARM64 instructions, an `.eh_frame_hdr` function
table, `.rodata` literals, a literal pointer table, and an RTTI → typeinfo →
vtable chain carried entirely in `.rela.dyn` addends (slots are zero in the
file, as on a PIE image).

`run_selftest.py` then runs all twelve tools against it and **asserts the
output** — 20 checks, including the exact guard bits per call site, the
UNIQUE/AMBIGUOUS/MISSING verdicts, the vtable slot count and addresses, and the
struct displacements `offset_check.py` reads back. A tool that runs but reports
nothing would pass a smoke test; it does not pass this.

The fixture also encodes two traps deliberately, so the negative paths are
covered:

* `face_up` has a byte-identical copy at `0x1300`, so any pattern taken from it
  is AMBIGUOUS — `mksig` and `sigscan` must both say so.
* the vtable slots are zero in the file, so `a64 ptr` correctly finds nothing
  there. That is the documented reason `vtable_walk.py` exists.

The real signature dictionary was also exercised end to end: from the repo root,
`python3 skills/scripts/verify_signatures.py <so>` picks up
`BedrockTools-main/src/core/memory/Signatures.cpp` with no `--sigs` flag and
parses **124** signatures, all well-formed tokens.

## Not available in this sandbox

Stated plainly so nothing gets assumed:

* **No Android NDK, no CMake, no Ninja, no xmake.** The mod's `xmake.lua` build
  (`xmake f -y -p android -a arm64-v8a -m release --ndk=<NDK> && xmake -y`)
  cannot run here, so **no `.so` was compiled and no mod was deployed.** The C++
  in `BedrockTools-main/` is reference source, read but not built.
* **No device, no `adb`.** Nothing was run against Minecraft; no `logcat` was
  read.
* **No disassembler / IDA.** This is explicitly supported by the skill
  (`SKILL.md`, "Tool selection"): the workflow must not depend on one. `a64.py`
  is the offline substitute and it is what the self-test exercises.
* `ida_mcp_client.py` and `ida_decompile_targets.py` were not exercised — they
  need a live IDA instance.

So: the *analysis* half of the workflow is verified here; the *build and deploy*
half needs a machine with the NDK and a device.
