; ==========================================================================
; irq_vq.asm - VQ IRQ Handler (Direct Bank Read)
; ==========================================================================
;
; Strategy: write-first-then-read for fixed output timing.
;   1. Write POKEY from cached codebook index via AUDC LUT
;   2. Bank in
;   3. Read NEXT sample from codebook at $4000+
;   4. On vector boundary: read next index byte (already banked in!)
;   5. Bank out
;   6. Cache raw index for next IRQ
;
; Cycle counts (fast path):
;   1ch: ~84cy  (37% CPU @8kHz)
;   2ch: ~92cy  (41% CPU @8kHz)
;   3ch: ~100cy (45% CPU @8kHz)
;   4ch: ~108cy (48% CPU @8kHz)
;
; Bank transitions: ZERO missed samples (no SEI, no codebook copy).
;
; Requires: config.asm (VEC_SIZE, POKEY_CHANNELS)
;           audc_tables.asm (audc1_tab..audc4_tab)
;           vq_tables.asm (vq_lo_tab, vq_hi_tab)
; ==========================================================================

; ------------------------------------------------------------------
; Validation (nested .if — MADS doesn't support && or || operators)
; ------------------------------------------------------------------
.if VEC_SIZE <> 2
 .if VEC_SIZE <> 4
  .if VEC_SIZE <> 8
   .if VEC_SIZE <> 16
    .error "VEC_SIZE must be 2, 4, 8, or 16"
   .endif
  .endif
 .endif
.endif
.if POKEY_CHANNELS < 1
    .error "POKEY_CHANNELS must be 1-4"
.endif
.if POKEY_CHANNELS > 4
    .error "POKEY_CHANNELS must be 1-4"
.endif

; ------------------------------------------------------------------
; IRQ Entry
; ------------------------------------------------------------------
irq_handler:
    sta ZP_SAVE_A                                           ; 3
    stx ZP_SAVE_X                                           ; 3

    ; ACK POKEY IRQ (must clear+set IRQEN)
    lda #$00                                                ; 2
    sta IRQEN                                               ; 4
    lda #IRQ_MASK                                           ; 2
    sta IRQEN                                               ; 4

    ; Check playing flag
    lda ZP_PLAYING                                          ; 3
    beq irq_exit                                            ; 2 (not taken)

; ==================================================================
; FIXED-TIMING OUTPUT: cached codebook index → AUDC via LUT
; LUT tables are in player code space ($2000+), always accessible.
; ==================================================================
    ldx ZP_CACHED                                           ; 3

.if POKEY_CHANNELS >= 1
    lda audc1_tab,x                                         ; 4
    sta AUDC1                                               ; 4
.endif
.if POKEY_CHANNELS >= 2
    lda audc2_tab,x                                         ; 4
    sta AUDC2                                               ; 4
.endif
.if POKEY_CHANNELS >= 3
    lda audc3_tab,x                                         ; 4
    sta AUDC3                                               ; 4
.endif
.if POKEY_CHANNELS >= 4
    lda audc4_tab,x                                         ; 4
    sta AUDC4                                               ; 4
.endif

; ==================================================================
; BANK IN: switch to current extended memory bank
; ==================================================================
    ldx ZP_BANK_IDX                                         ; 3
    lda portb_table,x           ; runtime-copied PORTB value  ; 4
    sta PORTB                                               ; 4

; ==================================================================
; READ NEXT SAMPLE from codebook ($4000+ in banked memory)
; ==================================================================
    ldy ZP_VEC_POS                                          ; 3
    lda (ZP_VEC_PTR),y       ; codebook vector byte          ; 5
    sta ZP_CACHED            ; cache for next IRQ             ; 3

; ------------------------------------------------------------------
; Advance position within vector
; ------------------------------------------------------------------
    iny                                                     ; 2
    cpy #VEC_SIZE                                           ; 2
    bcs irq_new_vector       ; boundary → need new index     ; 2/3
    sty ZP_VEC_POS                                          ; 3

; ------------------------------------------------------------------
; BANK OUT (fast path) and exit
; ------------------------------------------------------------------
    lda #PORTB_MAIN                                         ; 2
    sta PORTB                                               ; 4

irq_exit:
    ldx ZP_SAVE_X                                           ; 3
    lda ZP_SAVE_A                                           ; 3
    rti                                                     ; 6

; ==================================================================
; VECTOR BOUNDARY: read next index byte (STILL banked in!)
; No extra bank switch needed — already in the right bank.
; ==================================================================
irq_new_vector:
    ; Guard: if bank already exhausted, don't read new index.
    ; Just replay the same vector until main loop switches bank.
    lda ZP_NEED_BANK                                        ; 3
    bne @replay_vector                                      ; 2/3

    ldy #$00                                                ; 2
    sty ZP_VEC_POS                                          ; 3

    ; Read index byte from bank
    lda (ZP_IDX_PTR),y       ; Y=0, index byte               ; 5
    tax                      ; X = codebook index             ; 2

    ; BANK OUT (before accessing VQ tables in player code space)
    lda #PORTB_MAIN                                         ; 2
    sta PORTB                                               ; 4

    ; Update vec_ptr from VQ_LO/VQ_HI tables
    lda vq_lo_tab,x                                         ; 4
    sta ZP_VEC_PTR                                          ; 3
    lda vq_hi_tab,x                                         ; 4
    sta ZP_VEC_PTR+1                                        ; 3

    ; Advance idx_ptr
    ; BMI optimization: INC sets N flag. When hi byte reaches $80
    ; (past bank window), bit 7 set → BMI taken → bank exhausted.
    inc ZP_IDX_PTR                                          ; 5
    bne @no_check            ; lo didn't wrap (255/256 case)  ; 3
    inc ZP_IDX_PTR+1         ; lo wrapped → increment hi      ; 5
    bmi @bank_exhausted      ; hi=$80 → N flag → exhausted    ; 2/3
@no_check:
    jmp irq_exit                                            ; 3

@bank_exhausted:
    ; Signal main loop to advance to next bank
    lda #$FF
    sta ZP_NEED_BANK
    jmp irq_exit

@replay_vector:
    ; Bank still exhausted — replay last vector from position 0.
    ; VEC_PTR still points to last valid vector; just reset position.
    ldy #$00
    sty ZP_VEC_POS
    ; BANK OUT and exit
    lda #PORTB_MAIN
    sta PORTB
    jmp irq_exit
