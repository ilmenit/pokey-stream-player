; ==========================================================================
; stream_player.asm - POKEY Stream Player (Master File)
; ==========================================================================
;
; Build with MADS assembler:
;   mads stream_player.asm -o:output.xex
;
; Supports three compression modes (set in config.asm):
;   MODE_VQ  (2) - Vector quantization, ~4x compression
;   MODE_LZ  (1) - DeltaLZ, lossless ~1.3x compression
;   MODE_RAW (0) - Uncompressed direct streaming
;
; File organization:
;   STATIC (player code, version-controlled):
;     atari.inc          - Hardware register definitions
;     zeropage_vq.inc    - Zero page variables (VQ mode)
;     zeropage_lz.inc    - Zero page variables (LZ mode)
;     zeropage_raw.inc   - Zero page variables (RAW mode)
;     copy_rom.asm       - ROM-to-RAM copy (INIT segment)
;     mem_detect.asm     - Extended RAM detection (INIT segment)
;     splash.asm         - Splash screen + startup + key wait
;     player_vq.asm      - VQ player init + main loop
;     player_lz.asm      - LZ player init + main loop
;     player_raw.asm     - RAW player init + main loop
;     irq_vq.asm         - VQ IRQ handler
;     irq_lz.asm         - LZ IRQ handler (in-IRQ decompressor)
;     irq_raw.asm        - RAW IRQ handler
;     pokey_setup.asm    - POKEY hardware initialization
;
;   GENERATED (per-song data, from Python encoder):
;     config.asm         - Constants (N_BANKS, COMPRESS_MODE, etc.)
;     audc_tables.asm    - Index-to-AUDC lookup tables
;     portb_table.asm    - Bank switching PORTB placeholder
;     vq_tables.asm      - VQ codebook address lookup (VQ only)
;     splash_data.asm    - Splash screen text (screen codes)
;     bank_XX.asm        - Bank data (per bank)
;     banks.asm          - Bank loading stubs (INI segments)
;
; Memory map:
;   $0480-$04C1  TAB_MEM_BANKS (filled by mem_detect INIT)
;   $0600+       INIT stub area (reused: copy_rom, mem_detect, bank loaders)
;   $2000-$3FFF  Player code + tables + splash data
;   $4000-$7FFF  Extended RAM bank window (16KB per bank)
;   $8000-$BFFF  Decode buffer (LZ mode only)
;   $C000-$FFFF  RAM copy of OS ROM (charset at $E000, vectors at $FFFA+)
;
; ==========================================================================

; --- Compression mode constants ---
MODE_RAW = 0
MODE_LZ  = 1
MODE_VQ  = 2

; --- Include hardware definitions and generated config ---
    icl 'atari.inc'
    icl 'config.asm'

; --- Mode-specific zero page variables ---
.if COMPRESS_MODE = MODE_VQ
    icl 'zeropage_vq.inc'
.elseif COMPRESS_MODE = MODE_LZ
    icl 'zeropage_lz.inc'
.else
    icl 'zeropage_raw.inc'
.endif

; ==========================================================================
; INIT SEGMENT 1: Copy OS ROM to underlying RAM
; ==========================================================================
    icl 'copy_rom.asm'

; ==========================================================================
; INIT SEGMENT 2: Detect extended memory banks
; ==========================================================================
    icl 'mem_detect.asm'

; ==========================================================================
; MAIN CODE SEGMENT
; ==========================================================================
    org CODE_BASE

; --- Splash screen data (generated: song-specific text) ---
    icl 'splash_data.asm'

; --- Splash screen logic, startup, wait loop ---
    icl 'splash.asm'

; --- AUDC lookup tables (generated: index -> AUDC register value) ---
    icl 'audc_tables.asm'

; --- PORTB bank table (placeholder, patched at runtime) ---
    icl 'portb_table.asm'

; --- VQ address tables (VQ mode only) ---
.if COMPRESS_MODE = MODE_VQ
    icl 'vq_tables.asm'
.endif

; --- POKEY setup (shared across all modes) ---
    icl 'pokey_setup.asm'

; --- Mode-specific player + IRQ handler ---
.if COMPRESS_MODE = MODE_VQ
    icl 'player_vq.asm'
    icl 'irq_vq.asm'
.elseif COMPRESS_MODE = MODE_LZ
    icl 'player_lz.asm'
    icl 'irq_lz.asm'
.else
    icl 'player_raw.asm'
    icl 'irq_raw.asm'
.endif

; ==========================================================================
; BOUNDARY CHECK
; ==========================================================================
    .if * > BANK_BASE
        .error 'Player code overflows into bank window ($4000)!'
    .endif

_CODE_END_ = *
_CODE_SIZE_ = _CODE_END_ - CODE_BASE

; ==========================================================================
; BANK DATA SEGMENTS
; ==========================================================================
    icl 'banks.asm'

; ==========================================================================
; RUN ADDRESS
; ==========================================================================
    org $02E0
    .word start
