"""Simple MADS-compatible 6502 assembler for the Stream Player project.

Implements the MADS subset needed to assemble the player project:
  - Full 6502 instruction set
  - org, .byte, .word, icl, ini directives
  - .if / .elseif / .else / .endif conditional assembly
  - .error compile-time assertion
  - SYMBOL = expression equates
  - Global labels, @local labels, @anonymous labels (@+ / @:)
  - < > (lo/hi byte), comparison operators in .if
  - Atari XEX (DOS binary) output

Usage as library:
    from stream_player.simple_mads import assemble
    xex_bytes = assemble('stream_player.asm')

Usage from command line:
    python -m stream_player.simple_mads stream_player.asm -o output.xex
"""

import os
import sys

from .assembler import Assembler, AsmError
from .expressions import ExprError


def assemble(filename, search_dirs=None):
    """Assemble a MADS-compatible source file to XEX binary.

    Args:
        filename: Path to main .asm file.
        search_dirs: Extra directories to search for icl files.

    Returns:
        bytes: Atari XEX binary data.

    Raises:
        AsmError: On assembly errors.
    """
    asm = Assembler(filename, search_dirs)
    return asm.assemble()


def main(argv=None):
    """CLI entry point."""
    import argparse
    parser = argparse.ArgumentParser(
        description='Simple MADS-compatible 6502 assembler')
    parser.add_argument('input', help='Source .asm file')
    parser.add_argument('-o', '--output', default=None,
                        help='Output .xex file')
    parser.add_argument('-l', '--listing', default=None,
                        help='Output listing file (ignored, for MADS compat)')
    args = parser.parse_args(argv)

    # Handle MADS-style -o:filename
    out_path = args.output
    if out_path and out_path.startswith(':'):
        out_path = out_path[1:]
    if not out_path:
        out_path = os.path.splitext(args.input)[0] + '.xex'

    try:
        xex = assemble(args.input)
        with open(out_path, 'wb') as f:
            f.write(xex)
        print(f"Assembled: {out_path} ({len(xex)} bytes)")
        return 0
    except AsmError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
