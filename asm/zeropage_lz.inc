; ==========================================================================
; zeropage_lz.inc - Zero Page Variables (DeltaLZ Mode)
; ==========================================================================
; Range $80-$8F: safely above OS shadow registers.
;
; In-IRQ LZ decoder state machine. Each IRQ decodes one byte,
; delta-accumulates, and caches the result for the NEXT IRQ's
; fixed-timing AUDC output.

ZP_LZ_SRC       = $80       ; 2 bytes: read ptr in bank ($4000+)
ZP_LZ_DST       = $82       ; 2 bytes: write ptr in decode buffer ($8000+)
ZP_LZ_COUNT     = $84       ; 1 byte:  bytes remaining in literal/match run
ZP_LZ_MATCH     = $85       ; 2 bytes: match source ptr (in decode buffer)
ZP_LZ_MODE      = $87       ; 1 byte:  0=token, 1=literal, 2=match
ZP_DELTA_ACC    = $88       ; 1 byte:  running delta accumulator
ZP_BANK_IDX     = $89       ; 1 byte:  current bank index
ZP_PLAYING      = $8A       ; 1 byte:  $FF=playing, $00=stopped
ZP_SAVE_A       = $8B       ; 1 byte:  IRQ register save
ZP_SAVE_X       = $8C       ; 1 byte:  IRQ register save
ZP_CACHED       = $8D       ; 1 byte:  cached sample for fixed-timing output
