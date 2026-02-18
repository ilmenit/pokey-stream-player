; ==========================================================================
; irq_raw.asm - RAW Mode IRQ Handler (Direct Bank Read)
; ==========================================================================
;
; Strategy: write-first-then-read for fixed output timing.
;   1. Write POKEY from cached sample via AUDC LUT (fixed cycles from entry)
;   2. Bank in, read NEXT sample byte, bank out
;   3. Cache raw index for next IRQ
;   4. Advance pointer, check bank exhaustion
;
; Bank transitions: handled entirely within IRQ (inc bank_idx, reset ptr).
; No main loop involvement needed.
;
; Requires: config.asm (N_BANKS, POKEY_CHANNELS)
;           audc_tables.asm (audc1_tab..audc4_tab)
;           zeropage_raw.inc
; ==========================================================================

; ------------------------------------------------------------------
; Validation
; ------------------------------------------------------------------
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

    ; ACK POKEY IRQ
    lda #$00                                                ; 2
    sta IRQEN                                               ; 4
    lda #IRQ_MASK                                           ; 2
    sta IRQEN                                               ; 4

    lda ZP_PLAYING                                          ; 3
    beq irq_exit                                            ; 2

; ==================================================================
; FIXED-TIMING OUTPUT: cached sample index → AUDC via LUT
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
; BANK IN: read next sample from extended memory
; ==================================================================
    ldx ZP_BANK_IDX                                         ; 3
    lda portb_table,x                                       ; 4
    sta PORTB                                               ; 4

    ldy #$00                                                ; 2
    lda (ZP_SAMPLE_PTR),y        ; read sample byte          ; 5
    sta ZP_CACHED                ; cache for next IRQ         ; 3

    lda #PORTB_MAIN                                         ; 2
    sta PORTB                    ; bank out                   ; 4

; ==================================================================
; ADVANCE POINTER
; ==================================================================
    inc ZP_SAMPLE_PTR                                       ; 5
    bne irq_exit                 ; no page cross → done      ; 3
    inc ZP_SAMPLE_PTR+1                                     ; 5

    ; Check bank exhaustion: hi byte reached $80?
    ; INC sets N flag — BMI detects bit 7 set ($80+)
    bmi @bank_exhausted                                     ; 2/3

irq_exit:
    ldx ZP_SAVE_X                                           ; 3
    lda ZP_SAVE_A                                           ; 3
    rti                                                     ; 6

; ------------------------------------------------------------------
; Bank exhausted — advance to next or stop
; ------------------------------------------------------------------
@bank_exhausted:
    inc ZP_BANK_IDX
    lda ZP_BANK_IDX
    cmp #N_BANKS
    bcs @finished

    ; Reset pointer to start of new bank
    lda #<BANK_BASE
    sta ZP_SAMPLE_PTR
    lda #>BANK_BASE
    sta ZP_SAMPLE_PTR+1
    jmp irq_exit

@finished:
    lda #$00
    sta ZP_PLAYING
    lda #SILENCE
    sta AUDC1
    sta AUDC2
    sta AUDC3
    sta AUDC4
    jmp irq_exit
