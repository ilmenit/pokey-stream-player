"""Minimal 6502 assembler for stream-player code generation.

Supports the instruction subset needed by our player routines.
Handles labels, forward references, and two-pass assembly.
"""


class Asm6502:
    """Two-pass 6502 assembler."""

    def __init__(self, org: int = 0x2000):
        self._org = org
        self._pc = org
        self._output = bytearray()
        self._labels = {}
        self._fixups = []  # (output_offset, label, is_relative, is_word)
        self._pass = 1

    @property
    def pc(self) -> int:
        return self._pc

    @property
    def origin(self) -> int:
        return self._org

    def label(self, name: str):
        """Define a label at the current PC."""
        self._labels[name] = self._pc

    def org(self, addr: int):
        """Set new origin (only at start or for separate segments)."""
        self._org = addr
        self._pc = addr
        self._output = bytearray()
        self._labels = {}
        self._fixups = []

    # ── Data directives ──

    def byte(self, *values):
        """Emit raw bytes."""
        for v in values:
            if isinstance(v, (list, tuple, bytes, bytearray)):
                for b in v:
                    self._emit(b & 0xFF)
            else:
                self._emit(v & 0xFF)

    def word(self, value):
        """Emit 16-bit little-endian word."""
        if isinstance(value, str):
            self._emit(0x00)
            self._emit(0x00)
            off = len(self._output) - 2
            self._fixups.append((off, value, False, True))
        else:
            self._emit(value & 0xFF)
            self._emit((value >> 8) & 0xFF)

    def fill(self, count: int, value: int = 0):
        """Emit count bytes of a value."""
        for _ in range(count):
            self._emit(value & 0xFF)

    # ── Implied / Accumulator instructions ──

    def _implied(self, opcode):
        self._emit(opcode)

    def nop(self): self._implied(0xEA)
    def clc(self): self._implied(0x18)
    def sec(self): self._implied(0x38)
    def cli(self): self._implied(0x58)
    def sei(self): self._implied(0x78)
    def cld(self): self._implied(0xD8)
    def pha(self): self._implied(0x48)
    def pla(self): self._implied(0x68)
    def tax(self): self._implied(0xAA)
    def tay(self): self._implied(0xA8)
    def txa(self): self._implied(0x8A)
    def tya(self): self._implied(0x98)
    def txs(self): self._implied(0x9A)
    def tsx(self): self._implied(0xBA)
    def inx(self): self._implied(0xE8)
    def iny(self): self._implied(0xC8)
    def dex(self): self._implied(0xCA)
    def dey(self): self._implied(0x88)
    def rts(self): self._implied(0x60)
    def rti(self): self._implied(0x40)
    def asl_a(self): self._implied(0x0A)
    def lsr_a(self): self._implied(0x4A)
    def rol_a(self): self._implied(0x2A)
    def ror_a(self): self._implied(0x6A)

    # ── Immediate instructions ──

    def lda_imm(self, v): self._emit(0xA9); self._emit(v & 0xFF)
    def ldx_imm(self, v): self._emit(0xA2); self._emit(v & 0xFF)
    def ldy_imm(self, v): self._emit(0xA0); self._emit(v & 0xFF)
    def cmp_imm(self, v): self._emit(0xC9); self._emit(v & 0xFF)
    def cpx_imm(self, v): self._emit(0xE0); self._emit(v & 0xFF)
    def cpy_imm(self, v): self._emit(0xC0); self._emit(v & 0xFF)
    def adc_imm(self, v): self._emit(0x69); self._emit(v & 0xFF)
    def sbc_imm(self, v): self._emit(0xE9); self._emit(v & 0xFF)
    def and_imm(self, v): self._emit(0x29); self._emit(v & 0xFF)
    def ora_imm(self, v): self._emit(0x09); self._emit(v & 0xFF)
    def eor_imm(self, v): self._emit(0x49); self._emit(v & 0xFF)

    # ── Zero page instructions ──

    def lda_zp(self, addr):  self._emit(0xA5); self._emit(addr & 0xFF)
    def ldx_zp(self, addr):  self._emit(0xA6); self._emit(addr & 0xFF)
    def ldy_zp(self, addr):  self._emit(0xA4); self._emit(addr & 0xFF)
    def sta_zp(self, addr):  self._emit(0x85); self._emit(addr & 0xFF)
    def stx_zp(self, addr):  self._emit(0x86); self._emit(addr & 0xFF)
    def sty_zp(self, addr):  self._emit(0x84); self._emit(addr & 0xFF)
    def inc_zp(self, addr):  self._emit(0xE6); self._emit(addr & 0xFF)
    def dec_zp(self, addr):  self._emit(0xC6); self._emit(addr & 0xFF)
    def cmp_zp(self, addr):  self._emit(0xC5); self._emit(addr & 0xFF)
    def adc_zp(self, addr):  self._emit(0x65); self._emit(addr & 0xFF)
    def sbc_zp(self, addr):  self._emit(0xE5); self._emit(addr & 0xFF)
    def and_zp(self, addr):  self._emit(0x25); self._emit(addr & 0xFF)
    def ora_zp(self, addr):  self._emit(0x05); self._emit(addr & 0xFF)
    def asl_zp(self, addr):  self._emit(0x06); self._emit(addr & 0xFF)
    def lsr_zp(self, addr):  self._emit(0x46); self._emit(addr & 0xFF)
    def bit_zp(self, addr):  self._emit(0x24); self._emit(addr & 0xFF)

    # ── Zero page, X ──

    def lda_zpx(self, addr): self._emit(0xB5); self._emit(addr & 0xFF)
    def sta_zpx(self, addr): self._emit(0x95); self._emit(addr & 0xFF)

    # ── Absolute instructions ──

    def lda_abs(self, addr): self._emit(0xAD); self._emit16_or_label(addr)
    def ldx_abs(self, addr): self._emit(0xAE); self._emit16_or_label(addr)
    def ldy_abs(self, addr): self._emit(0xAC); self._emit16_or_label(addr)
    def sta_abs(self, addr): self._emit(0x8D); self._emit16_or_label(addr)
    def stx_abs(self, addr): self._emit(0x8E); self._emit16_or_label(addr)
    def sty_abs(self, addr): self._emit(0x8C); self._emit16_or_label(addr)
    def inc_abs(self, addr): self._emit(0xEE); self._emit16_or_label(addr)
    def dec_abs(self, addr): self._emit(0xCE); self._emit16_or_label(addr)
    def cmp_abs(self, addr): self._emit(0xCD); self._emit16_or_label(addr)
    def adc_abs(self, addr): self._emit(0x6D); self._emit16_or_label(addr)
    def sbc_abs(self, addr): self._emit(0xED); self._emit16_or_label(addr)
    def and_abs(self, addr): self._emit(0x2D); self._emit16_or_label(addr)
    def ora_abs(self, addr): self._emit(0x0D); self._emit16_or_label(addr)
    def bit_abs(self, addr): self._emit(0x2C); self._emit16_or_label(addr)

    # ── Absolute, X ──

    def lda_absx(self, addr): self._emit(0xBD); self._emit16_or_label(addr)
    def sta_absx(self, addr): self._emit(0x9D); self._emit16_or_label(addr)

    # ── Absolute, Y ──

    def lda_absy(self, addr): self._emit(0xB9); self._emit16_or_label(addr)
    def ldx_absy(self, addr): self._emit(0xBE); self._emit16_or_label(addr)
    def sta_absy(self, addr): self._emit(0x99); self._emit16_or_label(addr)

    # ── Indirect indexed ──

    def lda_indy(self, zp): self._emit(0xB1); self._emit(zp & 0xFF)
    def sta_indy(self, zp): self._emit(0x91); self._emit(zp & 0xFF)

    # ── JMP / JSR ──

    def jmp(self, addr_or_label):
        self._emit(0x4C)
        if isinstance(addr_or_label, str):
            off = len(self._output)
            self._emit(0x00)
            self._emit(0x00)
            self._fixups.append((off, addr_or_label, False, True))
        else:
            self._emit16(addr_or_label)

    def jsr(self, addr_or_label):
        self._emit(0x20)
        if isinstance(addr_or_label, str):
            off = len(self._output)
            self._emit(0x00)
            self._emit(0x00)
            self._fixups.append((off, addr_or_label, False, True))
        else:
            self._emit16(addr_or_label)

    # ── Branches ──

    def _branch(self, opcode, target):
        self._emit(opcode)
        if isinstance(target, str):
            off = len(self._output)
            self._emit(0x00)
            self._fixups.append((off, target, True, False))
        else:
            # target is an absolute address
            rel = target - (self._pc + 1)
            if rel < -128 or rel > 127:
                raise ValueError(f"Branch out of range: {rel}")
            self._emit(rel & 0xFF)

    def bne(self, t): self._branch(0xD0, t)
    def beq(self, t): self._branch(0xF0, t)
    def bcc(self, t): self._branch(0x90, t)
    def bcs(self, t): self._branch(0xB0, t)
    def bpl(self, t): self._branch(0x10, t)
    def bmi(self, t): self._branch(0x30, t)

    # ── Assembly output ──

    def assemble(self) -> bytes:
        """Resolve all fixups and return assembled bytes."""
        out = bytearray(self._output)
        for off, label_name, is_relative, is_word in self._fixups:
            if label_name not in self._labels:
                raise ValueError(f"Undefined label: '{label_name}'")
            target = self._labels[label_name]
            if is_relative:
                # Branch: relative to byte AFTER the offset byte
                branch_base = self._org + off + 1
                rel = target - branch_base
                if rel < -128 or rel > 127:
                    raise ValueError(
                        f"Branch to '{label_name}' out of range: "
                        f"{rel} (from ${branch_base:04X} to ${target:04X})")
                out[off] = rel & 0xFF
            elif is_word:
                out[off] = target & 0xFF
                out[off + 1] = (target >> 8) & 0xFF
            else:
                out[off] = target & 0xFF
        return bytes(out)

    def get_label(self, name: str) -> int:
        """Get the address of a defined label."""
        if name not in self._labels:
            raise ValueError(f"Undefined label: '{name}'")
        return self._labels[name]

    # ── Internals ──

    def _emit(self, byte: int):
        self._output.append(byte & 0xFF)
        self._pc += 1

    def _emit16(self, value: int):
        self._emit(value & 0xFF)
        self._emit((value >> 8) & 0xFF)

    def _emit16_or_label(self, value):
        """Emit a 16-bit value or register a label fixup."""
        if isinstance(value, str):
            off = len(self._output)
            self._emit(0x00)
            self._emit(0x00)
            self._fixups.append((off, value, False, True))
        else:
            self._emit16(value)
