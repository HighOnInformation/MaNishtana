"""Build a rebuild/change fixture from binaries you already have.

Usage:
    python make_fixture.py OUT_DIR --pe OLD.exe [NEW.exe] --elf OLD [NEW]

Writes OUT_DIR/v1 and OUT_DIR/v2 plus OUT_DIR/config.json:

    rebuilt.exe / rebuilt   the old binary vs itself with its link stamp (PE) or
                            build-id (ELF) rewritten: expect "noise only"
    patched.exe             the re-stamped PE with one code byte flipped: expect
                            "modified" and one differing region
    changed.exe / changed   old vs new, when a NEW was given: expect "modified"

The re-stamping is independent of manishtana.binnoise on purpose. It finds the
fields by raw byte layout, so the check does not reduce to the parser testing
itself. Standard library only, like the tool.
"""

import argparse
import os
import shutil
import struct


def restamp_pe(data):
    buf = bytearray(data)
    pe = struct.unpack_from("<I", buf, 0x3C)[0]
    if buf[pe:pe + 4] != b"PE\x00\x00":
        raise SystemExit("not a PE file")
    struct.pack_into("<I", buf, pe + 8, 0x6543210F)          # COFF TimeDateStamp
    struct.pack_into("<I", buf, pe + 24 + 64, 0x00ABCDEF)    # optional header CheckSum
    return buf, pe


def first_code_offset(buf, pe):
    """File offset 0x100 bytes into the first executable section."""
    count = struct.unpack_from("<H", buf, pe + 6)[0]
    table = pe + 24 + struct.unpack_from("<H", buf, pe + 20)[0]
    for index in range(count):
        at = table + index * 40
        raw_size, raw_off = struct.unpack_from("<II", buf, at + 16)
        flags = struct.unpack_from("<I", buf, at + 36)[0]
        if flags & 0x20000000 and raw_size > 0x200:               # IMAGE_SCN_MEM_EXECUTE
            return raw_off + 0x100
    raise SystemExit("no executable section found")


def restamp_elf(data):
    buf = bytearray(data)
    for little in (True, False):
        end = "<" if little else ">"
        marker = struct.pack(end + "III", 4, 20, 3) + b"GNU\x00"
        at = buf.find(marker)
        if at > 0:
            desc = at + len(marker)
            buf[desc:desc + 20] = bytes(range(20))
            return buf
    raise SystemExit("no 20-byte GNU build-id note found")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("out")
    parser.add_argument("--pe", nargs="+", metavar="FILE")
    parser.add_argument("--elf", nargs="+", metavar="FILE")
    args = parser.parse_args()
    v1, v2 = os.path.join(args.out, "v1"), os.path.join(args.out, "v2")
    for path in (v1, v2):
        os.makedirs(path, exist_ok=True)
    names = []

    if args.pe:
        old = open(args.pe[0], "rb").read()
        stamped, pe = restamp_pe(old)
        open(os.path.join(v1, "rebuilt.exe"), "wb").write(old)
        open(os.path.join(v2, "rebuilt.exe"), "wb").write(stamped)
        patched = bytearray(stamped)
        offset = first_code_offset(patched, pe)
        patched[offset] ^= 0xFF
        open(os.path.join(v1, "patched.exe"), "wb").write(old)
        open(os.path.join(v2, "patched.exe"), "wb").write(patched)
        print("patched.exe: flipped the byte at file offset 0x%X" % offset)
        names += ["rebuilt.exe", "patched.exe"]
        if len(args.pe) > 1:
            shutil.copy(args.pe[0], os.path.join(v1, "changed.exe"))
            shutil.copy(args.pe[1], os.path.join(v2, "changed.exe"))
            names.append("changed.exe")

    if args.elf:
        old = open(args.elf[0], "rb").read()
        open(os.path.join(v1, "rebuilt"), "wb").write(old)
        open(os.path.join(v2, "rebuilt"), "wb").write(restamp_elf(old))
        names.append("rebuilt")
        if len(args.elf) > 1:
            shutil.copy(args.elf[0], os.path.join(v1, "changed"))
            shutil.copy(args.elf[1], os.path.join(v2, "changed"))
            names.append("changed")

    with open(os.path.join(args.out, "config.json"), "w") as handle:
        handle.write('{\n  "rules": [{"match": %s, "normalize": ["binary-stamps"]}]\n}\n'
                     % str(names).replace("'", '"'))
    print("fixture written to %s: %s" % (args.out, ", ".join(names)))
    print("expect noise only: rebuilt*; modified: patched.exe, changed*")


if __name__ == "__main__":
    main()
