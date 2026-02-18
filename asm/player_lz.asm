; ==========================================================================
; player_lz.asm - DeltaLZ Player Init and Main Loop
; ==========================================================================
;
; Architecture:
;   - Each bank contains DeltaLZ-compressed POKEY data at $4000
;   - 1-byte header: initial delta accumulator value
;   - IRQ IS the decompressor: one byte decoded per interrupt
;   - Decode buffer at $8000-$BFFF (16 KB, circular)
;   - Bank transitions happen inside IRQ (no main loop involvement)
;
; Requires: config.asm, atari.inc, zeropage_lz.inc
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

    ; --- Initialize LZ state for bank 0 ---
    jsr init_first_bank

    ; --- Setup POKEY timer and enable IRQ ---
    jsr pokey_setup
    cli

; ==========================================================================
; MAIN LOOP - LZ mode needs no main loop work
; ==========================================================================
; All bank transitions and decompression happen inside the IRQ handler.
main_loop:
    lda ZP_PLAYING
    bne main_loop

    jmp return_to_idle

; ==========================================================================
; INIT_FIRST_BANK - Set up LZ state for bank 0
; ==========================================================================
init_first_bank:
    ; Bank in to read header
    lda portb_table              ; bank 0 = first entry
    and #$FE                     ; keep OS ROM off
    sta PORTB
    lda BANK_BASE                ; 1-byte header = initial delta_acc
    sta ZP_DELTA_ACC

    ; Source starts at $4001 (past 1-byte header)
    lda #$01
    sta ZP_LZ_SRC
    lda #>BANK_BASE
    sta ZP_LZ_SRC+1

    ; Decode buffer starts at $8000
    lda #$00
    sta ZP_LZ_DST
    lda #>LZ_BUF_BASE
    sta ZP_LZ_DST+1

    ; Clear state machine
    lda #$00
    sta ZP_LZ_MODE
    sta ZP_LZ_COUNT
    sta ZP_BANK_IDX

    ; Prime cached sample: first output = delta_acc value
    lda ZP_DELTA_ACC
    sta ZP_CACHED

    ; Mark as playing
    lda #$FF
    sta ZP_PLAYING

    rts
