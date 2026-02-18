; ==========================================================================
; zeropage_vq.inc - Zero Page Variables (VQ Mode)
; ==========================================================================
; Range $80-$8F: safely above OS shadow registers.

ZP_VEC_PTR      = $80       ; 2 bytes: pointer into banked codebook vector
ZP_BANK_IDX     = $82       ; 1 byte:  current bank index (0..N_BANKS-1)
ZP_PLAYING      = $83       ; 1 byte:  $FF=playing, $00=stopped
ZP_SAVE_A       = $84       ; 1 byte:  IRQ register save
ZP_SAVE_X       = $85       ; 1 byte:  IRQ register save
ZP_CACHED       = $86       ; 1 byte:  cached codebook index for AUDC output
ZP_VEC_POS      = $87       ; 1 byte:  position within vector (0..VEC_SIZE-1)
ZP_IDX_PTR      = $88       ; 2 bytes: pointer into index stream in bank
ZP_NEED_BANK    = $8A       ; 1 byte:  $FF=IRQ signals bank advance
