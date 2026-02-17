"""Generate 6502 player machine code for RAW and DeltaLZ modes.

RAW mode: IRQ momentarily switches bank, reads sample, switches back.
DeltaLZ mode: IRQ IS the decompressor. Each interrupt decodes one LZ
  byte, delta-accumulates, writes AUDC via table lookup. State machine
  tracks literal runs, match copies, and token fetches across IRQs.

Key insight: PORTB AND #$FE keeps OS ROM disabled while a data bank is
paged in, so the IRQ vector at $FFFE stays in RAM. No SEI needed.

Memory map (compressed): $2000 code+tables, $4000 bank window,
$8000-$BFFF decode buffer (raw deltas for LZ match back-references).

4-channel mode: all 4 POKEY channels in volume-only mode.
1CPS (1-Channel-Per-Sample) mode: each sample writes ONE register via
indexed addressing (sta AUDC1,x). Zero transition wobble, 54-cycle IRQ
enables 12-15 kHz sample rates.
"""

from .tables import (pack_dual_byte, quad_index_to_volumes, QUAD_MAX_LEVEL,
                     index_to_volumes, max_level, get_table)
from .layout import TAB_MEM_BANKS, DBANK_TABLE

# Hardware registers
PORTB  = 0xD301
AUDC1  = 0xD201; AUDC2  = 0xD203; AUDC3  = 0xD205; AUDC4  = 0xD207
AUDF1  = 0xD200; AUDF3  = 0xD204
AUDCTL = 0xD208; STIMER = 0xD209; SKCTL  = 0xD20F
IRQEN  = 0xD20E
DMACTL = 0xD400; DLISTL = 0xD402; DLISTH = 0xD403; NMIEN  = 0xD40E
P2_AUDC1 = 0xD211; P2_AUDC2 = 0xD213; P2_AUDC3 = 0xD215; P2_AUDC4 = 0xD217
P2_AUDF1 = 0xD210; P2_AUDCTL = 0xD218; P2_SKCTL = 0xD21F; P2_IRQEN = 0xD21E

# Zero page -- RAW mode
ZP_SAMPLE_PTR = 0x80   # 2 bytes
ZP_BANK_IDX   = 0x82
ZP_PLAYING    = 0x83
ZP_IRQ_SAVE_A = 0x84
ZP_IRQ_SAVE_X = 0x85
ZP_CACHED_RAW = 0x86   # cached sample index for fixed-timing output
ZP_STASH_LEFT = 0x91   # stereo RAW only

# Zero page -- in-IRQ compressed mode
ZP_LZ_SRC     = 0x80   # 2 bytes (read ptr in bank $4000+)
ZP_LZ_DST     = 0x82   # 2 bytes (write ptr in buffer $8000+)
ZP_LZ_COUNT   = 0x84   # 1 byte
ZP_LZ_MATCH   = 0x85   # 2 bytes
ZP_LZ_MODE    = 0x87   # 1 byte (0=token, 1=literal, 2=match)
ZP_DELTA_ACC  = 0x88   # 1 byte
ZP_C_BANK_IDX = 0x89   # 1 byte
ZP_C_PLAYING  = 0x8A   # 1 byte
ZP_C_SAVE_A   = 0x8B   # 1 byte
ZP_C_SAVE_X   = 0x8C   # 1 byte
ZP_CACHED_LZ  = 0x8D   # cached sample index for fixed-timing output

PORTB_MAIN = 0xFE
IRQ_MASK   = 0x01
CODE_BASE  = 0x2000
BANK_BASE  = 0x4000

# Display / keyboard registers for splash screen
COLPF1_W = 0xD017
COLPF2_W = 0xD018
COLBK_W  = 0xD01A
SKSTAT   = 0xD20F   # read (same addr as SKCTL write)
KBCODE   = 0xD209   # read (same addr as STIMER write)

# VQ codebook location in main RAM ($8000 — free since no LZ buffer)
VQ_CB_BASE = 0x8000

# Zero page -- VQ mode
ZP_VQ_VEC_PTR  = 0x80  # 2 bytes: pointer to current vector in main-RAM codebook
ZP_VQ_BANK_IDX = 0x82  # 1 byte
ZP_VQ_PLAYING  = 0x83  # 1 byte
ZP_VQ_SAVE_A   = 0x84  # 1 byte
ZP_VQ_SAVE_X   = 0x85  # 1 byte
ZP_VQ_CACHED   = 0x86  # 1 byte: cached sample for fixed-timing output
ZP_VQ_VEC_POS  = 0x87  # 1 byte: position within vector (0..vec_size-1)
ZP_VQ_IDX_PTR  = 0x88  # 2 bytes: pointer into index stream in banked memory
ZP_VQ_IDX_END  = 0x8A  # 2 bytes: end of index stream in banked memory
ZP_VQ_NEED_BANK = 0x8C # 1 byte: flag for main loop to copy next codebook


class _CodeBuilder:
    """Simple 6502 machine code builder with forward reference patching."""

    def __init__(self, origin):
        self.origin = origin
        self.code = bytearray()
        self.labels = {}
        self._patches = []

    @property
    def pc(self):
        return self.origin + len(self.code)

    def label(self, name):
        self.labels[name] = self.pc

    def emit(self, *data):
        self.code.extend(data)

    def lda_imm(self, v):   self.emit(0xA9, v & 0xFF)
    def ldx_imm(self, v):   self.emit(0xA2, v & 0xFF)
    def ldy_imm(self, v):   self.emit(0xA0, v & 0xFF)
    def sta_abs(self, a):   self.emit(0x8D, a & 0xFF, (a >> 8) & 0xFF)
    def lda_abs(self, a):   self.emit(0xAD, a & 0xFF, (a >> 8) & 0xFF)
    def sta_zp(self, z):    self.emit(0x85, z)
    def lda_zp(self, z):    self.emit(0xA5, z)
    def ldx_zp(self, z):    self.emit(0xA6, z)
    def ldy_zp(self, z):    self.emit(0xA4, z)
    def stx_zp(self, z):    self.emit(0x86, z)
    def sty_zp(self, z):    self.emit(0x84, z)
    def inc_zp(self, z):    self.emit(0xE6, z)
    def dec_zp(self, z):    self.emit(0xC6, z)
    def lda_indy(self, z):  self.emit(0xB1, z)
    def sta_indy(self, z):  self.emit(0x91, z)
    def lda_absx(self, a):  self.emit(0xBD, a & 0xFF, (a >> 8) & 0xFF)
    def and_imm(self, v):   self.emit(0x29, v & 0xFF)
    def ora_imm(self, v):   self.emit(0x09, v & 0xFF)
    def cmp_imm(self, v):   self.emit(0xC9, v & 0xFF)
    def cmp_zp(self, z):    self.emit(0xC5, z)
    def cpx_imm(self, v):   self.emit(0xE0, v & 0xFF)
    def cpy_imm(self, v):   self.emit(0xC0, v & 0xFF)
    def inx(self):          self.emit(0xE8)
    def adc_imm(self, v):   self.emit(0x69, v & 0xFF)
    def sbc_imm(self, v):   self.emit(0xE9, v & 0xFF)
    def adc_zp(self, z):    self.emit(0x65, z)
    def sbc_zp(self, z):    self.emit(0xE5, z)
    def clc(self):          self.emit(0x18)
    def sec(self):          self.emit(0x38)
    def sei(self):          self.emit(0x78)
    def cli(self):          self.emit(0x58)
    def cld(self):          self.emit(0xD8)
    def rti(self):          self.emit(0x40)
    def rts(self):          self.emit(0x60)
    def tax(self):          self.emit(0xAA)
    def txa(self):          self.emit(0x8A)
    def tay(self):          self.emit(0xA8)
    def iny(self):          self.emit(0xC8)
    def dex(self):          self.emit(0xCA)
    def lsr_a(self):        self.emit(0x4A)
    def nop(self):          self.emit(0xEA)
    def sta_absx(self, a):  self.emit(0x9D, a & 0xFF, (a >> 8) & 0xFF)
    def ldx_absy(self, a):  self.emit(0xBE, a & 0xFF, (a >> 8) & 0xFF)
    def lda_absy(self, a):  self.emit(0xB9, a & 0xFF, (a >> 8) & 0xFF)

    def bne(self, target):  self._branch(0xD0, target)
    def beq(self, target):  self._branch(0xF0, target)
    def bcs(self, target):  self._branch(0xB0, target)
    def bcc(self, target):  self._branch(0x90, target)
    def bpl(self, target):  self._branch(0x10, target)

    def jmp(self, target):
        self.emit(0x4C)
        self._abs_ref(target)

    def jsr(self, target):
        self.emit(0x20)
        self._abs_ref(target)

    def lda_imm_lo(self, lbl):
        self.emit(0xA9, 0x00)
        self._patches.append((len(self.code) - 1, lbl, 'lo'))

    def lda_imm_hi(self, lbl):
        self.emit(0xA9, 0x00)
        self._patches.append((len(self.code) - 1, lbl, 'hi'))

    def _branch(self, opcode, target):
        self.emit(opcode, 0x00)
        self._patches.append((len(self.code) - 1, target, 'rel'))

    def _abs_ref(self, target):
        if isinstance(target, str):
            self.emit(0x00, 0x00)
            self._patches.append((len(self.code) - 2, target, 'abs'))
        else:
            self.emit(target & 0xFF, (target >> 8) & 0xFF)

    def resolve(self):
        for offset, lbl, kind in self._patches:
            addr = self.labels[lbl]
            if kind == 'abs':
                self.code[offset] = addr & 0xFF
                self.code[offset + 1] = (addr >> 8) & 0xFF
            elif kind == 'lo':
                self.code[offset] = addr & 0xFF
            elif kind == 'hi':
                self.code[offset] = (addr >> 8) & 0xFF
            elif kind == 'rel':
                pc_after = self.origin + offset + 1
                diff = addr - pc_after
                if diff < -128 or diff > 127:
                    raise ValueError(
                        f"Branch to '{lbl}' out of range: {diff} "
                        f"(from ${pc_after:04X} to ${addr:04X})")
                self.code[offset] = diff & 0xFF
        return bytes(self.code)


# ======================================================================
# Splash screen: text display + SPACE-to-play + return-to-idle
# ======================================================================

def _to_screen_codes(text):
    """Convert ASCII text to ANTIC mode 2 screen codes (40 chars)."""
    codes = []
    for ch in text[:40]:
        v = ord(ch)
        if 0x20 <= v <= 0x5F:
            codes.append(v - 0x20)
        elif 0x60 <= v <= 0x7F:
            codes.append(v)
        else:
            codes.append(0x00)
    while len(codes) < 40:
        codes.append(0x00)
    return codes


def _format_info_line(pokey_channels, sample_rate, compress_mode,
                      vec_size=8, ram_kb=64):
    """Format 40-column info line for splash screen."""
    rate_hz = int(round(sample_rate))
    ch_str = f"{pokey_channels}CH"
    rate_str = f"{rate_hz}HZ"

    if compress_mode == 'vq':
        comp_str = f"VQ VEC={vec_size}"
    elif compress_mode == 'lz':
        comp_str = "DELTALZ"
    else:
        comp_str = "RAW"

    ram_str = f"{ram_kb}KB"
    line = f"  {ch_str}  {rate_str}  {comp_str}  {ram_str}"
    return line.upper().ljust(40)[:40]


def _emit_splash(c, pokey_channels, sample_rate, compress_mode,
                 vec_size=8, n_banks=0):
    """Emit splash screen: text, display list, wait loop, return-to-idle.

    Shows two text lines on screen:
      Line 1: "STREAM PLAYER  -  [SPACE] TO PLAY"
      Line 2: "4CH  7988HZ  DELTALZ  128KB"

    If n_banks > 0, a runtime check verifies that mem_detect found
    enough banks. If not, line 1 is replaced with an error message
    and the machine halts (no playback possible).

    Prerequisites:
      - Charset must be copied from ROM to RAM by an INIT segment in
        the XEX (see _build_charset_copy_init). Without this, ANTIC
        reads garbage from $E000 when ROM is disabled.

    Flow:
      start → [mem_check] → show_splash → wait_loop (polls SPACE)
        → play_init (caller must emit this label)
      return_to_idle ← (called when song ends) → show_splash
    """
    CHBASE = 0xD409  # ANTIC character base register

    ram_kb = n_banks * 16 + 64

    # ── Text data (40 bytes each, ANTIC screen codes) ──
    # Normal splash: line1 + line2 contiguous (display list uses continuation)
    line1 = "  STREAM PLAYER  -  [SPACE] TO PLAY     "
    line2 = _format_info_line(pokey_channels, sample_rate, compress_mode,
                              vec_size, ram_kb)
    # Error screen: err_title + err_msg contiguous (same display list trick)
    err_title = "STREAM PLAYER".center(40)
    err_msg = f"ERROR: {ram_kb}KB MEMORY REQUIRED".center(40)

    c.label('text_line1')
    for code in _to_screen_codes(line1):
        c.emit(code)
    c.label('text_line2')
    for code in _to_screen_codes(line2):
        c.emit(code)
    # Error pair must be contiguous so Mode 2 continuation works
    c.label('text_err_title')
    for code in _to_screen_codes(err_title):
        c.emit(code)
    c.label('text_err_msg')
    for code in _to_screen_codes(err_msg):
        c.emit(code)

    # ── Display list ──
    c.label('dlist')
    for _ in range(8):           # 64 blank scanlines (centers text)
        c.emit(0x70)
    c.emit(0x42)                 # Mode 2 + LMS
    c.label('dlist_lms')         # mark LMS address bytes for patching
    addr1 = c.labels['text_line1']
    c.emit(addr1 & 0xFF, (addr1 >> 8) & 0xFF)
    c.emit(0x02)                 # Mode 2 continuation (line 2)
    c.emit(0x41)                 # JVB (jump + wait for VBI)
    addr_dl = c.labels['dlist']
    c.emit(addr_dl & 0xFF, (addr_dl >> 8) & 0xFF)

    # ══════════════════════════════════════════════════════════════
    # ONE-TIME STARTUP (runs once at boot via RUN address)
    # ══════════════════════════════════════════════════════════════
    # The INIT segment already copied charset from ROM to RAM and
    # left PORTB=$FE (ROM off). We need to:
    #   1. Disable all interrupt sources FIRST (prevent spurious IRQ)
    #   2. Set BOTH NMI and IRQ vectors (RAM is writable at $FFFA+)
    #   3. Initialize keyboard scan
    c.label('start')
    c.sei(); c.cld()
    # Disable everything before touching vectors
    c.lda_imm(0x00)
    c.sta_abs(NMIEN)             # NMI off
    c.sta_abs(IRQEN)             # IRQ sources off
    c.sta_abs(DMACTL)            # DMA off
    c.lda_imm(PORTB_MAIN); c.sta_abs(PORTB)  # ROM off, main RAM
    # Set NMI vector → safe RTI stub
    c.lda_imm_lo('nmi_handler'); c.sta_abs(0xFFFA)
    c.lda_imm_hi('nmi_handler'); c.sta_abs(0xFFFB)
    # Set IRQ vector → same RTI stub (safe dummy for splash state)
    c.lda_imm_lo('nmi_handler'); c.sta_abs(0xFFFE)
    c.lda_imm_hi('nmi_handler'); c.sta_abs(0xFFFF)
    # Point ANTIC at charset in RAM (same address $E000, now RAM)
    c.lda_imm(0xE0); c.sta_abs(CHBASE)
    # Initialize keyboard scan
    c.lda_imm(0x00); c.sta_abs(SKCTL)
    c.lda_imm(0x03); c.sta_abs(SKCTL)

    # ══════════════════════════════════════════════════════════════
    # MEMORY CHECK (skip if n_banks==0, i.e. no extended RAM needed)
    # ══════════════════════════════════════════════════════════════
    if n_banks > 0:
        # TAB_MEM_BANKS entries are filled sequentially: +1, +2, ...
        # If entry +n_banks is non-zero, we have enough banks.
        c.lda_abs(TAB_MEM_BANKS + n_banks)
        c.bne('show_splash')     # non-zero → sufficient memory

        # Insufficient: patch display list LMS to show error text pair
        c.lda_imm_lo('text_err_title')
        c.sta_abs(c.labels['dlist_lms'])
        c.lda_imm_hi('text_err_title')
        c.sta_abs(c.labels['dlist_lms'] + 1)

        # Show error display and halt
        c.lda_imm_lo('dlist'); c.sta_abs(DLISTL)
        c.lda_imm_hi('dlist'); c.sta_abs(DLISTH)
        c.lda_imm(0x22); c.sta_abs(DMACTL)     # DL DMA + playfield
        c.lda_imm(0x40); c.sta_abs(NMIEN)      # VBI only (for JVB)
        c.lda_imm(0x3E); c.sta_abs(COLPF1_W)   # text: bright red
        c.lda_imm(0x00); c.sta_abs(COLPF2_W)   # text bg: black
        c.lda_imm(0x30); c.sta_abs(COLBK_W)    # border: red
        c.cli()
        c.label('error_halt')
        c.jmp('error_halt')

    # ══════════════════════════════════════════════════════════════
    # SHOW SPLASH (also re-entry point after song ends)
    # ══════════════════════════════════════════════════════════════
    c.label('show_splash')
    c.lda_imm_lo('dlist'); c.sta_abs(DLISTL)
    c.lda_imm_hi('dlist'); c.sta_abs(DLISTH)
    c.lda_imm(0x22); c.sta_abs(DMACTL)     # DL DMA + playfield
    c.lda_imm(0x40); c.sta_abs(NMIEN)      # VBI only (for JVB)
    c.lda_imm(0x0E); c.sta_abs(COLPF1_W)   # text: bright white
    c.lda_imm(0x94); c.sta_abs(COLPF2_W)   # text bg: blue
    c.lda_imm(0x00); c.sta_abs(COLBK_W)    # border: black
    # Reset keyboard state before polling
    c.lda_imm(0x00); c.sta_abs(SKCTL)
    c.lda_imm(0x03); c.sta_abs(SKCTL)
    c.cli()

    # ══════════════════════════════════════════════════════════════
    # WAIT FOR SPACE KEY
    # ══════════════════════════════════════════════════════════════
    # SKSTAT bit 2: "last key still pressed" (active low: 0=held)
    # KBCODE bits 0-5: scan code (SPACE = $21)
    c.label('wait_loop')
    c.lda_abs(SKSTAT)
    c.and_imm(0x04)              # bit 2: 0=key held down
    c.bne('wait_loop')           # bit set → no key → keep waiting
    c.lda_abs(SKSTAT)            # double-read for debounce
    c.and_imm(0x04)
    c.bne('wait_loop')

    # Key is held — check which key
    c.lda_abs(KBCODE)
    c.and_imm(0x3F)              # mask to key code (ignore shift/ctrl)
    c.cmp_imm(0x21)              # SPACE = $21
    c.beq('got_space')

    # Wrong key: wait for release, then resume polling
    c.label('wait_release')
    c.lda_abs(SKSTAT)
    c.and_imm(0x04)
    c.beq('wait_release')        # still held → keep waiting
    c.lda_abs(SKSTAT)            # double-read for debounce
    c.and_imm(0x04)
    c.beq('wait_release')
    c.jmp('wait_loop')           # released → poll again

    # SPACE pressed — wait for release to prevent ghost trigger
    c.label('got_space')
    c.label('space_release')
    c.lda_abs(SKSTAT)
    c.and_imm(0x04)
    c.beq('space_release')       # still held → wait
    c.lda_abs(SKSTAT)            # double-read for debounce
    c.and_imm(0x04)
    c.beq('space_release')

    # ── Transition to playback ──
    c.sei()
    c.lda_imm(0x00); c.sta_abs(DMACTL)     # screen OFF
    c.lda_imm(0x00); c.sta_abs(NMIEN)      # NMI OFF
    c.jmp('play_init')  # caller must emit 'play_init' label


def _emit_return_to_idle(c, pokey_channels):
    """Emit return-to-idle code (called when song ends).

    Disables IRQ, silences POKEY, restores main RAM bank,
    then jumps back to show_splash for next play cycle.
    """
    audc_regs = [AUDC1, AUDC2, AUDC3, AUDC4]
    c.label('return_to_idle')
    c.sei()
    c.lda_imm(0x00); c.sta_abs(IRQEN)
    c.lda_imm(0x10)
    for ch in range(4):
        c.sta_abs(audc_regs[ch])
    c.lda_imm(PORTB_MAIN); c.sta_abs(PORTB)
    c.jmp('show_splash')


def build_charset_copy_init():
    """Build INIT segment that copies OS ROM to underlying RAM.

    Copies $C000-$CFFF and $D800-$FFFF (skipping I/O at $D000-$D7FF).
    This allows ROM to be disabled later while keeping the character set
    and other OS data available in RAM.

    Follows the proven pattern from PokeyVQ tracker's copy_os_ram.asm:
      - SEI + disable NMIEN
      - INC/DEC $D301 for fast ROM/RAM toggling
      - Skip I/O area ($D000-$D7FF)
      - Re-enable NMIEN=$40 + CLI before RTS (DOS needs IRQ for SIO)

    Returns: 6502 machine code bytes (placed at $2E00 as INIT segment).
    """
    # ZP pointer — uses $CB which doesn't conflict with player vars ($80-$BF)
    ZP_PTR = 0xCB
    PORTB_HW = 0xD301
    NMIEN_HW = 0xD40E

    code = bytearray()

    def emit(*b):
        code.extend(b)

    # SEI
    emit(0x78)
    # LDA #$00 / STA NMIEN — disable NMI (VBI would crash with ROM off)
    emit(0xA9, 0x00, 0x8D, NMIEN_HW & 0xFF, (NMIEN_HW >> 8) & 0xFF)

    # Initialize pointer to $C000
    # LDY #$00 / STY zp / LDA #$C0 / STA zp+1
    emit(0xA0, 0x00)
    emit(0x84, ZP_PTR)
    emit(0xA9, 0xC0)
    emit(0x85, ZP_PTR + 1)

    # Ensure ROM is on: ORA #$01 on PORTB
    # LDA $D301 / ORA #$01 / STA $D301
    emit(0xAD, PORTB_HW & 0xFF, (PORTB_HW >> 8) & 0xFF)
    emit(0x09, 0x01)
    emit(0x8D, PORTB_HW & 0xFF, (PORTB_HW >> 8) & 0xFF)

    # ── Copy loop ──
    copy_loop = len(code)

    # LDA (zp),Y — read from ROM
    emit(0xB1, ZP_PTR)
    # DEC $D301 — switch to RAM (clears bit 0)
    emit(0xCE, PORTB_HW & 0xFF, (PORTB_HW >> 8) & 0xFF)
    # STA (zp),Y — write to RAM
    emit(0x91, ZP_PTR)
    # INC $D301 — switch back to ROM (sets bit 0)
    emit(0xEE, PORTB_HW & 0xFF, (PORTB_HW >> 8) & 0xFF)
    # INY / BNE copy_loop
    emit(0xC8)
    offset = copy_loop - len(code) - 2
    emit(0xD0, offset & 0xFF)

    # INC zp+1 — next page
    emit(0xE6, ZP_PTR + 1)

    # Check if we hit I/O area ($D0)
    # LDA zp+1 / CMP #$D0 / BNE check_end
    emit(0xA5, ZP_PTR + 1)
    emit(0xC9, 0xD0)
    check_end_patch = len(code)
    emit(0xD0, 0x00)  # BNE check_end — patched below

    # Skip I/O: LDA #$D8 / STA zp+1
    emit(0xA9, 0xD8)
    emit(0x85, ZP_PTR + 1)

    # check_end: LDA zp+1 / BNE copy_loop (stops when wraps past $FF to $00)
    check_end = len(code)
    code[check_end_patch + 1] = (check_end - check_end_patch - 2) & 0xFF
    emit(0xA5, ZP_PTR + 1)
    offset2 = copy_loop - len(code) - 2
    emit(0xD0, offset2 & 0xFF)

    # Done — restore interrupts for OS loader
    # LDA #$40 / STA NMIEN — re-enable VBI (OS needs it for loader)
    emit(0xA9, 0x40, 0x8D, NMIEN_HW & 0xFF, (NMIEN_HW >> 8) & 0xFF)
    # CLI — re-enable IRQ (needed for SIO disk loading)
    emit(0x58)
    # RTS
    emit(0x60)

    return bytes(code)


def build_mem_detect_init():
    """Build INIT segment that detects extended memory banks at runtime.

    Implements the @MEM_DETECT algorithm:
      Phase 1: Save $7FFF from each of 64 possible bank codes
      Phase 2: Write PORTB code as signature to $7FFF in each bank
      Phase 3: Write $FF sentinel to main RAM $7FFF, store as entry 0
      Phase 4: Read back — codes whose signature survived are unique banks
      Phase 5: Restore original $7FFF values

    Result stored at TAB_MEM_BANKS ($0480):
      +0: $FF (main memory)
      +1..+N: detected bank PORTB values (one per physical bank found)

    Returns: 6502 machine code bytes (placed at $2E00 as INIT segment).
    """
    BASE = 0x2E00
    PORTB_HW = 0xD301
    NMIEN_HW = 0xD40E
    TEST_ADDR = 0x7FFF
    TMB = TAB_MEM_BANKS

    code = bytearray()
    # Track patch sites: list of (offset_in_code, 'dbank'|'saved')
    patches = []

    def emit(*b):
        code.extend(b)

    def abs_addr(label_name):
        """Emit 2 placeholder bytes for a forward-referenced address."""
        patches.append((len(code), label_name))
        emit(0x00, 0x00)

    # ── SEI + disable NMI ──
    emit(0x78)                                            # SEI
    emit(0xA9, 0x00, 0x8D, NMIEN_HW & 0xFF, (NMIEN_HW >> 8) & 0xFF)  # LDA #0 / STA NMIEN

    # Save current PORTB
    emit(0xAD, PORTB_HW & 0xFF, (PORTB_HW >> 8) & 0xFF)  # LDA PORTB
    emit(0x48)                                            # PHA

    # Zero-fill TAB_MEM_BANKS (65 bytes: indices 0..64)
    emit(0xA9, 0x00)                                      # LDA #0
    emit(0xA2, 64)                                        # LDX #64
    zfill = len(code)
    emit(0x9D, TMB & 0xFF, (TMB >> 8) & 0xFF)            # STA TMB,X
    emit(0xCA)                                            # DEX
    emit(0x10, (zfill - len(code) - 2) & 0xFF)           # BPL zfill

    # ── Phase 1: Save $7FFF from each of 64 bank codes ──
    emit(0xA2, 63)                                        # LDX #63
    p1 = len(code)
    emit(0xBD); abs_addr('dbank')                         # LDA dbank,X
    emit(0x8D, PORTB_HW & 0xFF, (PORTB_HW >> 8) & 0xFF)  # STA PORTB
    emit(0xAD, TEST_ADDR & 0xFF, (TEST_ADDR >> 8) & 0xFF) # LDA $7FFF
    emit(0x9D); abs_addr('saved')                         # STA saved,X
    emit(0xCA)                                            # DEX
    emit(0x10, (p1 - len(code) - 2) & 0xFF)              # BPL p1

    # ── Phase 2: Write signatures (PORTB code → $7FFF) ──
    emit(0xA2, 63)                                        # LDX #63
    p2 = len(code)
    emit(0xBD); abs_addr('dbank')                         # LDA dbank,X
    emit(0x8D, PORTB_HW & 0xFF, (PORTB_HW >> 8) & 0xFF)  # STA PORTB
    emit(0x8D, TEST_ADDR & 0xFF, (TEST_ADDR >> 8) & 0xFF) # STA $7FFF
    emit(0xCA)                                            # DEX
    emit(0x10, (p2 - len(code) - 2) & 0xFF)              # BPL p2

    # ── Phase 3: Main RAM sentinel ──
    emit(0x68)                                            # PLA (original PORTB)
    emit(0x09, 0x11)                                      # ORA #$11 (bit0=ROM, bit4=main RAM)
    emit(0x48)                                            # PHA
    emit(0x8D, PORTB_HW & 0xFF, (PORTB_HW >> 8) & 0xFF)  # STA PORTB
    emit(0xA9, 0xFF)                                      # LDA #$FF
    emit(0x8D, TEST_ADDR & 0xFF, (TEST_ADDR >> 8) & 0xFF) # STA $7FFF
    emit(0x8D, TMB & 0xFF, (TMB >> 8) & 0xFF)            # STA TMB+0

    # ── Phase 4: Verify — find unique banks ──
    emit(0xA0, 0x01)                                      # LDY #1 (output idx)
    emit(0xA2, 63)                                        # LDX #63
    p4 = len(code)
    emit(0xBD); abs_addr('dbank')                         # LDA dbank,X
    emit(0x8D, PORTB_HW & 0xFF, (PORTB_HW >> 8) & 0xFF)  # STA PORTB
    emit(0xCD, TEST_ADDR & 0xFF, (TEST_ADDR >> 8) & 0xFF) # CMP $7FFF
    skip_patch = len(code)
    emit(0xD0, 0x00)                                      # BNE skip (patched)
    emit(0x99, TMB & 0xFF, (TMB >> 8) & 0xFF)            # STA TMB,Y
    emit(0xC8)                                            # INY
    skip_target = len(code)
    code[skip_patch + 1] = (skip_target - skip_patch - 2) & 0xFF
    emit(0xCA)                                            # DEX
    emit(0x10, (p4 - len(code) - 2) & 0xFF)              # BPL p4

    # ── Phase 5: Restore saved $7FFF values ──
    emit(0xA2, 63)                                        # LDX #63
    p5 = len(code)
    emit(0xBD); abs_addr('dbank')                         # LDA dbank,X
    emit(0x8D, PORTB_HW & 0xFF, (PORTB_HW >> 8) & 0xFF)  # STA PORTB
    emit(0xBD); abs_addr('saved')                         # LDA saved,X
    emit(0x8D, TEST_ADDR & 0xFF, (TEST_ADDR >> 8) & 0xFF) # STA $7FFF
    emit(0xCA)                                            # DEX
    emit(0x10, (p5 - len(code) - 2) & 0xFF)              # BPL p5

    # ── Restore PORTB and interrupts ──
    emit(0x68)                                            # PLA
    emit(0x8D, PORTB_HW & 0xFF, (PORTB_HW >> 8) & 0xFF)  # STA PORTB
    emit(0xA9, 0x40)                                      # LDA #$40
    emit(0x8D, NMIEN_HW & 0xFF, (NMIEN_HW >> 8) & 0xFF)  # STA NMIEN
    emit(0x58)                                            # CLI
    emit(0x60)                                            # RTS

    # ── Data tables (after code) ──
    labels = {}
    labels['dbank'] = BASE + len(code)
    for v in DBANK_TABLE:
        emit(v)

    labels['saved'] = BASE + len(code)
    for _ in range(64):
        emit(0x00)

    # ── Patch forward references ──
    for offset, name in patches:
        addr = labels[name]
        code[offset] = addr & 0xFF
        code[offset + 1] = (addr >> 8) & 0xFF

    return bytes(code)


def _emit_copy_detected_banks(c, n_banks):
    """Emit code to copy n_banks detected PORTB values into portb_table.

    Reads from TAB_MEM_BANKS+1..+n_banks (filled by mem_detect at load time)
    and writes to the player's embedded portb_table (filled with placeholders
    at build time).  Must be called in play_init BEFORE any portb_table access.
    """
    if n_banks <= 0:
        return  # nothing to copy
    c.ldx_imm(n_banks - 1)
    c.label('copy_det')
    c.lda_absx(TAB_MEM_BANKS + 1)
    c.sta_absx(c.labels['portb_table'])
    c.dex()
    c.bpl('copy_det')
# ======================================================================

def build_raw_player(pokey_divisor, audctl, n_banks, portb_table, stereo,
                     pokey_channels=4, sample_rate=8000):
    """Build direct-read player (no compression, N-channel)."""
    c = _CodeBuilder(CODE_BASE)

    c.label('nmi_handler'); c.rti()

    c.label('portb_table')
    for _ in range(64): c.emit(PORTB_MAIN)  # placeholder — filled by copy_det

    # N AUDC lookup tables (256 bytes each)
    max_lvl = max_level(pokey_channels)
    for ch in range(pokey_channels):
        c.label(f'audc{ch+1}_tab')
        for idx in range(max_lvl + 1):
            vols = index_to_volumes(idx, pokey_channels)
            c.emit(vols[ch] | 0x10)
        for _ in range(256 - (max_lvl + 1)):
            c.emit(0x10)

    # ── Splash screen ──
    _emit_splash(c, pokey_channels, sample_rate, 'off', n_banks=n_banks)

    # ── Play init (entered from splash on SPACE press, SEI already done) ──
    c.label('play_init')
    c.lda_imm_lo('irq_handler'); c.sta_abs(0xFFFE)
    c.lda_imm_hi('irq_handler'); c.sta_abs(0xFFFF)

    # Copy runtime-detected bank values into portb_table
    _emit_copy_detected_banks(c, n_banks)

    c.lda_imm(0x00); c.sta_zp(ZP_SAMPLE_PTR)
    c.lda_imm(0x40); c.sta_zp(ZP_SAMPLE_PTR + 1)
    c.lda_imm(0x00); c.sta_zp(ZP_BANK_IDX)
    c.lda_imm(0xFF); c.sta_zp(ZP_PLAYING)
    # Prime cached sample
    c.lda_abs(c.labels['portb_table']); c.sta_abs(PORTB)
    c.lda_abs(BANK_BASE)
    c.sta_zp(ZP_CACHED_RAW)
    c.lda_imm(PORTB_MAIN); c.sta_abs(PORTB)
    c.inc_zp(ZP_SAMPLE_PTR)

    c.jsr('pokey_setup')
    c.cli()

    c.label('main_loop'); c.lda_zp(ZP_PLAYING); c.bne('main_loop')
    c.jmp('return_to_idle')

    # ── Return to idle ──
    _emit_return_to_idle(c, pokey_channels)

    _emit_pokey_setup(c, pokey_divisor, audctl, stereo,
                      ZP_PLAYING, ZP_IRQ_SAVE_A, ZP_IRQ_SAVE_X,
                      pokey_channels=pokey_channels)
    _emit_irq_raw(c, stereo, n_banks, pokey_channels)

    return c.resolve(), CODE_BASE, c.labels['start']


# ======================================================================
# Compressed mode: in-IRQ LZ decode
# ======================================================================

def build_lzsa_player(pokey_divisor, audctl, n_banks, portb_table, stereo,
                      pokey_channels=4, mode='1cps', sample_rate=8000):
    """Build DeltaLZ player with in-IRQ decompression.

    The IRQ handler IS the decompressor: each interrupt decodes one byte,
    delta-accumulates, writes AUDC via lookup table.

    Modes:
      '1cps':   1-Channel-Per-Sample. Each sample writes ONE AUDC register
                via indexed addressing. Zero transition wobble, 54-cycle IRQ.
      'scalar': Legacy scalar quantization. 4 AUDC writes per sample.

    Returns: (code_bytes, code_origin, start_addr)
    """
    c = _CodeBuilder(CODE_BASE)

    # -- NMI handler --
    c.label('nmi_handler'); c.rti()

    # -- PORTB table --
    c.label('portb_table')
    for _ in range(64): c.emit(PORTB_MAIN)  # placeholder — filled by copy_det

    if mode == '1cps':
        # 2 lookup tables (256 bytes each), indexed by delta_acc value.
        # Byte format: (channel << 4) | volume
        # ch_offset_tab: maps byte → AUDC register offset (0,2,4,6)
        # audc_val_tab:  maps byte → volume | $10
        c.label('ch_offset_tab')
        for byte_val in range(256):
            ch = (byte_val >> 4) & 0x03
            c.emit(ch * 2)  # AUDC1=0, AUDC2=2, AUDC3=4, AUDC4=6
        c.label('audc_val_tab')
        for byte_val in range(256):
            vol = byte_val & 0x0F
            c.emit(vol | 0x10)
    elif pokey_channels >= 1:
        # N AUDC tables (256 bytes each)
        max_lvl = max_level(pokey_channels)
        for ch in range(pokey_channels):
            c.label(f'audc{ch+1}_tab')
            for idx in range(max_lvl + 1):
                vols = index_to_volumes(idx, pokey_channels)
                c.emit(vols[ch] | 0x10)
            for _ in range(256 - (max_lvl + 1)):
                c.emit(0x10)

    # ── Splash screen ──
    _emit_splash(c, pokey_channels, sample_rate, 'lz', n_banks=n_banks)

    # ── Play init ──
    c.label('play_init')
    c.lda_imm_lo('irq_handler'); c.sta_abs(0xFFFE)
    c.lda_imm_hi('irq_handler'); c.sta_abs(0xFFFF)

    # Copy runtime-detected bank values into portb_table
    _emit_copy_detected_banks(c, n_banks)

    c.jsr('init_first_bank')
    c.jsr('pokey_setup')
    c.cli()

    c.label('main_loop'); c.lda_zp(ZP_C_PLAYING); c.bne('main_loop')
    c.jmp('return_to_idle')

    # ── Return to idle ──
    _emit_return_to_idle(c, pokey_channels)

    # ==========================================================
    # SUBROUTINES
    # ==========================================================
    _emit_pokey_setup(c, pokey_divisor, audctl, stereo,
                      ZP_C_PLAYING, ZP_C_SAVE_A, ZP_C_SAVE_X,
                      pokey_channels)
    _emit_irq_inlz(c, n_banks, pokey_channels, mode)
    _emit_init_first_bank(c)

    return c.resolve(), CODE_BASE, c.labels['start']


# ======================================================================
# POKEY setup (shared)
# ======================================================================

def _emit_pokey_setup(c, divisor, audctl, stereo, zp_playing, zp_save_a,
                      zp_save_x, pokey_channels=4):
    audc_regs = [AUDC1, AUDC2, AUDC3, AUDC4]
    p2_audc_regs = [P2_AUDC1, P2_AUDC2, P2_AUDC3, P2_AUDC4]
    c.label('pokey_setup')
    c.lda_imm(0x00); c.sta_abs(IRQEN); c.sta_abs(SKCTL)
    c.lda_imm(0x10)
    for ch in range(4):  # always silence all 4
        c.sta_abs(audc_regs[ch])
    c.lda_imm(divisor); c.sta_abs(AUDF1)
    c.lda_imm(audctl); c.sta_abs(AUDCTL)
    c.lda_imm(0x03); c.sta_abs(SKCTL)
    if stereo:
        c.lda_imm(0x00); c.sta_abs(P2_IRQEN); c.sta_abs(P2_SKCTL)
        c.lda_imm(0x10)
        for ch in range(pokey_channels):
            c.sta_abs(p2_audc_regs[ch])
        c.lda_imm(divisor); c.sta_abs(P2_AUDF1)
        c.lda_imm(audctl); c.sta_abs(P2_AUDCTL)
        c.lda_imm(0x03); c.sta_abs(P2_SKCTL)
    c.lda_imm(IRQ_MASK); c.sta_abs(IRQEN)
    c.lda_imm(0x00); c.sta_abs(STIMER)
    c.rts()


# ======================================================================
# In-IRQ LZ decompression handler
# ======================================================================

def _emit_irq_inlz(c, n_banks, pokey_channels=4, mode='1cps'):
    """IRQ state machine: write-first-then-decode for fixed timing.

    Architecture:
      1. Save regs + ACK POKEY (18cy)
      2. Write AUDC from cached sample (35cy) ← FIXED timing, cycle ~23
      3. Decode NEXT sample via LZ state machine (variable)
      4. Cache decoded result for next IRQ
      5. Restore + RTI

    Mode dispatch via LSR:
      mode 0: LSR -> A=0,C=0 -> fall to JMP @need_token
      mode 1: LSR -> A=0,C=1 -> BCS @literal_byte (hot path)
      mode 2: LSR -> A=1,C=0 -> BNE @go_match
    """
    c.label('irq_handler')
    c.sta_zp(ZP_C_SAVE_A); c.stx_zp(ZP_C_SAVE_X)

    # ACK IRQ
    c.lda_imm(0x00); c.sta_abs(IRQEN)
    c.lda_imm(IRQ_MASK); c.sta_abs(IRQEN)

    c.lda_zp(ZP_C_PLAYING); c.beq('irq_exit')

    # --- FIXED-TIMING OUTPUT: write cached sample ---
    if mode == '1cps':
        c.ldy_zp(ZP_CACHED_LZ)
        c.ldx_absy(c.labels['ch_offset_tab'])   # X = AUDC offset
        c.lda_absy(c.labels['audc_val_tab'])    # A = vol | $10
        c.sta_absx(AUDC1)                        # write single register
    else:
        audc_regs = [AUDC1, AUDC2, AUDC3, AUDC4]
        c.ldx_zp(ZP_CACHED_LZ)
        for ch in range(pokey_channels):
            c.lda_absx(c.labels[f'audc{ch+1}_tab']); c.sta_abs(audc_regs[ch])

    # --- DECODE NEXT SAMPLE (timing no longer matters) ---

    # Mode dispatch via LSR
    c.lda_zp(ZP_LZ_MODE)
    c.lsr_a()
    c.bcs('literal_byte')      # mode 1
    c.bne('go_match')          # mode 2
    c.jmp('need_token')        # mode 0

    c.label('go_match')
    c.jmp('match_byte')

    # -- MODE 1: Literal (hot path, falls through to output) --
    c.label('literal_byte')
    c.ldy_imm(0x00)
    c.lda_indy(ZP_LZ_SRC)     # read delta from bank
    c.sta_indy(ZP_LZ_DST)     # store to buffer
    c.inc_zp(ZP_LZ_SRC)
    c.bne('output')
    c.inc_zp(ZP_LZ_SRC + 1)

    # -- Common output: CACHE for next IRQ (instead of AUDC writes) --
    c.label('output')

    if mode == '1cps':
        # 1CPS: raw packed byte, cache directly
        c.sta_zp(ZP_CACHED_LZ)
    else:
        # Scalar: delta accumulate, cache result
        c.clc()
        c.adc_zp(ZP_DELTA_ACC)
        c.sta_zp(ZP_DELTA_ACC)
        c.sta_zp(ZP_CACHED_LZ)         # cache for next IRQ

    # Advance decode buffer with circular wrap
    c.inc_zp(ZP_LZ_DST)
    c.bne('out_no_carry')
    c.inc_zp(ZP_LZ_DST + 1)
    c.lda_zp(ZP_LZ_DST + 1)
    c.cmp_imm(0xC0)                   # past $BFFF?
    c.bcc('out_no_carry')
    c.lda_imm(0x80)                   # wrap to $8000
    c.sta_zp(ZP_LZ_DST + 1)
    c.label('out_no_carry')

    # Decrement counter
    c.dec_zp(ZP_LZ_COUNT)
    c.bne('irq_exit')
    c.lda_imm(0x00); c.sta_zp(ZP_LZ_MODE)

    c.label('irq_exit')
    c.ldx_zp(ZP_C_SAVE_X); c.lda_zp(ZP_C_SAVE_A)
    c.rti()

    # -- MODE 0: Fetch token --
    c.label('need_token')
    c.ldy_imm(0x00)
    c.lda_indy(ZP_LZ_SRC)
    c.bne('token_not_zero')
    c.jmp('end_of_block')            # $00 = end of bank (too far for BEQ)
    c.label('token_not_zero')

    c.inc_zp(ZP_LZ_SRC)
    c.bne('tok_no_carry')
    c.inc_zp(ZP_LZ_SRC + 1)
    c.label('tok_no_carry')

    c.cmp_imm(0x80); c.bcs('token_match')

    # Literal token
    c.sta_zp(ZP_LZ_COUNT)
    c.lda_imm(0x01); c.sta_zp(ZP_LZ_MODE)
    c.jmp('literal_byte')

    # Match token
    c.label('token_match')
    c.tax()                        # save full token in X
    c.and_imm(0x3F); c.clc(); c.adc_imm(0x03); c.sta_zp(ZP_LZ_COUNT)
    c.cpx_imm(0xC0); c.bcs('long_match')

    # Short match: 1-byte offset
    c.ldy_imm(0x00)
    c.lda_indy(ZP_LZ_SRC); c.sta_zp(ZP_LZ_MATCH)
    c.inc_zp(ZP_LZ_SRC)
    c.bne('short_ok'); c.inc_zp(ZP_LZ_SRC + 1)
    c.label('short_ok')
    c.sec()
    c.lda_zp(ZP_LZ_DST); c.sbc_zp(ZP_LZ_MATCH); c.sta_zp(ZP_LZ_MATCH)
    c.lda_zp(ZP_LZ_DST + 1); c.sbc_imm(0x00)
    c.sta_zp(ZP_LZ_MATCH + 1)
    # No wrap: compressor guarantees offset <= buf_pos, so result >= $8000
    c.lda_imm(0x02); c.sta_zp(ZP_LZ_MODE)
    c.jmp('match_byte')

    # Long match: 2-byte offset
    c.label('long_match')
    c.ldy_imm(0x00)
    c.lda_indy(ZP_LZ_SRC); c.sta_zp(ZP_LZ_MATCH)
    c.iny()
    c.lda_indy(ZP_LZ_SRC); c.sta_zp(ZP_LZ_MATCH + 1)
    c.clc(); c.lda_zp(ZP_LZ_SRC); c.adc_imm(0x02); c.sta_zp(ZP_LZ_SRC)
    c.bcc('long_ok'); c.inc_zp(ZP_LZ_SRC + 1)
    c.label('long_ok')
    c.sec()
    c.lda_zp(ZP_LZ_DST); c.sbc_zp(ZP_LZ_MATCH); c.sta_zp(ZP_LZ_MATCH)
    c.lda_zp(ZP_LZ_DST + 1); c.sbc_zp(ZP_LZ_MATCH + 1)
    c.sta_zp(ZP_LZ_MATCH + 1)
    # No wrap: compressor guarantees result stays in [$8000,$BFFF]
    c.lda_imm(0x02); c.sta_zp(ZP_LZ_MODE)
    # fall through to match_byte

    # -- MODE 2: Match copy --
    c.label('match_byte')
    c.ldy_imm(0x00)
    c.lda_indy(ZP_LZ_MATCH); c.sta_indy(ZP_LZ_DST)
    c.inc_zp(ZP_LZ_MATCH)
    c.bne('match_no_carry'); c.inc_zp(ZP_LZ_MATCH + 1)
    # No wrap check: compressor guarantees match source stays in buffer
    c.label('match_no_carry')
    c.jmp('output')

    # -- End of block: switch bank --
    # lz_dst does NOT reset - circular buffer continues across banks
    c.label('end_of_block')
    c.inc_zp(ZP_C_BANK_IDX)
    c.lda_zp(ZP_C_BANK_IDX); c.cmp_imm(n_banks); c.bcs('finished')
    c.ldx_zp(ZP_C_BANK_IDX)
    c.lda_absx(c.labels['portb_table'])
    c.and_imm(0xFE); c.sta_abs(PORTB)
    c.lda_abs(BANK_BASE); c.sta_zp(ZP_DELTA_ACC)
    # Source at $4001 (past 1-byte header)
    c.lda_imm(0x01); c.sta_zp(ZP_LZ_SRC)
    c.lda_imm(0x40); c.sta_zp(ZP_LZ_SRC + 1)
    # lz_dst stays where it is (circular)
    c.jmp('irq_exit')

    c.label('finished')
    c.lda_imm(0x00); c.sta_zp(ZP_C_PLAYING)
    c.lda_imm(0x10)
    audc_all = [AUDC1, AUDC2, AUDC3, AUDC4]
    for ch in range(pokey_channels):
        c.sta_abs(audc_all[ch])
    c.jmp('irq_exit')


def _emit_init_first_bank(c):
    """Set up LZ state for bank 0 (called before CLI)."""
    c.label('init_first_bank')
    c.lda_abs(c.labels['portb_table'])   # bank 0 = first entry
    c.and_imm(0xFE)
    c.sta_abs(PORTB)
    c.lda_abs(BANK_BASE)
    c.sta_zp(ZP_DELTA_ACC)
    c.lda_imm(0x01); c.sta_zp(ZP_LZ_SRC)   # past 1-byte header
    c.lda_imm(0x40); c.sta_zp(ZP_LZ_SRC + 1)
    c.lda_imm(0x00); c.sta_zp(ZP_LZ_DST)
    c.lda_imm(0x80); c.sta_zp(ZP_LZ_DST + 1)
    c.lda_imm(0x00)
    c.sta_zp(ZP_LZ_MODE)
    c.sta_zp(ZP_LZ_COUNT)
    c.sta_zp(ZP_C_BANK_IDX)
    # Prime cached sample: first byte = initial delta_acc = first output level
    c.lda_zp(ZP_DELTA_ACC)
    c.sta_zp(ZP_CACHED_LZ)
    c.lda_imm(0xFF); c.sta_zp(ZP_C_PLAYING)
    c.rts()


# ======================================================================
# RAW mode IRQ handler
# ======================================================================

def _emit_irq_raw(c, stereo, n_banks, pokey_channels=4):
    """RAW IRQ handler: write-first-then-read for fixed timing.

    Note: with 3-4 channels, samples that jump by ≥2 levels cause
    multiple channel changes → intermediate voltage spikes during
    the sequential STA writes. This is a hardware limitation —
    POKEY registers cannot be written atomically.
    """
    audc_regs = [AUDC1, AUDC2, AUDC3, AUDC4]

    c.label('irq_handler')
    c.sta_zp(ZP_IRQ_SAVE_A); c.stx_zp(ZP_IRQ_SAVE_X)

    c.lda_imm(0x00); c.sta_abs(IRQEN)
    c.lda_imm(IRQ_MASK); c.sta_abs(IRQEN)
    c.lda_zp(ZP_PLAYING); c.beq('irq_exit')

    # --- FIXED-TIMING OUTPUT: write cached sample ---
    c.ldx_zp(ZP_CACHED_RAW)
    for ch in range(pokey_channels):
        c.lda_absx(c.labels[f'audc{ch+1}_tab']); c.sta_abs(audc_regs[ch])

    # --- Read NEXT sample ---
    c.ldx_zp(ZP_BANK_IDX)
    c.lda_absx(c.labels['portb_table']); c.sta_abs(PORTB)
    c.ldy_imm(0x00)
    c.lda_indy(ZP_SAMPLE_PTR)
    c.sta_zp(ZP_CACHED_RAW)
    c.lda_imm(PORTB_MAIN); c.sta_abs(PORTB)

    c.inc_zp(ZP_SAMPLE_PTR); c.bne('irq_check'); c.inc_zp(ZP_SAMPLE_PTR + 1)

    c.label('irq_check')
    c.lda_zp(ZP_SAMPLE_PTR + 1); c.cmp_imm(0x80); c.bcc('irq_exit')
    c.inc_zp(ZP_BANK_IDX)
    c.lda_zp(ZP_BANK_IDX); c.cmp_imm(n_banks); c.bcs('irq_done')
    c.lda_imm(0x00); c.sta_zp(ZP_SAMPLE_PTR)
    c.lda_imm(0x40); c.sta_zp(ZP_SAMPLE_PTR + 1)
    c.jmp('irq_exit')

    c.label('irq_done')
    c.lda_imm(0x00); c.sta_zp(ZP_PLAYING)
    c.lda_imm(0x10)
    for ch in range(pokey_channels):
        c.sta_abs(audc_regs[ch])

    c.label('irq_exit')
    c.ldx_zp(ZP_IRQ_SAVE_X); c.lda_zp(ZP_IRQ_SAVE_A)
    c.rti()




# ======================================================================
# VQ mode player (per-bank codebook, inspired by PokeyVQ)
# ======================================================================

def build_vq_player(pokey_divisor, audctl, n_banks, portb_table, stereo,
                    pokey_channels=2, vec_size=8, sample_rate=8000):
    """Build VQ player with per-bank codebook.

    Architecture (from PokeyVQ tracker_irq_speed.asm):
      - Codebook (256 * vec_size) copied to main RAM at $8000
      - VQ_LO/VQ_HI tables map index -> vector address in codebook
      - IRQ reads samples from main-RAM codebook (no bank switch!)
      - Every vec_size IRQs: bank-in, read 1 index byte, bank-out
      - Bank transition: main loop copies new codebook (SEI, ~10ms)
    """
    assert vec_size in (4, 8, 16)
    cb_bytes = 256 * vec_size
    idx_start_lo = (BANK_BASE + cb_bytes) & 0xFF
    idx_start_hi = ((BANK_BASE + cb_bytes) >> 8) & 0xFF

    c = _CodeBuilder(CODE_BASE)
    audc_regs = [AUDC1, AUDC2, AUDC3, AUDC4]

    # ── NMI handler ──
    c.label('nmi_handler'); c.rti()

    # ── PORTB table ──
    c.label('portb_table')
    for _ in range(64): c.emit(PORTB_MAIN)  # placeholder — filled by copy_det

    # ── AUDC lookup tables (256 bytes × pokey_channels) ──
    max_lvl = max_level(pokey_channels)
    for ch in range(pokey_channels):
        c.label(f'audc{ch+1}_tab')
        for idx in range(max_lvl + 1):
            vols = index_to_volumes(idx, pokey_channels)
            c.emit(vols[ch] | 0x10)
        for _ in range(256 - (max_lvl + 1)):
            c.emit(0x10)

    # ── VQ_LO / VQ_HI tables (256 bytes each) ──
    # codebook_index → address of vector in main RAM at VQ_CB_BASE
    c.label('vq_lo_tab')
    for i in range(256):
        c.emit((VQ_CB_BASE + i * vec_size) & 0xFF)
    c.label('vq_hi_tab')
    for i in range(256):
        c.emit(((VQ_CB_BASE + i * vec_size) >> 8) & 0xFF)

    # ── Splash screen ──
    _emit_splash(c, pokey_channels, sample_rate, 'vq', vec_size, n_banks=n_banks)

    # ══════════════════════════════════════════════════════════════
    # PLAY INIT (entered from splash on SPACE press, SEI already done)
    # ══════════════════════════════════════════════════════════════
    c.label('play_init')
    c.lda_imm_lo('irq_handler'); c.sta_abs(0xFFFE)
    c.lda_imm_hi('irq_handler'); c.sta_abs(0xFFFF)

    # Copy runtime-detected bank values into portb_table
    _emit_copy_detected_banks(c, n_banks)

    # Init ZP
    c.lda_imm(0x00)
    c.sta_zp(ZP_VQ_BANK_IDX)
    c.sta_zp(ZP_VQ_VEC_POS)
    c.sta_zp(ZP_VQ_NEED_BANK)

    # Copy first codebook to $8000
    c.jsr('copy_codebook')

    # Set idx_ptr to start of index stream
    c.lda_imm(idx_start_lo); c.sta_zp(ZP_VQ_IDX_PTR)
    c.lda_imm(idx_start_hi); c.sta_zp(ZP_VQ_IDX_PTR + 1)

    # Read first index byte, set up vec_ptr
    c.ldx_zp(ZP_VQ_BANK_IDX)
    c.lda_absx(c.labels['portb_table']); c.sta_abs(PORTB)
    c.ldy_imm(0x00)
    c.lda_indy(ZP_VQ_IDX_PTR)   # first index byte
    c.tax()
    c.lda_imm(PORTB_MAIN); c.sta_abs(PORTB)
    c.lda_absx(c.labels['vq_lo_tab']); c.sta_zp(ZP_VQ_VEC_PTR)
    c.lda_absx(c.labels['vq_hi_tab']); c.sta_zp(ZP_VQ_VEC_PTR + 1)
    c.inc_zp(ZP_VQ_IDX_PTR); c.bne('prime_nc'); c.inc_zp(ZP_VQ_IDX_PTR + 1)
    c.label('prime_nc')

    # Cache first sample
    c.ldy_imm(0x00)
    c.lda_indy(ZP_VQ_VEC_PTR)
    c.sta_zp(ZP_VQ_CACHED)
    c.lda_imm(0x01); c.sta_zp(ZP_VQ_VEC_POS)

    c.lda_imm(0xFF); c.sta_zp(ZP_VQ_PLAYING)

    # Silence POKEY
    c.lda_imm(0x10)
    for ch in range(pokey_channels):
        c.sta_abs(audc_regs[ch])

    c.jsr('pokey_setup')
    c.cli()

    # ══════════════════════════════════════════════════════════════
    # MAIN LOOP: handle bank transitions
    # ══════════════════════════════════════════════════════════════
    c.label('main_loop')
    c.lda_zp(ZP_VQ_PLAYING); c.beq('song_ended')
    c.lda_zp(ZP_VQ_NEED_BANK); c.beq('main_loop')

    # Bank switch requested
    c.sei()
    c.lda_imm(0x00); c.sta_zp(ZP_VQ_NEED_BANK)
    c.inc_zp(ZP_VQ_BANK_IDX)
    c.lda_zp(ZP_VQ_BANK_IDX); c.cmp_imm(n_banks); c.bcs('stop_play')

    # Copy new codebook
    c.jsr('copy_codebook')

    # Reset idx_ptr
    c.lda_imm(idx_start_lo); c.sta_zp(ZP_VQ_IDX_PTR)
    c.lda_imm(idx_start_hi); c.sta_zp(ZP_VQ_IDX_PTR + 1)

    c.cli()
    c.jmp('main_loop')

    c.label('stop_play')
    c.lda_imm(0x00)
    c.sta_zp(ZP_VQ_PLAYING)
    c.cli()

    c.label('song_ended')
    c.jmp('return_to_idle')

    # ── Return to idle ──
    _emit_return_to_idle(c, pokey_channels)

    # ══════════════════════════════════════════════════════════════
    # COPY_CODEBOOK: copy 256*vec_size bytes from bank $4000→$8000
    # ══════════════════════════════════════════════════════════════
    c.label('copy_codebook')
    c.ldx_zp(ZP_VQ_BANK_IDX)
    c.lda_absx(c.labels['portb_table']); c.sta_abs(PORTB)

    n_pages = cb_bytes // 256
    c.ldx_imm(0x00)
    c.label('copy_loop')
    for page in range(n_pages):
        src = (0x40 + page) * 256
        dst = (0x80 + page) * 256
        c.lda_absx(src); c.sta_absx(dst)
    c.inx()
    c.bne('copy_loop')

    c.lda_imm(PORTB_MAIN); c.sta_abs(PORTB)
    c.rts()

    # ══════════════════════════════════════════════════════════════
    # POKEY SETUP
    # ══════════════════════════════════════════════════════════════
    _emit_pokey_setup(c, pokey_divisor, audctl, stereo,
                      ZP_VQ_PLAYING, ZP_VQ_SAVE_A, ZP_VQ_SAVE_X,
                      pokey_channels=pokey_channels)

    # ══════════════════════════════════════════════════════════════
    # IRQ HANDLER
    # ══════════════════════════════════════════════════════════════
    _emit_irq_vq(c, n_banks, pokey_channels, vec_size)

    return c.resolve(), CODE_BASE, c.labels['start']


def _emit_irq_vq(c, n_banks, pokey_channels, vec_size):
    """Emit VQ IRQ handler.

    Fast path (vec_size-1 of every vec_size IRQs):
      - Write cached AUDC values to POKEY
      - Read next sample from main-RAM codebook
      - ~45 cycles

    Boundary path (1 of every vec_size IRQs):
      - Write cached AUDC values
      - Bank-in, read 1 index byte, bank-out
      - Update vec_ptr via VQ_LO/VQ_HI lookup
      - ~80 cycles
    """
    audc_regs = [AUDC1, AUDC2, AUDC3, AUDC4]

    c.label('irq_handler')
    c.sta_zp(ZP_VQ_SAVE_A); c.stx_zp(ZP_VQ_SAVE_X)

    # ACK IRQ
    c.lda_imm(0x00); c.sta_abs(IRQEN)
    c.lda_imm(IRQ_MASK); c.sta_abs(IRQEN)
    c.lda_zp(ZP_VQ_PLAYING); c.beq('vq_irq_exit')

    # ── FIXED-TIMING OUTPUT: write cached AUDC ──
    c.ldx_zp(ZP_VQ_CACHED)
    for ch in range(pokey_channels):
        c.lda_absx(c.labels[f'audc{ch+1}_tab'])
        c.sta_abs(audc_regs[ch])

    # ── Read next sample from codebook (MAIN RAM, no bank switch!) ──
    c.ldy_zp(ZP_VQ_VEC_POS)
    c.lda_indy(ZP_VQ_VEC_PTR)    # LDA (vec_ptr),Y — always accessible
    c.sta_zp(ZP_VQ_CACHED)        # cache for next IRQ
    c.iny()
    c.cpy_imm(vec_size)
    c.bcs('vq_new_vector')
    c.sty_zp(ZP_VQ_VEC_POS)
    # Fall through to exit (fast path)

    c.label('vq_irq_exit')
    c.ldx_zp(ZP_VQ_SAVE_X); c.lda_zp(ZP_VQ_SAVE_A)
    c.rti()

    # ── Vector boundary: advance index stream ──
    c.label('vq_new_vector')
    c.ldy_imm(0x00)
    c.sty_zp(ZP_VQ_VEC_POS)

    # Read next index byte from banked memory
    c.ldx_zp(ZP_VQ_BANK_IDX)
    c.lda_absx(c.labels['portb_table']); c.sta_abs(PORTB)
    c.lda_indy(ZP_VQ_IDX_PTR)    # Y=0, read index byte
    c.tax()                        # X = codebook index
    c.lda_imm(PORTB_MAIN); c.sta_abs(PORTB)

    # Update vec_ptr from VQ_LO/VQ_HI tables
    c.lda_absx(c.labels['vq_lo_tab']); c.sta_zp(ZP_VQ_VEC_PTR)
    c.lda_absx(c.labels['vq_hi_tab']); c.sta_zp(ZP_VQ_VEC_PTR + 1)

    # Advance idx_ptr
    c.inc_zp(ZP_VQ_IDX_PTR); c.bne('vq_idx_nc')
    c.inc_zp(ZP_VQ_IDX_PTR + 1)
    c.label('vq_idx_nc')

    # Check if bank exhausted (idx_ptr reached $8000)
    c.lda_zp(ZP_VQ_IDX_PTR + 1)
    c.cmp_imm(0x80)
    c.bcc('vq_irq_exit')

    # Bank exhausted — signal main loop
    c.lda_imm(0xFF); c.sta_zp(ZP_VQ_NEED_BANK)
    c.jmp('vq_irq_exit')
