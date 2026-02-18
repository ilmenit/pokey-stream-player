; ==========================================================================
; mem_detect.asm - Extended Memory Bank Detection (INIT Segment)
; ==========================================================================
; Runs once during XEX loading. Detects available extended RAM banks
; by writing unique signatures and reading them back.
;
; Results stored at TAB_MEM_BANKS ($0480):
;   +0: $FF (main memory sentinel, always present)
;   +1..+64: PORTB values for each detected physical bank
;
; Method:
;   Phase 1: Write unique signature (index) to $4100 in each bank
;   Phase 2: Read back signatures, eliminate aliases (mirrored banks)
;   Phase 3: Build sorted table of unique bank PORTB values
;
; Requires: atari.inc (PORTB, TAB_MEM_BANKS, BANK_BASE, PORTB_MAIN, STUB_ADDR)
; ==========================================================================

    org STUB_ADDR

; DBANK probe order table (64 entries)
; Phase 2 writes signatures using X from 63->0, so the lowest-index
; entry per physical bank becomes the canonical PORTB code.
dbank_table:
    .byte $E3,$C3,$A3,$83,$63,$43,$23,$03
    .byte $E7,$C7,$A7,$87,$67,$47,$27,$07
    .byte $EB,$CB,$AB,$8B,$6B,$4B,$2B,$0B
    .byte $EF,$CF,$AF,$8F,$6F,$4F,$2F,$0F
    .byte $ED,$CD,$AD,$8D,$6D,$4D,$2D,$0D
    .byte $E9,$C9,$A9,$89,$69,$49,$29,$09
    .byte $E5,$C5,$A5,$85,$65,$45,$25,$05
    .byte $E1,$C1,$A1,$81,$61,$41,$21,$01
DBANK_COUNT = 64

; Signature address within bank window (offset $100 to avoid page-0 aliasing)
PROBE_ADDR = BANK_BASE + $100

mem_detect:
    sei

    ; --- Zero TAB_MEM_BANKS table (65 bytes) ---
    ; Ensures undetected slots are $00 for splash memory check.
    lda #$00
    ldx #DBANK_COUNT            ; 64 entries + 1 sentinel = 65
@zero_loop:
    sta TAB_MEM_BANKS,x
    dex
    bpl @zero_loop

    ; --- Phase 1: Write unique signature to each bank ---
    ; Write index X to PROBE_ADDR in bank X (0..63)
    ldx #DBANK_COUNT-1
@write_loop:
    lda dbank_table,x
    sta PORTB                   ; Switch to bank X
    txa
    sta PROBE_ADDR              ; Write signature = X
    dex
    bpl @write_loop

    ; --- Phase 2: Read back, detect aliases ---
    ; For each bank, check if signature matches index.
    ; If not, this bank aliases another (mirrors same physical RAM).
    ; Store unique banks and their PORTB values.
    
    ldy #1                      ; Y = output index (start at +1, +0 = sentinel)
    ldx #0                      ; X = bank probe index
@read_loop:
    lda dbank_table,x
    sta PORTB                   ; Switch to bank X
    txa
    cmp PROBE_ADDR              ; Does signature match our index?
    bne @skip                   ; No -> alias, skip it
    
    ; Unique bank found - store its PORTB value
    lda dbank_table,x
    sta TAB_MEM_BANKS,y
    iny
@skip:
    inx
    cpx #DBANK_COUNT
    bcc @read_loop

    ; --- Finalize ---
    lda #PORTB_MAIN
    sta PORTB                   ; Restore main memory
    
    ; Store sentinel at +0
    lda #$FF
    sta TAB_MEM_BANKS

    ; Store bank count after table (Y-1 = number of unique banks)
    dey
    sty TAB_MEM_BANKS + DBANK_COUNT + 1

    cli
    rts

    ini mem_detect
