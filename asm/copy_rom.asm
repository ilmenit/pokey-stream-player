; ==========================================================================
; copy_rom.asm - Copy OS ROM to underlying RAM
; ==========================================================================
; INI segment: runs during XEX loading.
; Copies $C000-$CFFF and $D800-$FFFF from ROM to RAM underneath.
; After this, disabling ROM (PORTB bit0=0) still has valid charset at $E000.
; Skips I/O space $D000-$D7FF.
;
; Technique: INC/DEC PORTB toggles bit0 (ROM enable) without disturbing
; other bits. Read from ROM, flip to RAM, write, flip back to ROM.
; ==========================================================================

    org STUB_ADDR

ZP_COPY_PTR = $CB           ; 2 bytes: temp pointer (safe, above player ZP)

copy_rom_start:
    sei
    lda #$00
    sta NMIEN                ; disable NMI (VBI would crash with ROM off)

    ; Initialize pointer to $C000
    ldy #$00
    sty ZP_COPY_PTR
    lda #$C0
    sta ZP_COPY_PTR+1

    ; Ensure ROM is enabled: set bit 0 of PORTB
    lda PORTB
    ora #$01
    sta PORTB

@copy_loop:
    lda (ZP_COPY_PTR),y      ; read from ROM
    dec PORTB                 ; flip to RAM (clear bit 0)
    sta (ZP_COPY_PTR),y      ; write to RAM
    inc PORTB                 ; flip back to ROM (set bit 0)
    iny
    bne @copy_loop

    ; Next page
    inc ZP_COPY_PTR+1
    lda ZP_COPY_PTR+1

    ; Skip I/O area ($D000-$D7FF)
    cmp #$D0
    bne @check_end
    lda #$D8
    sta ZP_COPY_PTR+1

@check_end:
    lda ZP_COPY_PTR+1
    bne @copy_loop            ; loops until wraps $FF→$00

    ; Done — restore interrupts for XEX loader
    lda #$40
    sta NMIEN                 ; re-enable VBI
    cli
    rts

    ini copy_rom_start
