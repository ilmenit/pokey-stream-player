; ==========================================================================
; player_vq.asm - VQ Player Init and Main Loop (Direct Bank Read)
; ==========================================================================
;
; Architecture:
;   - Codebook + index stream live together in each bank at $4000
;   - IRQ banks in on EVERY sample to read codebook, then banks out
;   - AUDC LUTs in player code space, always accessible regardless of bank
;   - Every VEC_SIZE IRQs: also read next index byte (already banked in)
;   - Bank transition: SEI, prime new bank (read first index, set vec_ptr,
;     cache first sample), CLI. ~50cy atomic window, ZERO corrupted samples.
;
; Requires: config.asm, atari.inc, zeropage_vq.inc
;           audc_tables.asm, vq_tables.asm
; ==========================================================================

; Index stream starts right after codebook in each bank
IDX_START = BANK_BASE + (256 * VEC_SIZE)

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
    ; mem_detect filled TAB_MEM_BANKS+1..+N with bank PORTB codes.
    ; IRQ reads portb_table,x for fast indexed access.
    ldx #0
@copy_portb:
    lda TAB_MEM_BANKS+1,x
    sta portb_table,x
    inx
    cpx #N_BANKS
    bcc @copy_portb

    ; --- Initialize zero page ---
    lda #$00
    sta ZP_BANK_IDX
    sta ZP_VEC_POS
    sta ZP_NEED_BANK

    ; --- Set idx_ptr to start of index stream ---
    lda #<IDX_START
    sta ZP_IDX_PTR
    lda #>IDX_START
    sta ZP_IDX_PTR+1

    ; --- Bank in to read first index + cache first sample ---
    ldx #0                      ; bank 0
    lda portb_table,x
    sta PORTB                   ; Switch to bank 0

    ; Read first index byte
    ldy #$00
    lda (ZP_IDX_PTR),y          ; First index byte
    tax                         ; X = codebook index

    ; Set vec_ptr from VQ tables
    lda vq_lo_tab,x
    sta ZP_VEC_PTR
    lda vq_hi_tab,x
    sta ZP_VEC_PTR+1

    ; Advance idx_ptr past first index byte
    inc ZP_IDX_PTR
    bne @prime_nc
    inc ZP_IDX_PTR+1
@prime_nc:

    ; Cache first sample from codebook (still banked in)
    ldy #$00
    lda (ZP_VEC_PTR),y          ; Raw codebook value at vector[0]
    sta ZP_CACHED
    lda #$01
    sta ZP_VEC_POS              ; Next IRQ reads vector[1]

    ; Bank out
    lda #PORTB_MAIN
    sta PORTB

    ; --- Mark as playing ---
    lda #$FF
    sta ZP_PLAYING

    ; --- Silence POKEY before starting ---
    lda #SILENCE
    sta AUDC1
    sta AUDC2
    sta AUDC3
    sta AUDC4

    ; --- Setup POKEY timer and enable IRQ ---
    jsr pokey_setup
    cli                         ; Let the music play!

; ==========================================================================
; MAIN LOOP - Handle bank transitions
; ==========================================================================
main_loop:
    lda ZP_PLAYING
    beq song_ended

    lda ZP_NEED_BANK
    beq main_loop               ; Spin until IRQ signals bank exhausted

    ; --- Bank transition ---
    ; MUST be atomic: IRQ reads from ZP_VEC_PTR/ZP_VEC_POS and we need
    ; to reset them consistently for the new bank.
    sei

    lda #$00
    sta ZP_NEED_BANK

    inc ZP_BANK_IDX
    lda ZP_BANK_IDX
    cmp #N_BANKS
    bcs stop_play               ; All banks consumed

    ; Reset idx_ptr to start of new bank's index stream
    lda #<IDX_START
    sta ZP_IDX_PTR
    lda #>IDX_START
    sta ZP_IDX_PTR+1

    ; CRITICAL: Prime new bank state (same as play_init).
    ; Without this, the first IRQs after transition would read from
    ; VEC_PTR (still pointing at old bank's codebook offset) into the
    ; new bank's memory — producing corrupted samples until the next
    ; vector boundary naturally resets VEC_PTR.
    ldx ZP_BANK_IDX
    lda portb_table,x
    sta PORTB                   ; Bank in

    ; Read first index byte from new bank
    ldy #$00
    lda (ZP_IDX_PTR),y
    tax

    ; Set vec_ptr from VQ tables
    lda vq_lo_tab,x
    sta ZP_VEC_PTR
    lda vq_hi_tab,x
    sta ZP_VEC_PTR+1

    ; Advance idx_ptr
    inc ZP_IDX_PTR
    bne @bnc
    inc ZP_IDX_PTR+1
@bnc:

    ; Cache first sample from new bank
    ldy #$00
    lda (ZP_VEC_PTR),y
    sta ZP_CACHED
    lda #$01
    sta ZP_VEC_POS

    lda #PORTB_MAIN
    sta PORTB                   ; Bank out

    cli
    jmp main_loop

stop_play:
    cli
    lda #$00
    sta ZP_PLAYING

song_ended:
    jmp return_to_idle
