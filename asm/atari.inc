; ==========================================================================
; atari.inc - Atari 800XL/XE Hardware Register Definitions
; ==========================================================================

; --- POKEY (primary) ---
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
SKSTAT  = $D20F             ; read
SKCTL   = $D20F             ; write
KBCODE  = $D209             ; read (same addr as STIMER write)
IRQEN   = $D20E

; --- GTIA ---
COLPF1_W = $D017
COLPF2_W = $D018
COLBK_W  = $D01A

; --- ANTIC ---
DMACTL  = $D400
DLISTL  = $D402
DLISTH  = $D403
CHBASE  = $D409
NMIEN   = $D40E

; --- PIA ---
PORTB   = $D301

; --- Constants ---
BANK_BASE    = $4000         ; Extended RAM bank window
BANK_SIZE    = $4000         ; 16384 bytes per bank
LZ_BUF_BASE  = $8000         ; LZ decode buffer start
LZ_BUF_END   = $C000         ; LZ decode buffer end
BANK_END     = $8000         ; Bank window end
PORTB_MAIN   = $FE           ; Main RAM, OS ROM disabled
IRQ_MASK     = $01           ; Timer 1 IRQ
SILENCE      = $10           ; AUDC volume-only, vol=0

; --- Fixed addresses ---
CODE_BASE    = $2000         ; Player code origin
CHARSET_ADDR = $E000         ; OS charset address (copied ROM→RAM)
BANK_STUB    = $0600         ; INI stub area (cassette buffer)
STUB_ADDR    = BANK_STUB    ; Alias used by copy_rom.asm / mem_detect.asm
TAB_MEM_BANKS = $0480        ; 65 bytes: [0]=$FF, [1..64]=bank PORTB values
