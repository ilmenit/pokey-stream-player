; ==========================================================================
; pokey_setup.asm - POKEY Hardware Initialization
; ==========================================================================
; Requires: config.asm (POKEY_DIVISOR, AUDCTL_VAL)

pokey_setup:
    lda #$00
    sta IRQEN                ; disable IRQs
    sta SKCTL                ; reset POKEY
    lda #SILENCE
    sta AUDC1                ; silence all channels
    sta AUDC2
    sta AUDC3
    sta AUDC4
    lda #POKEY_DIVISOR
    sta AUDF1                ; timer 1 frequency
    lda #AUDCTL_VAL
    sta AUDCTL               ; clock configuration
    lda #$03
    sta SKCTL                ; enable keyboard + serial
    lda #IRQ_MASK
    sta IRQEN                ; enable timer 1 IRQ
    lda #$00
    sta STIMER               ; start timers
    rts
