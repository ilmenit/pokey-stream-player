; ==========================================================================
; irq_lz.asm - DeltaLZ In-IRQ Decoder
; ==========================================================================
;
; Each IRQ: decode one LZ byte, delta-accumulate, cache for next IRQ.
; State machine: 0=token fetch, 1=literal run, 2=match copy.
;
; Decode buffer: $8000-$BFFF (16 KB). The COMPRESSOR guarantees:
;   - No literal or match copy straddles the buffer boundary
;   - No match offset reaches outside valid buffer range
;   - ZP_LZ_MATCH never needs wrap checking
;
; ZP_LZ_DST wraps from $C000 to $8000 only at token boundaries (rare).
; ZP_LZ_MATCH NEVER wraps — saves ~8 cycles on every match byte.
;
; Bank header: 1 byte (delta_acc). Source starts at $4001.
; PORTB AND #$FE keeps OS ROM off -> no SEI needed for bank switch.
;
; Mode dispatch via LSR:
;   mode 0: LSR -> A=0,C=0 -> fall to JMP @need_token
;   mode 1: LSR -> A=0,C=1 -> BCS @literal_byte (hot path)
;   mode 2: LSR -> A=1,C=0 -> BNE @go_match
;
; Requires: config.asm (N_BANKS, POKEY_CHANNELS)
;           audc_tables.asm, zeropage_lz.inc
; ==========================================================================

; Buffer boundaries (from atari.inc: LZ_BUF_BASE=$8000, LZ_BUF_END=$C000)
BUF_START_HI = >LZ_BUF_BASE
BUF_END_HI   = >LZ_BUF_END

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
    sta ZP_SAVE_A
    stx ZP_SAVE_X

    ; ACK POKEY IRQ
    lda #$00
    sta IRQEN
    lda #IRQ_MASK
    sta IRQEN

    lda ZP_PLAYING
    beq irq_exit

; ==================================================================
; FIXED-TIMING OUTPUT: cached sample → AUDC via LUT
; ==================================================================
    ldx ZP_CACHED

.if POKEY_CHANNELS >= 1
    lda audc1_tab,x
    sta AUDC1
.endif
.if POKEY_CHANNELS >= 2
    lda audc2_tab,x
    sta AUDC2
.endif
.if POKEY_CHANNELS >= 3
    lda audc3_tab,x
    sta AUDC3
.endif
.if POKEY_CHANNELS >= 4
    lda audc4_tab,x
    sta AUDC4
.endif

; ==================================================================
; DECODE NEXT SAMPLE (timing no longer matters past this point)
; ==================================================================
    ; Mode dispatch via LSR
    lda ZP_LZ_MODE
    lsr
    bcs @literal_byte            ; mode 1 (hot path)
    bne @go_match                ; mode 2
    jmp @need_token              ; mode 0
@go_match:
    jmp @match_byte

; --- MODE 1: Literal (fast path, falls through to @output) ---
@literal_byte:
    ldy #0
    lda (ZP_LZ_SRC),y
    sta (ZP_LZ_DST),y
    inc ZP_LZ_SRC
    bne @output
    inc ZP_LZ_SRC+1

; --- Common output: delta accumulate + cache ---
@output:
    clc
    adc ZP_DELTA_ACC
    sta ZP_DELTA_ACC
    sta ZP_CACHED                ; cache for next IRQ

    ; Advance ZP_LZ_DST (circular buffer wrap)
    inc ZP_LZ_DST
    bne @dst_ok
    inc ZP_LZ_DST+1
    lda ZP_LZ_DST+1
    cmp #BUF_END_HI
    bcc @dst_ok
    lda #BUF_START_HI            ; wrap $C000 -> $8000
    sta ZP_LZ_DST+1
@dst_ok:
    dec ZP_LZ_COUNT
    bne irq_exit
    lda #0
    sta ZP_LZ_MODE

irq_exit:
    ldx ZP_SAVE_X
    lda ZP_SAVE_A
    rti

; --- MODE 0: Token fetch ---
@need_token:
    ldy #0
    lda (ZP_LZ_SRC),y
    bne @token_not_zero
    jmp @end_of_block            ; $00 = end of bank
@token_not_zero:
    inc ZP_LZ_SRC
    bne @+
    inc ZP_LZ_SRC+1
@:
    cmp #$80
    bcs @token_match

    ; Literal token: byte = count
    sta ZP_LZ_COUNT
    lda #$01
    sta ZP_LZ_MODE
    jmp @literal_byte            ; decode first literal immediately

    ; Match token: bit 7 set
@token_match:
    tax                          ; save full token in X
    and #$3F
    clc
    adc #$03                     ; length = (token & $3F) + 3
    sta ZP_LZ_COUNT

    cpx #$C0
    bcs @long_match

    ; Short match: 1-byte offset (token $80-$BF)
    ldy #0
    lda (ZP_LZ_SRC),y
    sta ZP_LZ_MATCH
    inc ZP_LZ_SRC
    bne @+
    inc ZP_LZ_SRC+1
@:
    ; match_ptr = dst - offset
    sec
    lda ZP_LZ_DST
    sbc ZP_LZ_MATCH
    sta ZP_LZ_MATCH
    lda ZP_LZ_DST+1
    sbc #$00
    sta ZP_LZ_MATCH+1
    ; No wrap: compressor guarantees result >= $8000
    lda #$02
    sta ZP_LZ_MODE
    jmp @match_byte

    ; Long match: 2-byte offset (token $C0-$FF)
@long_match:
    ldy #0
    lda (ZP_LZ_SRC),y
    sta ZP_LZ_MATCH
    iny
    lda (ZP_LZ_SRC),y
    sta ZP_LZ_MATCH+1
    ; Advance src past 2-byte offset
    clc
    lda ZP_LZ_SRC
    adc #$02
    sta ZP_LZ_SRC
    bcc @+
    inc ZP_LZ_SRC+1
@:
    ; match_ptr = dst - offset
    sec
    lda ZP_LZ_DST
    sbc ZP_LZ_MATCH
    sta ZP_LZ_MATCH
    lda ZP_LZ_DST+1
    sbc ZP_LZ_MATCH+1
    sta ZP_LZ_MATCH+1
    ; No wrap: compressor guarantees result in [$8000,$BFFF]
    lda #$02
    sta ZP_LZ_MODE
    ; fall through to @match_byte

; --- MODE 2: Match copy ---
@match_byte:
    ldy #0
    lda (ZP_LZ_MATCH),y
    sta (ZP_LZ_DST),y
    inc ZP_LZ_MATCH
    bne @+
    inc ZP_LZ_MATCH+1
    ; No wrap check: compressor guarantees match source stays in buffer
@:
    jmp @output

; --- End of block: switch to next bank ---
; ZP_LZ_DST does NOT reset — circular buffer continues across banks.
@end_of_block:
    inc ZP_BANK_IDX
    lda ZP_BANK_IDX
    cmp #N_BANKS
    bcs @finished

    ; Bank in to read header of new bank
    ldx ZP_BANK_IDX
    lda portb_table,x
    and #$FE                     ; keep OS ROM off
    sta PORTB
    lda BANK_BASE                ; 1-byte header = new delta_acc
    sta ZP_DELTA_ACC

    ; Source at $4001 (past header)
    lda #$01
    sta ZP_LZ_SRC
    lda #>BANK_BASE
    sta ZP_LZ_SRC+1
    ; ZP_LZ_DST stays where it is (circular)
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
