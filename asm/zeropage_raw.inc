; ==========================================================================
; zeropage_raw.inc - Zero Page Variables (RAW Mode)
; ==========================================================================
; Range $80-$8F: safely above OS shadow registers.

ZP_SAMPLE_PTR   = $80       ; 2 bytes: read pointer into bank ($4000+)
ZP_BANK_IDX     = $82       ; 1 byte:  current bank index (0..N_BANKS-1)
ZP_PLAYING      = $83       ; 1 byte:  $FF=playing, $00=stopped
ZP_SAVE_A       = $84       ; 1 byte:  IRQ register save
ZP_SAVE_X       = $85       ; 1 byte:  IRQ register save
ZP_CACHED       = $86       ; 1 byte:  cached sample index for AUDC output
