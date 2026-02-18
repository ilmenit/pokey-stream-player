"""6502 instruction encoder.

Parses operand strings, detects addressing modes, and emits machine code.
"""

import re

from .opcodes import OPCODES, BRANCHES, SHIFT_OPS
from .expressions import evaluate, ExprError


class EncodeError(Exception):
    pass


def parse_operand(operand, mnemonic):
    """Parse operand string → (mode, expression_string).

    Mode may be a provisional mode like 'zp_or_abs' resolved later.
    """
    s = operand.strip()

    if not s:
        if mnemonic in SHIFT_OPS:
            return 'acc', ''
        return 'imp', ''

    if s.lower() == 'a' and mnemonic in SHIFT_OPS:
        return 'acc', ''

    # Immediate: #expr
    if s.startswith('#'):
        return 'imm', s[1:].strip()

    # Indirect modes
    if s.startswith('('):
        inner = s[1:]
        # (expr),Y
        m = re.match(r'^(.+)\)\s*,\s*[yY]\s*$', inner)
        if m:
            return 'izy', m.group(1).strip()
        # (expr,X)
        m = re.match(r'^(.+),\s*[xX]\s*\)\s*$', inner)
        if m:
            return 'izx', m.group(1).strip()
        # (expr) — indirect JMP
        m = re.match(r'^(.+)\)\s*$', inner)
        if m:
            return 'ind', m.group(1).strip()

    # Indexed: expr,X or expr,Y
    m = re.match(r'^(.+),\s*([xXyY])\s*$', s)
    if m:
        idx = m.group(2).lower()
        return ('abx_or_zpx' if idx == 'x' else 'aby_or_zpy'), m.group(1).strip()

    # Branches → relative
    if mnemonic in BRANCHES:
        return 'rel', s

    # Plain expression → ZP or ABS (decided at resolution time)
    return 'zp_or_abs', s


def estimate_size(mnemonic, operand):
    """Estimate instruction size when value is unknown (forward ref)."""
    mode, _ = parse_operand(operand, mnemonic)
    if mode in ('imp', 'acc'):
        return 1
    if mode in ('imm', 'rel', 'izy', 'izx'):
        return 2
    # For zp_or_abs, abx_or_zpx, aby_or_zpy: assume 3 (absolute).
    # This is conservative — if it turns out to be ZP, the next pass
    # will shrink it and addresses will converge.
    return 3


def encode(mnemonic, operand, symbols, pc):
    """Encode one 6502 instruction.

    Returns:
        bytes object (1-3 bytes of machine code).

    Raises:
        ExprError if a symbol can't be resolved.
        EncodeError for invalid mode/mnemonic combos.
    """
    mode, expr_str = parse_operand(operand, mnemonic)

    # No operand
    if mode in ('imp', 'acc'):
        opcode = OPCODES.get((mnemonic, mode))
        if opcode is None:
            raise EncodeError(f"Invalid mode '{mode}' for {mnemonic}")
        return bytes([opcode])

    # Evaluate the operand expression
    value = evaluate(expr_str, symbols, pc)

    # ── Relative (branches) ──
    if mode == 'rel':
        opcode = OPCODES[(mnemonic, 'rel')]
        offset = value - (pc + 2)
        if not (-128 <= offset <= 127):
            raise EncodeError(
                f"Branch out of range: {mnemonic} "
                f"(offset {offset:+d}, PC=${pc:04X} target=${value:04X})")
        return bytes([opcode, offset & 0xFF])

    # ── Immediate ──
    if mode == 'imm':
        opcode = OPCODES.get((mnemonic, 'imm'))
        if opcode is None:
            raise EncodeError(f"No immediate mode for {mnemonic}")
        return bytes([opcode, value & 0xFF])

    # ── Indirect Y: (zp),Y ──
    if mode == 'izy':
        opcode = OPCODES.get((mnemonic, 'izy'))
        if opcode is None:
            raise EncodeError(f"No (indirect),Y mode for {mnemonic}")
        return bytes([opcode, value & 0xFF])

    # ── Indirect X: (zp,X) ──
    if mode == 'izx':
        opcode = OPCODES.get((mnemonic, 'izx'))
        if opcode is None:
            raise EncodeError(f"No (indirect,X) mode for {mnemonic}")
        return bytes([opcode, value & 0xFF])

    # ── Indirect: (abs) — JMP only ──
    if mode == 'ind':
        opcode = OPCODES.get((mnemonic, 'ind'))
        if opcode is None:
            raise EncodeError(f"No indirect mode for {mnemonic}")
        return bytes([opcode, value & 0xFF, (value >> 8) & 0xFF])

    # ── ZP vs Absolute decision ──
    is_zp = 0 <= (value & 0xFFFF) <= 0xFF

    if mode == 'zp_or_abs':
        if is_zp and (mnemonic, 'zp') in OPCODES:
            return bytes([OPCODES[(mnemonic, 'zp')], value & 0xFF])
        opcode = OPCODES.get((mnemonic, 'abs'))
        if opcode is None:
            raise EncodeError(f"No absolute mode for {mnemonic}")
        return bytes([opcode, value & 0xFF, (value >> 8) & 0xFF])

    if mode == 'abx_or_zpx':
        if is_zp and (mnemonic, 'zpx') in OPCODES:
            return bytes([OPCODES[(mnemonic, 'zpx')], value & 0xFF])
        opcode = OPCODES.get((mnemonic, 'abx'))
        if opcode is None:
            raise EncodeError(f"No absolute,X mode for {mnemonic}")
        return bytes([opcode, value & 0xFF, (value >> 8) & 0xFF])

    if mode == 'aby_or_zpy':
        if is_zp and (mnemonic, 'zpy') in OPCODES:
            return bytes([OPCODES[(mnemonic, 'zpy')], value & 0xFF])
        opcode = OPCODES.get((mnemonic, 'aby'))
        if opcode is None:
            raise EncodeError(f"No absolute,Y mode for {mnemonic}")
        return bytes([opcode, value & 0xFF, (value >> 8) & 0xFF])

    raise EncodeError(f"Cannot encode: {mnemonic} {operand}")
