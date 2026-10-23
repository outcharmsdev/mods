#!/usr/bin/env python3
"""Build a synthetic AArch64 ELF .so so the skill's scripts can be smoke-tested.

There is no `libminecraftpe.so` in a clean sandbox, and shipping one is not an
option (it is hundreds of MB and it is not redistributable). Every script under
`skills/scripts/` only needs *structure* though -- `.text` with real ARM64
instructions, `.eh_frame_hdr` with a function table, `.rodata` strings, data
pointer slots, `.rela.dyn` relocations, program headers -- so a hand-built
30 KB ELF exercises the same code paths a real game library would.

The layout deliberately mirrors a real Bedrock Android image: image base 0,
**file offset == vaddr**, and the first PT_LOAD (R|X) starting at file offset 0
so it covers the ELF header. `verify_signatures.py` maps matches through the
PF_X segments, so a fixture whose executable segment started elsewhere would
exercise none of that.

What the fixture contains:

    0x1000  driver()        ADRP+ADD to a .rodata string, then three guarded
                            calls -- one BL per "face emitter", each followed
                            by a `tbz` on a different bit of w9 (bits 0/1/2)
    0x1100  face_up()       register-form prologue
    0x1140  face_down()     a *different* prologue (so its signature is unique)
    0x1180  face_north()    another distinct prologue
    0x1200  setup_camera()  load/store with struct displacements (offset_check)
    0x1300  face_up_copy()  byte-identical to face_up, on purpose, so a pattern
                            taken from face_up is AMBIGUOUS

    .rodata     RTTI name strings "10FaceUpBase", "8DtorPair" + two literals
                that `sigscan --strings` and `a64 xref` can find
    .data.rel.ro  vtable-shaped pointer slots: one 3-slot vtable for
                FaceUpBase and one 2-slot (destructor-pair) vtable for DtorPair,
                which is the case `vtable_walk.py` exists to explain. The slots
                are ZERO in the file.
    .data       a literal pointer table whose values *are* in the file, which is
                the shape `a64 ptr` scans for
    .rela.dyn   R_AARCH64_RELATIVE addends carrying every relocated pointer,
                because on a PIE image the slot itself is zero

Usage:
    python3 make_fake_so.py <out.so>
"""
import struct
import sys

# --------------------------------------------------------------------------- #
# AArch64 instruction encoders (little-endian, 32-bit words)
# --------------------------------------------------------------------------- #
NOP = 0xD503201F
RET = 0xD65F03C0
FMOV_S0_ONE = 0x1E2E1000          # fmov s0, #1.0


def bl(pc, target):
    """bl <target> from <pc>."""
    return 0x94000000 | (((target - pc) >> 2) & 0x03FFFFFF)


def adrp(rd, pc, target_page):
    """adrp x<rd>, <page containing target_page>."""
    imm = ((target_page - (pc & ~0xFFF)) >> 12) & 0x1FFFFF
    return 0x90000000 | ((imm & 3) << 29) | (((imm >> 2) & 0x7FFFF) << 5) | rd


def add_imm(rd, rn, imm12):
    """add x<rd>, x<rn>, #<imm12>."""
    return 0x91000000 | (imm12 << 10) | (rn << 5) | rd


def mov_reg(rd, rm):
    """mov x<rd>, x<rm>  (ORR Xd, XZR, Xm) -- no immediate, so it survives
    mksig's house-style wildcarding and gives the pattern something to anchor."""
    return 0xAA0003E0 | (rm << 16) | rd


def add_reg(rd, rn, rm):
    """add x<rd>, x<rn>, x<rm> (shifted register form)."""
    return 0x8B000000 | (rm << 16) | (rn << 5) | rd


def tbz(rt, bit, pc, target):
    """tbz w<rt>, #<bit>, <target> (bit < 32, so b5 = 0)."""
    assert bit < 32
    return 0x36000000 | ((bit & 0x1F) << 19) | (((target - pc) >> 2 & 0x3FFF) << 5) | rt


def ldr_imm(rd, rn, off):
    """ldr x<rd>, [x<rn>, #<off>] -- off must be a multiple of 8."""
    assert off % 8 == 0
    return 0xF9400000 | ((off // 8) << 10) | (rn << 5) | rd


def str_imm(rd, rn, off):
    """str x<rd>, [x<rn>, #<off>] -- off must be a multiple of 8."""
    assert off % 8 == 0
    return 0xF9000000 | ((off // 8) << 10) | (rn << 5) | rd


def words(code, base):
    """[(vaddr, word)] for a list of instructions placed at base."""
    return [(base + i * 4, w) for i, w in enumerate(code)]


# --------------------------------------------------------------------------- #
# Section layout (offset == vaddr everywhere)
# --------------------------------------------------------------------------- #
TEXT_AT = 0x1000
RODATA_AT = 0x2000
DATARELRO_AT = 0x3000             # relocated: slots are ZERO in the file
DATA_AT = 0x3400                  # literal pointers, no relocation needed
EHFRAME_AT = 0x4000
EHFRAMEHDR_AT = 0x4100
RELA_AT = 0x5000
DYNSYM_AT = 0x5200                # .rela.dyn is 16*24 = 0x180 bytes; keep clear
DYNSTR_AT = 0x5300
SHSTRTAB_AT = 0x6000
SHDR_AT = 0x7000
TEXT_SEGMENT_SIZE = 0x1400        # PT_LOAD(R|X) spans 0 .. 0x1400

# Addresses the tests will ask about.
FUNC_DRIVER = 0x1000
FUNC_FACE_UP = 0x1100
FUNC_FACE_DOWN = 0x1140
FUNC_FACE_NORTH = 0x1180
FUNC_SETUP_CAMERA = 0x1200
FUNC_FACE_UP_COPY = 0x1300

STR_GAMEMODE_ATTACK = RODATA_AT + 0x00
STR_TESSELLATE_UP = RODATA_AT + 0x20
STR_RTTI_FACEUPBASE = RODATA_AT + 0x60     # "10FaceUpBase"
STR_RTTI_DTORPAIR = RODATA_AT + 0x80       # "8DtorPair"

# Struct displacements setup_camera() touches; offset_check.py looks for these.
CAM_OFFSETS = (0x620, 0xEB8, 0xF38)

# RTTI chain, all in .data.rel.ro.
TYPEINFO_FACEUPBASE = 0x3100
TYPEINFO_DTORPAIR = 0x3140
VTABLE_FACEUPBASE = 0x3210       # first virtual-slot address
VTABLE_DTORPAIR = 0x3290

R_AARCH64_RELATIVE = 1027


def build_text():
    """Return {vaddr: word} for .text."""
    out = {}
    jump_past_guards = 0x1020

    driver = [
        adrp(8, FUNC_DRIVER, STR_GAMEMODE_ATTACK),          # 0x1000
        add_imm(8, 8, STR_GAMEMODE_ATTACK & 0xFFF),         # 0x1004
        bl(0x1008, FUNC_FACE_UP),                           # 0x1008
        tbz(9, 0, 0x100C, jump_past_guards),                # 0x100c
        bl(0x1010, FUNC_FACE_DOWN),                         # 0x1010
        tbz(9, 1, 0x1014, jump_past_guards),                # 0x1014
        bl(0x1018, FUNC_FACE_NORTH),                        # 0x1018
        tbz(9, 2, 0x101C, jump_past_guards),                # 0x101c
        NOP,                                                # 0x1020
        RET,                                                # 0x1024
    ]
    out.update(words(driver, FUNC_DRIVER))

    # Register-form prologues: `mksig` wildcards immediate/memory operands, so
    # these stay fixed bytes and the generated pattern can be unique.
    face_up = [mov_reg(0, 19), add_reg(0, 0, 1), FMOV_S0_ONE, RET]
    out.update(words(face_up, FUNC_FACE_UP))
    out.update(words(face_up, FUNC_FACE_UP_COPY))   # deliberate duplicate

    out.update(words([mov_reg(0, 20), add_reg(0, 0, 2), FMOV_S0_ONE, RET],
                     FUNC_FACE_DOWN))
    out.update(words([mov_reg(0, 21), add_reg(0, 0, 3), FMOV_S0_ONE, RET],
                     FUNC_FACE_NORTH))

    setup_camera = [
        ldr_imm(1, 0, CAM_OFFSETS[0]),
        ldr_imm(2, 0, CAM_OFFSETS[1]),
        str_imm(3, 0, CAM_OFFSETS[2]),
        RET,
    ]
    out.update(words(setup_camera, FUNC_SETUP_CAMERA))
    return out


def build_rodata():
    strings = {
        STR_GAMEMODE_ATTACK: b"GameMode::attack\x00",
        STR_TESSELLATE_UP: b"BlockTessellator::_tessellateFaceUp\x00",
        STR_RTTI_FACEUPBASE: b"10FaceUpBase\x00",
        STR_RTTI_DTORPAIR: b"8DtorPair\x00",
    }
    blob = bytearray(0x100)
    for addr, raw in strings.items():
        blob[addr - RODATA_AT:addr - RODATA_AT + len(raw)] = raw
    return bytes(blob)


def relocations():
    """(slot, addend) pairs, as they would appear in .rela.dyn."""
    return [
        # typeinfo structs: [ti+0] = &__class_type_info vtable, [ti+8] = &name
        (TYPEINFO_FACEUPBASE + 0, 0x3080),
        (TYPEINFO_FACEUPBASE + 8, STR_RTTI_FACEUPBASE),
        (TYPEINFO_DTORPAIR + 0, 0x3080),
        (TYPEINFO_DTORPAIR + 8, STR_RTTI_DTORPAIR),
        # vtable headers: [vt-16] = offset-to-top, [vt-8] = &typeinfo
        (VTABLE_FACEUPBASE - 16, 0),
        (VTABLE_FACEUPBASE - 8, TYPEINFO_FACEUPBASE),
        (VTABLE_DTORPAIR - 16, 0),
        (VTABLE_DTORPAIR - 8, TYPEINFO_DTORPAIR),
        # FaceUpBase: three virtual slots
        (VTABLE_FACEUPBASE + 0, FUNC_FACE_UP),
        (VTABLE_FACEUPBASE + 8, FUNC_FACE_DOWN),
        (VTABLE_FACEUPBASE + 16, FUNC_FACE_NORTH),
        # DtorPair: only a destructor pair, so the tool must report "non-virtual"
        (VTABLE_DTORPAIR + 0, FUNC_FACE_UP),
        (VTABLE_DTORPAIR + 8, FUNC_FACE_DOWN),
    ]


def build_data():
    """A literal pointer table with no relocation behind it.

    This is the shape `a64 ptr` is built for: 8-byte values that are already
    present in the file, so a raw qword scan finds them. The vtable slots in
    `.data.rel.ro` are the opposite case -- zero in the file, value in the
    `.rela.dyn` addend -- which is exactly why `vtable_walk.py` exists.
    """
    return b"".join(struct.pack("<Q", a) for a in
                    (FUNC_FACE_UP, FUNC_FACE_DOWN, FUNC_FACE_NORTH, 0))


def build_data_rel_ro():
    """Zero-filled slots: on a PIE image the values live in .rela.dyn addends."""
    hi = max(slot for slot, _ in relocations()) + 8
    return bytes(hi - DATARELRO_AT)


def build_rela():
    return b"".join(
        struct.pack("<QQq", slot, R_AARCH64_RELATIVE, addend)
        for slot, addend in relocations()
    )


def build_eh_frame_hdr():
    """A .eh_frame_hdr whose binary-search table lists every function start."""
    starts = (FUNC_DRIVER, FUNC_FACE_UP, FUNC_FACE_DOWN, FUNC_FACE_NORTH,
              FUNC_SETUP_CAMERA, FUNC_FACE_UP_COPY)
    body = bytearray()
    body.append(1)          # version
    body.append(0x1B)       # eh_frame_ptr: DW_EH_PE_pcrel|sdata4
    body.append(0x03)       # fde_count:   DW_EH_PE_absptr|udata4
    body.append(0x3B)       # table:       DW_EH_PE_datarel|sdata4
    body += struct.pack("<i", 0)             # eh_frame_ptr (value unused)
    body += struct.pack("<I", len(starts))
    for i, start in enumerate(starts):
        entry_off = 12 + i * 8
        body += struct.pack("<i", start - EHFRAMEHDR_AT)          # loc, datarel
        body += struct.pack("<i", -(entry_off + 4))               # fde, unused
    blob = bytearray(0x100)
    blob[:len(body)] = body
    return bytes(blob)


def build_shstrtab(names):
    """Return (blob, {name: offset})."""
    blob = bytearray(b"\x00")
    offs = {"": 0}
    for n in names:
        if n in offs:
            continue
        offs[n] = len(blob)
        blob += n.encode() + b"\x00"
    return bytes(blob), offs


SH_TYPE_PROGBITS = 1
SH_TYPE_STRTAB = 3
SH_TYPE_RELA = 4
SH_TYPE_DYNSYM = 11


def write_elf(path):
    text = build_text()
    last = max(text)
    text_size = ((last - TEXT_AT) // 4 + 1) * 4
    text_blob = bytearray(text_size)
    for addr, word in text.items():
        struct.pack_into("<I", text_blob, addr - TEXT_AT, word)
    text_blob = bytes(text_blob)

    rodata = build_rodata()
    datarelro = build_data_rel_ro()
    data = build_data()
    rela = build_rela()
    ehframe = b"\x00" * 0x100
    ehframehdr = build_eh_frame_hdr()
    dynsym = b"\x00" * 0x18        # one NULL symbol entry
    dynstr = b"\x00"

    sections = [
        # (name, type, addr, blob)
        (".text", SH_TYPE_PROGBITS, TEXT_AT, text_blob),
        (".rodata", SH_TYPE_PROGBITS, RODATA_AT, rodata),
        (".data.rel.ro", SH_TYPE_PROGBITS, DATARELRO_AT, datarelro),
        (".data", SH_TYPE_PROGBITS, DATA_AT, data),
        (".eh_frame", SH_TYPE_PROGBITS, EHFRAME_AT, ehframe),
        (".eh_frame_hdr", SH_TYPE_PROGBITS, EHFRAMEHDR_AT, ehframehdr),
        (".rela.dyn", SH_TYPE_RELA, RELA_AT, rela),
        (".dynsym", SH_TYPE_DYNSYM, DYNSYM_AT, dynsym),
        (".dynstr", SH_TYPE_STRTAB, DYNSTR_AT, dynstr),
    ]

    # offset == vaddr here, so any overlap silently corrupts the earlier
    # section -- the failure that produced a vtable_walk slot count of 0.
    ordered = sorted(((addr, addr + len(blob), name)
                      for name, _t, addr, blob in sections))
    for (a0, a1, n0), (b0, _b1, n1) in zip(ordered, ordered[1:]):
        assert a1 <= b0, f"sections {n0} and {n1} overlap at {a1:#x}"

    shstrtab, shstr_off = build_shstrtab([s[0] for s in sections] + [".shstrtab"])
    shstrtab_addr = SHSTRTAB_AT

    image = bytearray(SHDR_AT + len(shstrtab))
    for name, _t, addr, blob in sections:
        image[addr:addr + len(blob)] = blob
    image[shstrtab_addr:shstrtab_addr + len(shstrtab)] = shstrtab

    # ---- ELF64 header ----
    e_phoff = 0x40
    e_phnum = 2
    # Section header table = reserved NULL entry + the sections + .shstrtab.
    shnum = len(sections) + 2
    e_shstrndx = shnum - 1             # .shstrtab is written last
    e_shoff = SHDR_AT + len(shstrtab)
    image[0:64] = struct.pack(
        "<4sBBBBB7xHHIQQQIHHHHHH",
        b"\x7fELF", 2, 1, 1, 0, 0,     # ELF64, little-endian, ELF v1, SysV
        3,                              # e_type = ET_DYN
        0xB7,                           # e_machine = EM_AARCH64
        1,                              # e_version
        0,                              # e_entry
        e_phoff,                        # e_phoff
        e_shoff,                        # e_shoff
        0,                              # e_flags
        64,                             # e_ehsize
        56,                             # e_phentsize
        e_phnum,                        # e_phnum
        64,                             # e_shentsize
        shnum,                          # e_shnum
        e_shstrndx,                     # e_shstrndx
    )

    # ---- Program headers ----
    # Segment 0 starts at file offset 0 and covers the ELF header, exactly like
    # a real Android .so; verify_signatures.py builds its byte image from the
    # union of PF_X PT_LOADs and maps match offsets back through them.
    rw_end = max(DATARELRO_AT + len(datarelro), DATA_AT + len(data))
    rw_size = rw_end - RODATA_AT
    image[e_phoff:e_phoff + 56] = struct.pack(
        "<IIQQQQQQ",
        1, 5,                           # PT_LOAD, PF_R|PF_X
        0, 0, 0,
        TEXT_SEGMENT_SIZE, TEXT_SEGMENT_SIZE, 0x1000,
    )
    image[e_phoff + 56:e_phoff + 112] = struct.pack(
        "<IIQQQQQQ",
        1, 6,                           # PT_LOAD, PF_R|PF_W
        RODATA_AT, RODATA_AT, RODATA_AT,
        rw_size, rw_size, 0x1000,
    )

    # ---- Section headers (index 0 is the reserved NULL entry) ----
    def shdr(name_off, stype, flags, addr, off, size, entsize=0, align=1):
        return struct.pack("<IIQQQQIIQQ", name_off, stype, flags, addr, off,
                           size, 0, 0, align, entsize)

    flags_of = {".text": 0x6, ".rodata": 0x2, ".data.rel.ro": 0x3, ".data": 0x3,
                ".eh_frame": 0x2, ".eh_frame_hdr": 0x2, ".rela.dyn": 0x3,
                ".dynsym": 0x2, ".dynstr": 0x2}
    sh = bytearray(shdr(0, 0, 0, 0, 0, 0))
    for name, stype, addr, blob in sections:
        sh += shdr(shstr_off[name], stype, flags_of[name], addr, addr, len(blob))
    sh += shdr(shstr_off[".shstrtab"], SH_TYPE_STRTAB, 0, shstrtab_addr,
               shstrtab_addr, len(shstrtab))
    assert len(sh) == shnum * 64, (len(sh), shnum * 64)
    image[e_shoff:e_shoff + len(sh)] = sh

    with open(path, "wb") as fh:
        fh.write(bytes(image))
    return path


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "fake.so"
    print("wrote", write_elf(out))
    print(f"  driver        = {FUNC_DRIVER:#x}")
    print(f"  face_up       = {FUNC_FACE_UP:#x}  (duplicate at {FUNC_FACE_UP_COPY:#x})")
    print(f"  face_down     = {FUNC_FACE_DOWN:#x}")
    print(f"  face_north    = {FUNC_FACE_NORTH:#x}")
    print(f"  setup_camera  = {FUNC_SETUP_CAMERA:#x}  offsets "
          + ",".join(hex(o) for o in CAM_OFFSETS))
    print(f"  string        = {STR_GAMEMODE_ATTACK:#x}")
    print(f"  ptr table     = {DATA_AT:#x} (.data, literal qwords)")
    print(f"  FaceUpBase    typeinfo={TYPEINFO_FACEUPBASE:#x} "
          f"vtable={VTABLE_FACEUPBASE:#x} (3 slots)")
    print(f"  DtorPair      typeinfo={TYPEINFO_DTORPAIR:#x} "
          f"vtable={VTABLE_DTORPAIR:#x} (2 slots)")
