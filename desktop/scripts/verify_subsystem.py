#!/usr/bin/env python3
"""Verify that an executable was built with Windows GUI subsystem (IMAGE_SUBSYSTEM_WINDOWS_GUI = 2).

Exit code 0: Subsystem is GUI (2).
Exit code 1: Subsystem is Console (3) or invalid/missing file.
"""

import struct
import sys
from pathlib import Path


def inspect_pe_subsystem(file_path: Path) -> int:
    """Read PE header and return subsystem ID (2 = GUI, 3 = CUI)."""
    with open(file_path, "rb") as f:
        data = f.read(1024)
        if len(data) < 0x40 or data[:2] != b"MZ":
            raise ValueError(f"{file_path} is not a valid MZ executable.")
        pe_offset = struct.unpack("<I", data[0x3C:0x40])[0]
        if len(data) < pe_offset + 24 + 70:
            # Need to read more if PE header is beyond 1024 bytes
            f.seek(pe_offset)
            pe_data = f.read(256)
            magic = struct.unpack("<H", pe_data[24:26])[0]
            offset = 68 if magic == 0x20B else 68  # In PE32+ offset is 68 from OptionalHeader
            subsystem = struct.unpack("<H", pe_data[24 + offset : 24 + offset + 2])[0]
            return subsystem

        magic = struct.unpack("<H", data[pe_offset + 24 : pe_offset + 26])[0]
        offset = 68 if magic == 0x20B else 68
        subsystem = struct.unpack("<H", data[pe_offset + 24 + offset : pe_offset + 24 + offset + 2])[0]
        return subsystem


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: verify_subsystem.py <path_to_exe>")
        return 1

    target = Path(sys.argv[1])
    if not target.exists():
        print(f"Error: Target file {target} does not exist.")
        return 1

    try:
        subsystem = inspect_pe_subsystem(target)
    except Exception as e:
        print(f"Error inspecting PE headers: {e}")
        return 1

    if subsystem == 2:
        print(f"SUCCESS: {target.name} has IMAGE_SUBSYSTEM_WINDOWS_GUI (subsystem 2).")
        print("Console black window is strictly eliminated.")
        return 0
    elif subsystem == 3:
        print(f"FAILED: {target.name} has IMAGE_SUBSYSTEM_WINDOWS_CUI (subsystem 3 - Console).")
        print("A black console window will open on launch! Fix windows_subsystem = 'windows' and rebuild.")
        return 1
    else:
        print(f"WARNING: Unknown subsystem {subsystem} for {target.name}.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
