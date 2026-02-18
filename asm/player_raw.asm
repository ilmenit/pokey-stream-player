; ==========================================================================
; player_raw.asm - RAW Player Init and Main Loop
; ==========================================================================
;
; Architecture:
;   - Each bank contains raw POKEY indices at $4000-$7FFF (16384 bytes)
;   - IRQ banks in on every sample, reads one byte, banks out
;   - AUDC LUT in player code space, always accessible
;   - Bank transition handled entirely within IRQ handler
;
; Requires: config.asm, atari.inc, zeropage_raw.inc
;           audc_tables.asm, portb_table.asm
; ==========================================================================

; ==========================================================================
; PLAY_INIT - entered from splash.asm after SPACE pressed (SEI already done)
; ==========================================================================
play_init:
    ; --- Set IRQ vector to our handler ---
    lda #<irq_handler
    sta $FFFE
    lda #>irq_handler
    sta $FFFF

    ; --- Copy runtime-detected PORTB values into portb_table ---
    ldx #0
@copy_portb:
    lda TAB_MEM_BANKS+1,x
    sta portb_table,x
    inx
    cpx #N_BANKS
    bcc @copy_portb

    ; --- Initialize playback state ---
    lda #<BANK_BASE
    sta ZP_SAMPLE_PTR
    lda #>BANK_BASE
    sta ZP_SAMPLE_PTR+1
    lda #$00
    sta ZP_BANK_IDX

    ; --- Prime cached sample (read first byte from bank 0) ---
    lda portb_table
    sta PORTB                   ; Switch to bank 0
    lda BANK_BASE               ; Read first sample byte
    sta ZP_CACHED
    lda #PORTB_MAIN
    sta PORTB                   ; Bank out

    ; Advance pointer past first byte
    inc ZP_SAMPLE_PTR

    ; --- Mark as playing ---
    lda #$FF
    sta ZP_PLAYING

    ; --- Setup POKEY timer and enable IRQ ---
    jsr pokey_setup
    cli

; ==========================================================================
; MAIN LOOP - RAW mode needs no main loop work
; ==========================================================================
; All bank transitions happen inside the IRQ handler.
main_loop:
    lda ZP_PLAYING
    bne main_loop

    jmp return_to_idle
