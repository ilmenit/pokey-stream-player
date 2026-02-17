"""Generate MADS-compatible assembly project with in-IRQ LZ decoder.

Produces a project directory with:
  stream_player.asm  -- Master file (includes everything)
  config.icl         -- Per-song constants (NUM_BANKS, divisor, AUDCTL)
  tables.icl         -- PORTB table + AUDC lookup tables
  player.icl         -- IRQ state machine, POKEY setup, bank init
  banks.icl          -- INI stubs for loading each bank
  bank_00.bin ...    -- Compressed bank data files
  build.sh / .bat    -- Build scripts
"""

import os

from .tables import pack_dual_byte, index_to_volumes, max_level
from .layout import DBANK_TABLE, TAB_MEM_BANKS


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


def generate_asm_project(output_dir, banks, portb_table, divisor, audctl,
                         stereo, compressed, actual_rate, duration,
                         source_name="", pokey_channels=4):
    """Generate complete MADS assembly project."""
    os.makedirs(output_dir, exist_ok=True)

    n_banks = len(banks)

    # Write bank binary files
    for i, bank_data in enumerate(banks):
        path = os.path.join(output_dir, f"bank_{i:02d}.bin")
        with open(path, 'wb') as f:
            f.write(bank_data)

    # Write all assembly files
    _write_config(output_dir, n_banks, divisor, audctl, actual_rate,
                  source_name, duration, stereo, compressed)
    _write_tables(output_dir, portb_table, pokey_channels)
    _write_player(output_dir, compressed, stereo)
    _write_banks(output_dir, n_banks, portb_table)
    _write_mem_detect(output_dir)
    _write_main(output_dir, compressed, stereo, source_name, actual_rate,
                duration, n_banks, pokey_channels)
    _write_build_scripts(output_dir)

    return os.path.join(output_dir, "stream_player.asm")


def _write_config(output_dir, n_banks, divisor, audctl, actual_rate,
                  source_name, duration, stereo, compressed):
    """Write config.icl with per-song constants."""
    clk = "1.77MHz ch1" if (audctl & 0x40) else "64kHz base"
    mode = "DeltaLZ in-IRQ" if compressed else "RAW"
    ch = "stereo" if stereo else "mono"
    dur_m = int(duration) // 60
    dur_s = int(duration) % 60

    with open(os.path.join(output_dir, "config.icl"), 'w', newline='\n') as f:
        f.write(f"""\
; ==========================================================================
; config.icl -- Hardware registers, zero page, player constants
; ==========================================================================
; Source: {source_name}
; Mode: {mode}, {ch}
; Rate: {actual_rate:.1f} Hz (divisor ${divisor:02X})
; Duration: {dur_m}:{dur_s:02d}
; Banks: {n_banks}
; ==========================================================================

; --- POKEY ---
AUDF1   = $D200
AUDC1   = $D201
AUDF2   = $D202
AUDC2   = $D203
AUDF3   = $D204
AUDC3   = $D205
AUDF4   = $D206
AUDC4   = $D207
AUDCTL  = $D208
STIMER  = $D209
IRQEN   = $D20E
SKCTL   = $D20F

; --- GTIA / ANTIC ---
DMACTL  = $D400
DLISTL  = $D402
DLISTH  = $D403
NMIEN   = $D40E

; --- PIA ---
PORTB   = $D301

; --- Runtime bank detection ---
TAB_MEM_BANKS = $0480   ; 65 bytes: detected bank PORTB values
""")
        if stereo:
            f.write("""
; --- Second POKEY ($D210) ---
AUDF1_2 = $D210
AUDC1_2 = $D211
AUDC2_2 = $D213
AUDC3_2 = $D215
AUDC4_2 = $D217
AUDCTL2 = $D218
SKCTL2  = $D21F
IRQEN2  = $D21E
""")

        f.write(f"""
; --- Player configuration ---
NUM_BANKS       = {n_banks}
POKEY_DIVISOR   = ${divisor:02X}       ; -> {actual_rate:.1f} Hz
AUDCTL_VAL      = ${audctl:02X}       ; {clk}
IRQ_MASK        = $01       ; Timer 1 IRQ
PORTB_MAIN      = $FE       ; Main RAM, OS ROM disabled
""")

        if compressed:
            f.write("""
; --- Zero page (in-IRQ LZ decoder) ---
lz_src          = $80       ; 2 bytes -- read ptr in bank ($4000+)
lz_dst          = $82       ; 2 bytes -- write ptr in buffer ($8000+)
lz_count        = $84       ; 1 byte  -- bytes remaining in run
lz_match        = $85       ; 2 bytes -- match source ptr (in buffer)
lz_mode         = $87       ; 1 byte  -- 0=token, 1=literal, 2=match
delta_acc       = $88       ; 1 byte  -- running delta accumulator
bank_idx        = $89       ; 1 byte  -- current bank
playing         = $8A       ; 1 byte  -- $FF=playing, $00=stopped
irq_save_a      = $8B       ; 1 byte
irq_save_x      = $8C       ; 1 byte
""")
        else:
            f.write("""
; --- Zero page (RAW mode) ---
sample_ptr      = $80       ; 2 bytes -- IRQ read position
bank_idx        = $82       ; 1 byte  -- current bank index
playing         = $83       ; 1 byte  -- $FF=playing, $00=stopped
irq_save_a      = $84       ; 1 byte
irq_save_x      = $85       ; 1 byte
cached_sample   = $86       ; 1 byte  -- cached sample for fixed-timing output
""")
            if stereo:
                f.write("stash_left      = $91       ; 1 byte  -- left byte stash\n")


def _write_tables(output_dir, portb_table, pokey_channels=4):
    """Write tables.icl with PORTB and AUDC lookup tables."""
    with open(os.path.join(output_dir, "tables.icl"), 'w', newline='\n') as f:
        f.write("""\
; ==========================================================================
; tables.icl -- Lookup tables
; ==========================================================================

; --- PORTB values for extended memory banks ---
; Placeholder — filled at runtime from TAB_MEM_BANKS (detected by mem_detect)
portb_table:
""")
        for i in range(0, 64, 8):
            vals = ",".join(["$FE"] * 8)
            f.write(f"    .byte {vals}\n")

        max_lvl = max_level(pokey_channels)
        for ch in range(pokey_channels):
            ch_name = f"audc{ch+1}_tab"
            f.write(f"""
; --- AUDC{ch+1}: index 0-{max_lvl} -> vol{ch+1} | $10, padded to 256 ---
{ch_name}:
""")
            tab = []
            for idx in range(max_lvl + 1):
                vols = index_to_volumes(idx, pokey_channels)
                tab.append(vols[ch] | 0x10)
            tab += [0x10] * (256 - (max_lvl + 1))
            for i in range(0, 256, 16):
                vals = ",".join(f"${v:02X}" for v in tab[i:i+16])
                f.write(f"    .byte {vals}\n")


def _write_player(output_dir, compressed, stereo):
    """Write player.icl -- IRQ handler, POKEY setup, init."""
    path = os.path.join(output_dir, "player.icl")
    with open(path, 'w', newline='\n') as f:
        if compressed:
            _write_player_compressed(f, stereo)
        else:
            _write_player_raw(f, stereo)


def _write_player_compressed(f, stereo):
    """Write in-IRQ LZ decoder player (buffer-aware, no match wrap)."""
    f.write("""\
; ==========================================================================
; player.icl -- In-IRQ LZ decoder, buffer-aware compression
; ==========================================================================
;
; Each IRQ: decode one LZ byte, delta-accumulate, write AUDC via table.
; State machine: 0=token fetch, 1=literal run, 2=match copy.
;
; Decode buffer: $8000-$BFFF (16 KB). The COMPRESSOR guarantees:
;   - No literal or match copy straddles the buffer boundary
;   - No match offset reaches outside valid buffer range
;   - lz_match never needs wrap checking
;
; lz_dst wraps from $C000 to $8000 only at token boundaries (rare).
; lz_match NEVER wraps — saves ~8 cycles on every match byte.
;
; Bank header: 1 byte (delta_acc). Source starts at $4001.
; PORTB AND #$FE keeps OS ROM off -> no SEI needed.
; ==========================================================================

BUF_START_HI = $80
BUF_END_HI   = $C0

nmi_handler:
    rti

irq_handler:
    sta irq_save_a
    stx irq_save_x
    lda #0
    sta IRQEN
    lda #IRQ_MASK
    sta IRQEN
    lda playing
    beq @exit

    ; Mode dispatch via LSR
    lda lz_mode
    lsr
    bcs @literal_byte               ; mode 1 (hot path)
    bne @go_match                   ; mode 2
    jmp @need_token                 ; mode 0
@go_match:
    jmp @match_byte

; --- MODE 1: Literal (fast path, falls through to @output) ---
@literal_byte:
    ldy #0
    lda (lz_src),y
    sta (lz_dst),y
    inc lz_src
    bne @output
    inc lz_src+1

; --- Common output ---
@output:
    clc
    adc delta_acc
    sta delta_acc
    tax
    lda audc1_tab,x
    sta AUDC1
    lda audc2_tab,x
    sta AUDC2
    lda audc3_tab,x
    sta AUDC3
    lda audc4_tab,x
    sta AUDC4
    ; Advance lz_dst (wrap check only on page cross)
    inc lz_dst
    bne @dst_ok
    inc lz_dst+1
    lda lz_dst+1
    cmp #BUF_END_HI
    bcc @dst_ok
    lda #BUF_START_HI               ; wrap $C000 -> $8000
    sta lz_dst+1
@dst_ok:
    dec lz_count
    bne @exit
    lda #0
    sta lz_mode
@exit:
    ldx irq_save_x
    lda irq_save_a
    rti

; --- MODE 0: Token fetch ---
@need_token:
    ldy #0
    lda (lz_src),y
    bne @token_not_zero
    jmp @end_of_block
@token_not_zero:
    inc lz_src
    bne @+
    inc lz_src+1
@:
    cmp #$80
    bcs @token_match
    ; Literal token
    sta lz_count
    lda #1
    sta lz_mode
    jmp @literal_byte

@token_match:
    tax
    and #$3F
    clc
    adc #3
    sta lz_count
    cpx #$C0
    bcs @long_match

    ; Short match: 1-byte offset
    ldy #0
    lda (lz_src),y
    sta lz_match
    inc lz_src
    bne @+
    inc lz_src+1
@:
    ; match = lz_dst - offset (no wrap: compressor guarantees >= $8000)
    sec
    lda lz_dst
    sbc lz_match
    sta lz_match
    lda lz_dst+1
    sbc #0
    sta lz_match+1
    lda #2
    sta lz_mode
    jmp @match_byte

    ; Long match: 2-byte offset (LE)
@long_match:
    ldy #0
    lda (lz_src),y
    sta lz_match
    iny
    lda (lz_src),y
    sta lz_match+1
    clc
    lda lz_src
    adc #2
    sta lz_src
    bcc @+
    inc lz_src+1
@:
    ; match = lz_dst - offset (no wrap needed)
    sec
    lda lz_dst
    sbc lz_match
    sta lz_match
    lda lz_dst+1
    sbc lz_match+1
    sta lz_match+1
    lda #2
    sta lz_mode
    ; fall through

; --- MODE 2: Match copy (no wrap on lz_match!) ---
@match_byte:
    ldy #0
    lda (lz_match),y
    sta (lz_dst),y
    ; Advance lz_match -- no wrap check needed
    inc lz_match
    bne @+
    inc lz_match+1
@:
    jmp @output

; --- End of block ---
@end_of_block:
    inc bank_idx
    lda bank_idx
    cmp #NUM_BANKS
    bcs @finished
    ldx bank_idx
    lda portb_table,x
    and #$FE
    sta PORTB
    lda $4000
    sta delta_acc
    lda #$01
    sta lz_src
    lda #$40
    sta lz_src+1
    ; lz_dst NOT reset — continuous across banks
    jmp @exit

@finished:
    lda #0
    sta playing
    lda #$10
    sta AUDC1
    sta AUDC2
    sta AUDC3
    sta AUDC4
    jmp @exit

; ==========================================================================
pokey_setup:
    lda #0
    sta IRQEN
    sta SKCTL
    lda #$10
    sta AUDC1
    sta AUDC2
    sta AUDC3
    sta AUDC4
    lda #POKEY_DIVISOR
    sta AUDF1
    lda #AUDCTL_VAL
    sta AUDCTL
    lda #$03
    sta SKCTL
""")
    if stereo:
        f.write("""\
    lda #0
    sta IRQEN2
    sta SKCTL2
    lda #$10
    sta AUDC1_2
    sta AUDC2_2
    sta AUDC3_2
    sta AUDC4_2
    lda #POKEY_DIVISOR
    sta AUDF1_2
    lda #AUDCTL_VAL
    sta AUDCTL2
    lda #$03
    sta SKCTL2
""")
    f.write("""\
    lda #IRQ_MASK
    sta IRQEN
    lda #0
    sta STIMER
    rts

; ==========================================================================
init_first_bank:
    lda portb_table
    and #$FE
    sta PORTB
    lda $4000
    sta delta_acc
    lda #$01
    sta lz_src
    lda #$40
    sta lz_src+1
    lda #$00
    sta lz_dst
    lda #BUF_START_HI
    sta lz_dst+1
    lda #0
    sta lz_mode
    sta lz_count
    sta bank_idx
    lda #$FF
    sta playing
    rts
""")

def _write_player_raw(f, stereo):
    """Write RAW mode player (write-first-then-read, 4ch 61-level)."""
    f.write("""\
; ==========================================================================
; player.icl -- RAW mode: write-first-then-read, 4ch 61-level
;
; Architecture: AUDC writes happen FIRST from cached sample (fixed timing),
; then the NEXT sample is read from banked memory and cached for next IRQ.
; This eliminates timing jitter in the analog output.
; ==========================================================================

nmi_handler:
    rti

irq_handler:
    sta irq_save_a
    stx irq_save_x

    lda #0
    sta IRQEN
    lda #IRQ_MASK
    sta IRQEN

    lda playing
    beq @exit

    ; --- FIXED-TIMING OUTPUT: write cached sample (always at same cycle) ---
    ldx cached_sample
    lda audc1_tab,x
    sta AUDC1
    lda audc2_tab,x
    sta AUDC2
    lda audc3_tab,x
    sta AUDC3
    lda audc4_tab,x
    sta AUDC4

    ; --- Read NEXT sample (timing no longer matters) ---
    ldx bank_idx
    lda portb_table,x
    sta PORTB

    ldy #0
    lda (sample_ptr),y
    sta cached_sample        ; cache for next IRQ

    lda #PORTB_MAIN
    sta PORTB

    inc sample_ptr
    bne @check
    inc sample_ptr+1

@check:
    lda sample_ptr+1
    cmp #$80
    bcc @exit

    inc bank_idx
    lda bank_idx
    cmp #NUM_BANKS
    bcs @finished

    lda #$00
    sta sample_ptr
    lda #$40
    sta sample_ptr+1
    jmp @exit

@finished:
    lda #0
    sta playing
    lda #$10
    sta AUDC1
    sta AUDC2
    sta AUDC3
    sta AUDC4

@exit:
    ldx irq_save_x
    lda irq_save_a
    rti

; ==========================================================================
; POKEY initialization
; ==========================================================================

pokey_setup:
    lda #0
    sta IRQEN
    sta SKCTL
    lda #$10
    sta AUDC1
    sta AUDC2
    sta AUDC3
    sta AUDC4
    lda #POKEY_DIVISOR
    sta AUDF1
    lda #AUDCTL_VAL
    sta AUDCTL
    lda #$03
    sta SKCTL
""")
    if stereo:
        f.write("""\
    lda #0
    sta IRQEN2
    sta SKCTL2
    lda #$10
    sta AUDC1_2
    sta AUDC2_2
    sta AUDC3_2
    sta AUDC4_2
    lda #POKEY_DIVISOR
    sta AUDF1_2
    lda #AUDCTL_VAL
    sta AUDCTL2
    lda #$03
    sta SKCTL2
""")
    f.write("""\
    lda #IRQ_MASK
    sta IRQEN
    lda #0
    sta STIMER
    rts
""")


def _write_banks(output_dir, n_banks, portb_table):
    """Write banks.icl with INI stubs for each bank."""
    with open(os.path.join(output_dir, "banks.icl"), 'w', newline='\n') as f:
        f.write(f"""\
; ==========================================================================
; banks.icl -- Bank data segments ({n_banks} banks)
;
; Each stub reads the runtime-detected PORTB value from TAB_MEM_BANKS
; (populated by mem_detect INI) and switches to that bank before loading.
; ==========================================================================

""")
        for i in range(n_banks):
            tmb_offset = i + 1  # +1 because entry 0 = main RAM
            f.write(f"; --- Bank {i} (TAB_MEM_BANKS+{tmb_offset}) ---\n")
            f.write(f"    org bank_switch_stub\n")
            f.write(f"    lda TAB_MEM_BANKS+{tmb_offset}\n")
            f.write(f"    sta PORTB\n")
            f.write(f"    rts\n")
            f.write(f"    ini bank_switch_stub\n")
            f.write(f"    org $4000\n")
            f.write(f"    ins 'bank_{i:02d}.bin'\n\n")


def _write_mem_detect(output_dir):
    """Write mem_detect.icl — runtime extended memory bank detection."""
    with open(os.path.join(output_dir, "mem_detect.icl"), 'w', newline='\n') as f:
        f.write("""\
; ==========================================================================
; mem_detect.icl -- Runtime detection of extended memory banks
; ==========================================================================
; Implements the @MEM_DETECT algorithm:
;   Phase 1: Save $7FFF from each of 64 possible bank codes
;   Phase 2: Write PORTB code as signature to $7FFF in each bank
;   Phase 3: Write $FF sentinel to main RAM $7FFF
;   Phase 4: Read back — codes whose signature survived are unique banks
;   Phase 5: Restore original $7FFF values
;
; Result: TAB_MEM_BANKS filled with:
;   +0: $FF (main memory)
;   +1: PORTB value for first detected extended bank
;   +2: PORTB value for second detected extended bank
;   ...
; ==========================================================================

    org bank_switch_stub

mem_detect:
    sei
    lda #0
    sta $D40E               ; disable NMI

    lda PORTB
    pha                     ; save original PORTB

    ; Zero-fill TAB_MEM_BANKS
    lda #0
    ldx #64
@zfill:
    sta TAB_MEM_BANKS,x
    dex
    bpl @zfill

    ; Phase 1: Save $7FFF from each bank
    ldx #63
@p1:
    lda @dbank,x
    sta PORTB
    lda $7FFF
    sta @saved,x
    dex
    bpl @p1

    ; Phase 2: Write signatures
    ldx #63
@p2:
    lda @dbank,x
    sta PORTB
    sta $7FFF               ; signature = PORTB code
    dex
    bpl @p2

    ; Phase 3: Main RAM sentinel
    pla                     ; original PORTB
    ora #$11                ; ensure ROM on + main RAM (bit4=1)
    pha
    sta PORTB
    lda #$FF
    sta $7FFF               ; main RAM sentinel
    sta TAB_MEM_BANKS       ; entry 0 = main

    ; Phase 4: Verify
    ldy #1                  ; output index
    ldx #63
@p4:
    lda @dbank,x
    sta PORTB
    cmp $7FFF
    bne @skip
    sta TAB_MEM_BANKS,y
    iny
@skip:
    dex
    bpl @p4

    ; Phase 5: Restore saved $7FFF
    ldx #63
@p5:
    lda @dbank,x
    sta PORTB
    lda @saved,x
    sta $7FFF
    dex
    bpl @p5

    ; Restore
    pla
    sta PORTB
    lda #$40
    sta $D40E               ; re-enable VBI
    cli
    rts

; --- Probe table (64 entries) ---
@dbank:
""")
        # Write DBANK_TABLE as .byte directives
        for i in range(0, 64, 8):
            chunk = DBANK_TABLE[i:i+8]
            vals = ",".join(f"${v:02X}" for v in chunk)
            f.write(f"    .byte {vals}\n")

        f.write("""
; --- Saved $7FFF values (64 bytes) ---
@saved:
    .ds 64

    ini mem_detect
""")


def _write_main(output_dir, compressed, stereo, source_name, actual_rate,
                duration, n_banks, pokey_channels=4):
    """Write stream_player.asm master file."""
    mode = "In-IRQ DeltaLZ" if compressed else "RAW"
    ch = "stereo (dual POKEY)" if stereo else "mono (4-channel)"
    dur_m = int(duration) // 60
    dur_s = int(duration) % 60
    ram_kb = n_banks * 16 + 64
    compress_tag = "DELTALZ" if compressed else "RAW"

    # Generate screen codes for error/info text
    # Generate screen codes for error display (two contiguous lines)
    err_title = "STREAM PLAYER".center(40)
    err_msg = f"ERROR: {ram_kb}KB MEMORY REQUIRED".center(40)
    err_title_codes = _to_screen_codes(err_title)
    err_msg_codes = _to_screen_codes(err_msg)

    def _fmt_bytes(codes):
        """Format screen codes as MADS .byte directives (8 per line)."""
        lines = []
        for i in range(0, len(codes), 8):
            chunk = codes[i:i+8]
            vals = ",".join(f"${v:02X}" for v in chunk)
            lines.append(f"    .byte {vals}")
        return "\n".join(lines)

    err_title_bytes = _fmt_bytes(err_title_codes)
    err_msg_bytes = _fmt_bytes(err_msg_codes)

    with open(os.path.join(output_dir, "stream_player.asm"), 'w', newline='\n') as f:
        f.write(f"""\
; ==========================================================================
; STREAM PLAYER -- {mode}, {ch}
; ==========================================================================
; Source: {source_name}
; Rate: {actual_rate:.1f} Hz
; Duration: {dur_m}:{dur_s:02d}
; Banks: {n_banks}
; RAM required: {ram_kb}KB
; Assembler: MADS
; ==========================================================================

    icl 'config.icl'

; --- Bank switch stub area ($0600, XEX loading only) ---
; banks.icl writes complete LDA/STA/RTS routines here.

bank_switch_stub = $0600

; --- Player code ($2000) ---

    org $2000

    icl 'tables.icl'
    icl 'player.icl'

; --- Entry point ---

start:
    sei
    cld
    lda #0
    sta NMIEN
    sta IRQEN
    sta DMACTL
    lda #PORTB_MAIN
    sta PORTB

    lda #<irq_handler
    sta $FFFE
    lda #>irq_handler
    sta $FFFF
    lda #<nmi_handler
    sta $FFFA
    lda #>nmi_handler
    sta $FFFB

    ; Copy runtime-detected bank values into portb_table
    ldx #{n_banks-1}
copy_det:
    lda TAB_MEM_BANKS+1,x
    sta portb_table,x
    dex
    bpl copy_det

    ; Memory check: verify enough extended banks detected
    lda TAB_MEM_BANKS+{n_banks}
    bne mem_ok
    jmp mem_error
mem_ok:

""")
        if compressed:
            f.write("""\
    jsr init_first_bank
    jsr pokey_setup
    cli

main_loop:
    lda playing
    bne main_loop
""")
        else:
            f.write("""\
    lda #0
    sta bank_idx
    sta sample_ptr
    lda #$40
    sta sample_ptr+1
    lda #$FF
    sta playing

    jsr pokey_setup
    cli

main_loop:
    lda playing
    bne main_loop
""")

        f.write(f"""\

    sei
    lda #0
    sta IRQEN
    lda #$10
    sta AUDC1
    sta AUDC2
    sta AUDC3
    sta AUDC4
halt:
    jmp halt

; --- Memory error display ---
; Shown when machine has fewer extended banks than song requires.

mem_error:
    lda #$FF
    sta PORTB               ; ROM on (need charset at $E000)
    lda #<error_dlist
    sta DLISTL
    lda #>error_dlist
    sta DLISTH
    lda #$22
    sta DMACTL              ; enable DL DMA + playfield
    lda #$40
    sta NMIEN               ; VBI only (for JVB)
    lda #$3E
    sta $D017               ; COLPF1 = bright red
    lda #0
    sta $D018               ; COLPF2 = black
    lda #$30
    sta $D01A               ; COLBK = red
    cli
error_halt:
    jmp error_halt

    ; Display list: 8 blank lines, then 2 text lines (Mode 2)
error_dlist:
    .byte $70,$70,$70,$70,$70,$70,$70,$70
    .byte $42               ; Mode 2 + LMS
    .word error_title
    .byte $02               ; Mode 2 (line 2 = error_msg, contiguous)
    .byte $41               ; JVB
    .word error_dlist

    ; Title (40 bytes) + error message (40 bytes), contiguous for display list
error_title:
{err_title_bytes}
error_msg:
{err_msg_bytes}

; --- Memory detection (runs as first INI, before bank loading) ---

    icl 'mem_detect.icl'

; --- Bank data ---

    icl 'banks.icl'

; --- Restore main RAM after bank loading ---

    org bank_switch_stub
    lda #PORTB_MAIN
    sta PORTB
    rts
    ini bank_switch_stub

    run start
""")


def _write_build_scripts(output_dir):
    """Write build.sh and build.bat."""
    sh_path = os.path.join(output_dir, "build.sh")
    with open(sh_path, 'w', newline='\n') as f:
        f.write("#!/bin/sh\nmads stream_player.asm -o:stream_player.xex\n")
    os.chmod(sh_path, 0o755)

    bat_path = os.path.join(output_dir, "build.bat")
    with open(bat_path, 'w', newline='\r\n') as f:
        f.write("@echo off\nmads stream_player.asm -o:stream_player.xex\nif errorlevel 1 pause\n")
