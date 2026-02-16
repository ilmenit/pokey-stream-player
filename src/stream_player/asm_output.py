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
from .layout import DBANK_TABLE


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
    _write_main(output_dir, compressed, stereo, source_name, actual_rate,
                duration, n_banks)
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
portb_table:
""")
        for i in range(0, len(portb_table), 8):
            chunk = portb_table[i:i+8]
            vals = ",".join(f"${v:02X}" for v in chunk)
            f.write(f"    .byte {vals}\n")

        remaining = 64 - len(portb_table)
        if remaining > 0:
            for i in range(0, remaining, 8):
                chunk_size = min(8, remaining - i)
                vals = ",".join(["$FE"] * chunk_size)
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
; ==========================================================================

""")
        for i in range(n_banks):
            pv = portb_table[i]
            f.write(f"; --- Bank {i} (PORTB=${pv:02X}) ---\n")
            f.write(f"    org bank_switch_val\n")
            f.write(f"    .byte ${pv:02X}\n")
            f.write(f"    ini bank_switch_stub\n")
            f.write(f"    org $4000\n")
            f.write(f"    ins 'bank_{i:02d}.bin'\n\n")


def _write_main(output_dir, compressed, stereo, source_name, actual_rate,
                duration, n_banks):
    """Write stream_player.asm master file."""
    mode = "In-IRQ DeltaLZ" if compressed else "RAW"
    ch = "stereo (dual POKEY)" if stereo else "mono (4-channel)"
    dur_m = int(duration) // 60
    dur_s = int(duration) % 60

    with open(os.path.join(output_dir, "stream_player.asm"), 'w', newline='\n') as f:
        f.write(f"""\
; ==========================================================================
; STREAM PLAYER -- {mode}, {ch}
; ==========================================================================
; Source: {source_name}
; Rate: {actual_rate:.1f} Hz
; Duration: {dur_m}:{dur_s:02d}
; Banks: {n_banks}
; Assembler: MADS
; ==========================================================================

    icl 'config.icl'

; --- Bank switch stub ($0600, XEX loading only) ---

    org $0600

bank_switch_stub:
bank_switch_val = *+1
    lda #$FF
    sta PORTB
    rts

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

        f.write("""\

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

; --- Bank data ---

    icl 'banks.icl'

; --- Restore main RAM, start player ---

    org bank_switch_val
    .byte PORTB_MAIN
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
