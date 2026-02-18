; ==========================================================================
; splash.asm - Splash Screen, Startup, Wait Loop, Return to Idle
; ==========================================================================
; Requires:
;   atari.inc       - hardware registers
;   config.asm      - N_BANKS
;   splash_data.asm - text_line1, text_line2, text_err_title, text_err_msg
;                     (40 bytes each, ANTIC Mode 2 screen codes)
; ==========================================================================

; --- Display list (vertically centered 2-line display) ---
dlist:
    .byte $70,$70,$70,$70       ; 32 blank scanlines
    .byte $70,$70,$70,$70       ; 32 more (centers text vertically)
    .byte $42                   ; Mode 2 text + LMS
dlist_lms:
    .word text_line1            ; Address of first text line
    .byte $02                   ; Mode 2 continuation (line 2, contiguous)
    .byte $41                   ; JVB (jump + wait for VBI)
    .word dlist

; ==========================================================================
; START - One-time startup (RUN entry point)
; ==========================================================================
start:
    sei
    cld

    ; --- Disable all interrupt sources FIRST ---
    lda #$00
    sta NMIEN
    sta IRQEN
    sta DMACTL

    ; --- Main RAM visible, OS ROM disabled ---
    lda #PORTB_MAIN
    sta PORTB

    ; --- NMI vector -> safe RTI stub ---
    lda #<nmi_handler
    sta $FFFA
    lda #>nmi_handler
    sta $FFFB

    ; --- IRQ vector -> RTI stub (safe dummy during splash) ---
    lda #<nmi_handler
    sta $FFFE
    lda #>nmi_handler
    sta $FFFF

    ; --- ANTIC charset in RAM (INIT segment copied ROM->RAM at $E000) ---
    lda #$E0
    sta CHBASE

    ; --- Initialize keyboard scan ---
    lda #$00
    sta SKCTL
    lda #$03
    sta SKCTL

; ==========================================================================
; MEMORY CHECK
; ==========================================================================
.if N_BANKS > 0
    lda TAB_MEM_BANKS + N_BANKS
    bne show_splash             ; Non-zero -> enough banks detected

    ; --- Insufficient memory: error screen + halt ---
    lda #<text_err_title
    sta dlist_lms
    lda #>text_err_title
    sta dlist_lms+1

    lda #<dlist
    sta DLISTL
    lda #>dlist
    sta DLISTH
    lda #$22
    sta DMACTL
    lda #$40
    sta NMIEN
    lda #$3E
    sta COLPF1_W                ; Bright red text
    lda #$00
    sta COLPF2_W
    lda #$30
    sta COLBK_W                 ; Dark red border
    cli
error_halt:
    jmp error_halt
.endif

; ==========================================================================
; SHOW SPLASH (also re-entry from return_to_idle)
; ==========================================================================
show_splash:
    lda #<text_line1
    sta dlist_lms
    lda #>text_line1
    sta dlist_lms+1

    lda #<dlist
    sta DLISTL
    lda #>dlist
    sta DLISTH
    lda #$22
    sta DMACTL
    lda #$40
    sta NMIEN
    lda #$0E
    sta COLPF1_W                ; Bright white text
    lda #$94
    sta COLPF2_W                ; Blue background
    lda #$00
    sta COLBK_W                 ; Black border

    lda #$00
    sta SKCTL
    lda #$03
    sta SKCTL
    cli

; ==========================================================================
; WAIT FOR SPACE KEY
; ==========================================================================
wait_loop:
    lda SKSTAT
    and #$04
    bne wait_loop
    lda SKSTAT                  ; Double-read debounce
    and #$04
    bne wait_loop

    lda KBCODE
    and #$3F
    cmp #$21                    ; SPACE
    beq got_space

wait_release:
    lda SKSTAT
    and #$04
    beq wait_release
    lda SKSTAT
    and #$04
    beq wait_release
    jmp wait_loop

got_space:
space_release:
    lda SKSTAT
    and #$04
    beq space_release
    lda SKSTAT
    and #$04
    beq space_release

    ; --- Transition to playback ---
    sei
    lda #$00
    sta DMACTL
    sta NMIEN
    jmp play_init

; ==========================================================================
; NMI HANDLER
; ==========================================================================
nmi_handler:
    rti

; ==========================================================================
; RETURN TO IDLE (called when song ends)
; ==========================================================================
return_to_idle:
    sei
    lda #$00
    sta IRQEN
    lda #SILENCE
    sta AUDC1
    sta AUDC2
    sta AUDC3
    sta AUDC4
    lda #PORTB_MAIN
    sta PORTB
    jmp show_splash
