#!/usr/bin/env python3
"""End-to-end self-test for the BedrockTools skill toolchain.

Every script under `skills/scripts/` is exercised against the synthetic ELF
built by `make_fake_so.py`, and the output is *asserted*, not just printed: a
tool that runs but silently reports nothing would pass a smoke test and still be
useless. Each check names the fixture fact it is recovering, so a failure says
which capability broke.

Run:
    python3 tools/selftest/run_selftest.py [path/to/skills/scripts]

Exit status is 0 only if every check passes.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from make_fake_so import (  # noqa: E402
    CAM_OFFSETS, DATA_AT, FUNC_DRIVER, FUNC_FACE_DOWN, FUNC_FACE_NORTH,
    FUNC_FACE_UP, FUNC_SETUP_CAMERA, STR_GAMEMODE_ATTACK, TYPEINFO_DTORPAIR,
    TYPEINFO_FACEUPBASE, VTABLE_DTORPAIR, VTABLE_FACEUPBASE,
)

PROLOGUE_UP = "E0 03 13 AA 00 00 01 8B 00 10 2E 1E C0 03 5F D6"
PROLOGUE_DOWN = "E0 03 14 AA 00 00 02 8B 00 10 2E 1E C0 03 5F D6"
PROLOGUE_NORTH = "E0 03 15 AA 00 00 03 8B 00 10 2E 1E C0 03 5F D6"
PROLOGUE_ABSENT = "DE AD BE EF 00 11 22 33 44 55 66 77"

PATTERNS = {
    "FaceUp": PROLOGUE_UP,
    "FaceDown": PROLOGUE_DOWN,
    "FaceNorth": PROLOGUE_NORTH,
    "NoSuchFunction": PROLOGUE_ABSENT,
}

results = []


def check(name, command, expect, cwd=None):
    """Run `command`, assert every string in `expect` appears in its output."""
    proc = subprocess.run(command, capture_output=True, text=True, cwd=cwd)
    out = proc.stdout + proc.stderr
    missing = [e for e in expect if e not in out]
    ok = proc.returncode == 0 and not missing
    results.append((ok, name, missing, out, proc.returncode))
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        if proc.returncode != 0:
            print(f"      exit {proc.returncode}")
        for m in missing:
            print(f"      missing: {m!r}")
        tail = [ln for ln in out.splitlines() if ln.strip()][-12:]
        for ln in tail:
            print(f"      | {ln}")
    return out


def main():
    scripts = Path(sys.argv[1]) if len(sys.argv) > 1 else \
        HERE.parent.parent / "skills" / "scripts"
    if not (scripts / "a64.py").is_file():
        print(f"cannot find skills/scripts at {scripts}", file=sys.stderr)
        return 2
    a64 = [sys.executable, str(scripts / "a64.py")]

    work = Path(tempfile.mkdtemp(prefix="bedrock-selftest-"))
    so = work / "fake.so"
    subprocess.run([sys.executable, str(HERE / "make_fake_so.py"), str(so)],
                   check=True, capture_output=True)
    print(f"fixture: {so} ({so.stat().st_size} bytes)\n")

    patterns = work / "patterns.json"
    patterns.write_text(json.dumps(PATTERNS, indent=1))
    sigs_cpp = work / "Signatures.cpp"
    sigs_cpp.write_text(
        "namespace bedrocktools::memory {\n"
        "const std::array<SignatureDefinition, SignatureCount> definitions{{\n"
        + "".join(
            f'    SignatureDefinition{{SignatureId::{n}, "{p.replace("??", "? ?")}"}},\n'
            for n, p in PATTERNS.items())
        + "}};\n}\n")

    # ---- a64: the offline structural questions ----------------------------- #
    check("a64 func: address -> enclosing function",
          a64 + ["func", str(so), "--addr", hex(FUNC_DRIVER + 0x10)],
          [f"function start {FUNC_DRIVER:#x}"])

    check("a64 disas: real instruction stream",
          a64 + ["disas", str(so), "--addr", hex(FUNC_DRIVER), "--count", "10"],
          [f"bl         #{FUNC_FACE_UP:#x}", "tbz        w9, #0",
           f"{FUNC_DRIVER:#012x}"])

    check("a64 callers: direct BL callers, labelled by function",
          a64 + ["callers", str(so), "--addrs",
                 ",".join(hex(a) for a in (FUNC_FACE_UP, FUNC_FACE_DOWN,
                                           FUNC_FACE_NORTH))],
          ["1 direct caller(s)", f"func {FUNC_DRIVER:#x}"])

    check("a64 cluster: one driver calls the whole family",
          a64 + ["cluster", str(so), "--addrs",
                 ",".join(hex(a) for a in (FUNC_FACE_UP, FUNC_FACE_DOWN,
                                           FUNC_FACE_NORTH))],
          ["3/3 target(s)", f"func={FUNC_DRIVER:#x}"])

    out = check("a64 guards: per-face decision mask (bits 0/1/2)",
                a64 + ["guards", str(so), "--addrs",
                       ",".join(hex(a) for a in (FUNC_FACE_UP, FUNC_FACE_DOWN,
                                                 FUNC_FACE_NORTH))],
                ["tbz w9,0", "tbz w9,1", "tbz w9,2"])
    # The bit assigned to each face must be the one the fixture encoded: the
    # north emitter's call site is the last one, so only bit 2 may follow it.
    north_block = out.split(f"target {FUNC_FACE_NORTH:#012x}")[-1]
    if "tbz w9,1" in north_block:
        results.append((False, "a64 guards: face_north must not inherit bit 1",
                        ["no tbz w9,1 after the north call"], north_block, 0))
        print("FAIL  a64 guards: face_north must not inherit bit 1")

    check("a64 xref: ADRP+ADD reference to a .rodata string",
          a64 + ["xref", str(so), "--addrs", hex(STR_GAMEMODE_ATTACK)],
          ["1 xref(s)", "add x8"])

    # `ptr` reads literal qwords, so it finds the .data table and correctly
    # reports nothing for a relocated vtable slot (zero in the file). That
    # negative result is the whole reason vtable_walk.py exists.
    check("a64 ptr: literal pointer table found",
          a64 + ["ptr", str(so), "--addrs", hex(FUNC_FACE_UP)],
          [".data", f"{DATA_AT:#012x}"])

    # ---- signature tools --------------------------------------------------- #
    check("sigscan: UNIQUE / AMBIGUOUS / MISSING verdicts",
          [sys.executable, str(scripts / "sigscan.py"), str(so), str(patterns)],
          [f"AMBIGUOUS  FaceUp", "UNIQUE     FaceDown",
           "UNIQUE     FaceNorth", "MISSING    NoSuchFunction"])

    check("sigscan --strings: literal search with its address",
          [sys.executable, str(scripts / "sigscan.py"), str(so), "--strings",
           "GameMode|Tessellator"],
          [f"{STR_GAMEMODE_ATTACK:#012x}", "GameMode::attack",
           "BlockTessellator::_tessellateFaceUp"])

    check("verify_signatures: parses Signatures.cpp, reports vaddrs",
          [sys.executable, str(scripts / "verify_signatures.py"), str(so),
           "--sigs", str(sigs_cpp), "--json", str(work / "report.json")],
          ["ELF64 little-endian machine=0xb7", "parsed 4 signatures",
           f"vaddr={FUNC_FACE_DOWN:#x}", "summary: 2 unique, 1 ambiguous, 1 missing"])

    report = json.loads((work / "report.json").read_text())
    by_id = {r["id"]: r for r in report["signatures"]}
    ok = (by_id["FaceUp"]["matches"] == 2 and by_id["FaceDown"]["matches"] == 1
          and by_id["FaceNorth"]["matches"] == 1
          and by_id["FaceNorth"]["vaddr"] == FUNC_FACE_NORTH
          and by_id["NoSuchFunction"]["status"] == "MISSING")
    results.append((ok, "verify_signatures: JSON report contents", [], "", 0))
    print(f"{'PASS' if ok else 'FAIL'}  verify_signatures: JSON report contents")
    if not ok:
        print(f"      {by_id}")

    check("mksig: house-style pattern from a unique head is UNIQUE",
          [sys.executable, str(scripts / "mksig.py"), str(so),
           "--addr", hex(FUNC_FACE_DOWN), "--len", "4", "--name", "FaceDown"],
          ["matches=1", "E0 03 14 AA 00 00 02 8B", "C0 03 5F D6"])

    check("mksig: pattern from a duplicated head is visibly non-unique",
          [sys.executable, str(scripts / "mksig.py"), str(so),
           "--addr", hex(FUNC_FACE_UP), "--len", "4", "--name", "FaceUp"],
          ["matches=2"])

    check("multi_verify: per-build hit matrix",
          [sys.executable, str(scripts / "multi_verify.py"), "--sigs",
           str(patterns), "--so", f"{so},{so}"],
          ["unique in every build : 2", "ambiguous somewhere",
           "dead everywhere       : 1"])

    check("offset_check: struct displacements read back from the code",
          [sys.executable, str(scripts / "offset_check.py"), "--sigs",
           str(patterns), "--names", "FaceDown", "--so", str(so),
           "--offsets", ",".join(hex(o) for o in CAM_OFFSETS)],
          [hex(o) for o in CAM_OFFSETS])

    # ---- RTTI / vtable ----------------------------------------------------- #
    check("vtable_walk: 3-slot vtable recovered from .rela.dyn",
          [sys.executable, str(scripts / "vtable_walk.py"), str(so),
           "--classes", "10FaceUpBase"],
          [f"typeinfo @ {TYPEINFO_FACEUPBASE:#x} -> 1 vtable(s)",
           f"vtable @ {VTABLE_FACEUPBASE:#x}  3 slot(s)",
           f"[  0] {FUNC_FACE_UP:#x}",
           f"[  1] {FUNC_FACE_DOWN:#x}",
           f"[  2] {FUNC_FACE_NORTH:#x}"])

    check("vtable_walk: 2-slot vtable is flagged as a destructor pair",
          [sys.executable, str(scripts / "vtable_walk.py"), str(so),
           "--classes", "8DtorPair"],
          [f"typeinfo @ {TYPEINFO_DTORPAIR:#x}",
           f"vtable @ {VTABLE_DTORPAIR:#x}  2 slot(s)",
           "destructor pair", "NON-virtual"])

    # ---- ELF facts and packaging ------------------------------------------ #
    check("elf_facts: read-only ELF summary",
          [sys.executable, str(scripts / "elf_facts.py"), str(so),
           "--json", str(work / "elf_facts.json")],
          ["machine=0xb7", "e_type=3", ".text vaddr=0x1000", "report written:"])

    repo = HERE.parent.parent
    check("package_levipack: builds a .levipack from a .so + assets",
          [sys.executable, str(scripts / "package_levipack.py"),
           "--library", str(so),
           "--icon", str(repo / "skills/assets/hud-button-templates/ic_zoom_normal.png"),
           "--font", str(repo / "BedrockTools-main/resources/minecraft.ttf"),
           "--version-header",
           str(repo / "BedrockTools-main/include/bedrocktools/Version.hpp"),
           "--output", str(work / "Mod.levipack")],
          ["Mod.levipack"])

    if (work / "Mod.levipack").is_file():
        import zipfile
        names = zipfile.ZipFile(work / "Mod.levipack").namelist()
        ok = any(n.endswith(".so") for n in names)
        results.append((ok, "package_levipack: archive contains the .so", [],
                        str(names), 0))
        print(f"{'PASS' if ok else 'FAIL'}  package_levipack: archive contains the .so")
        if not ok:
            print(f"      {names}")

    shutil.rmtree(work, ignore_errors=True)

    passed = sum(1 for r in results if r[0])
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
