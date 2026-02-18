# Project Design: POKEY Stream Player

## What This Project Does

This project converts standard audio files (WAV, MP3, FLAC, OGG) into
self-running Atari 8-bit XEX executables that play back the audio through
the POKEY sound chip in PCM mode. It targets Atari XL/XE computers with
extended RAM (128KB to 1MB), using banked memory to store audio data and a
timer-driven IRQ handler to feed samples to POKEY at rates up to 15 kHz.

The output XEX file is completely standalone: load it on an Atari (or
emulator), press SPACE, and it plays. No DOS, no runtime, no external
player needed.


## The Problem Being Solved

POKEY was designed as a synthesizer, not a PCM DAC. It has four tone
generators with frequency dividers and polynomial noise — great for sound
effects and music synthesis, but no direct "play this waveform" mode.

However, each channel has a 4-bit volume register (AUDC, bits 0–3) with a
volume-only mode (bit 4 = 1). Writing successive volume values at a fixed
rate produces PCM audio. This is the foundation of the player: a timer IRQ
fires thousands of times per second, each time writing a new volume value
to POKEY.

The challenges:

- **4-bit resolution per channel.** 16 levels is terrible for audio. Using
  multiple channels in parallel gives 31 levels (2ch), 46 levels (3ch), or
  61 levels (4ch), but this requires writing multiple registers per sample
  with careful level allocation to avoid voltage glitches.

- **CPU budget.** At 8 kHz, the IRQ fires every 224 cycles (PAL). The
  handler must save registers, acknowledge the interrupt, read the next
  sample from banked memory, look up AUDC values, write POKEY, and restore
  registers — all within that window while leaving some CPU for the main
  loop.

- **Memory.** 8 kHz mono audio consumes 8 KB/sec. A 130XE has only 64 KB
  of extended RAM (four 16 KB banks). Without compression, that's 8 seconds
  of audio. With VQ compression at vec_size=4, it's about 32 seconds.
  A 1 MB machine gets roughly 8.5 minutes.


## Architecture Overview

```
Audio File (WAV/MP3/FLAC)
        │
        ▼
┌─────────────────────────────────┐
│  Python Encoder (cli.py)        │
│                                 │
│  1. Load & resample to ~8 kHz   │
│  2. DC-block & normalize        │
│  3. Optional treble pre-emphasis│
│  4. Quantize to POKEY levels    │
│  5. Compress (VQ / LZ / raw)    │
│  6. Pack into 16 KB banks       │
│  7. Generate assembly project   │
│  8. Assemble → XEX binary       │
└──────────┬──────────────────────┘
           │
     ┌─────┴──────┐
     ▼            ▼
  output.xex    output_asm/
  (ready to     (MADS project
   run)          for hacking)
```

### How XEX Gets Built

The encoder generates a self-contained 6502 assembly project (MADS-
compatible source files + per-song data), then assembles it into a
single XEX binary. Assembly is handled by:

1. **External MADS** — if `mads` / `mads.exe` is found next to the
   encoder or in the system PATH, it is used automatically.
2. **Built-in assembler** — a MADS-compatible subset (`simple_mads`)
   bundled with the encoder. Covers all instructions, addressing modes,
   directives, and conditional assembly needed by the player. Requires
   no external tools.

The method used is shown in the output: `[mads]` or `[built-in]`.

The `-a` flag saves the assembly project to disk so it can be inspected,
modified, or built manually with MADS.


## Encoding Pipeline

### 1. Audio Loading (`audio.py`)

Loads audio via the `soundfile` library (WAV, MP3, FLAC, OGG, AIFF
natively) or falls back to `ffmpeg` for tracker formats (MOD, XM, S3M).
Stereo input is mixed to mono. The result is a float32 array normalized
to [-1, 1].

### 2. Resampling

Downsamples to the target rate (default 8 kHz) using `scipy.signal.resample`.
The exact rate is constrained by POKEY's timer hardware — the divisor
register is 8-bit, so only certain rates are achievable:

```
actual_rate = PAL_CLOCK / (divisor + 1)
```

where PAL_CLOCK = 1,773,447 Hz (with AUDCTL bit 6 set for 1.77 MHz clock).
The encoder picks the divisor closest to the requested rate. At 8 kHz
the actual rate is 7988.5 Hz (divisor $DD).

### 3. DC Block & Normalize

A 20 Hz high-pass filter removes DC offset (critical when you only have
31 quantization levels — even a small offset wastes dynamic range). Then
the signal is peak-normalized to [-1, 1].

### 4. Treble Pre-Emphasis (`enhance.py`, optional `-e` flag)

POKEY's DAC uses zero-order hold: each sample is held as a constant
voltage until the next write. This creates a sinc(f/fs) rolloff that
attenuates treble. At 8 kHz, 3 kHz is down 2.1 dB, and it gets worse
near Nyquist.

Pre-emphasis applies the inverse curve as a 15-tap FIR filter at 70%
blend strength, so the combined response (pre-emphasis × ZOH rolloff)
is approximately flat. This is a waveform modification applied before
quantization — it works with all compression modes.

### 5. Quantization (`tables.py`)

The core challenge. POKEY's voltage output per channel is nonlinear —
measured from a real AMI C012294 chip:

```
Volume 0:  0.000V    Volume 8:  0.301V
Volume 1:  0.033V    Volume 9:  0.333V
Volume 4:  0.144V    Volume 12: 0.444V
Volume 7:  0.245V    Volume 15: 0.546V
```

With multiple channels, the voltages sum on the analog output. The encoder
builds a combined voltage table using a single-step allocation strategy:
each consecutive level changes exactly one channel's volume register by +1.
This guarantees that the 6502 IRQ handler can reach any adjacent level by
writing a single register, eliminating intermediate voltage glitches during
the write sequence.

The quantizer maps each input sample to the nearest level in this table.
Two modes:

- **Nearest (no noise shaping):** Simple closest-match. Used for VQ
  compression because noise shaping creates patterns that k-means clustering
  cannot efficiently represent (adds ~3 dB of noise).

- **Noise-shaped (error diffusion):** Quantization error from each sample
  is added to the next sample's target. This pushes quantization noise to
  higher frequencies where it is less audible. Used for RAW and LZ modes.

Channel counts and their tradeoffs:

| Channels | Levels | CPU/IRQ | Quality | Notes |
|----------|--------|---------|---------|-------|
| 1 | 16 | ~80cy (36%) | Rough, audible steps | Minimal CPU |
| 2 | 31 | ~88cy (39%) | Good balance | Default |
| 3 | 46 | ~96cy (43%) | Smoother | |
| 4 | 61 | ~104cy (46%) | Smoothest | Slight write-order artifacts |

### 6. Compression

Three modes, each with different tradeoffs:

#### VQ (Vector Quantization) — Default

Groups consecutive samples into fixed-length vectors (2, 4, 8, or 16
samples). Each 16 KB bank has its own 256-entry codebook trained by
k-means clustering on that bank's vectors. The index stream is one byte
per vector, so vec_size=4 gives ~4× compression.

Bank format:
```
$4000  ┌──────────────────────┐
       │ Codebook             │  256 × vec_size bytes
       │ (256 vectors)        │
       ├──────────────────────┤
       │ Index stream         │  1 byte per vector
       │ (codebook indices)   │
$7FFF  └──────────────────────┘
```

Quality depends on vec_size:
- vec_size=2: ~42 dB SNR, near-transparent, 2× compression
- vec_size=4: ~18 dB SNR, good quality, ~4× compression
- vec_size=8: ~14 dB, noticeable artifacts, ~8× compression

Noise shaping is deliberately disabled for VQ. Plain rounding produces
vectors like [15, 15, 15, 15] that get exact codebook matches. Noise-shaped
[14, 16, 15, 14] gets approximated with ±1 error per element, creating
audible white noise. This single insight improved SNR by 3.4 dB.

**Noise gate (`-g N`).** Controls how aggressively near-silence is
snapped to true zero (0–100%, default 5).  When gate > 0, codebook
index 0 is reserved for silence `[0,0,...,0]` and vectors where every
sample falls below `max_level × gate / 100` are excluded from k-means
training.  The default of 5% is very mild — it only catches vectors
that are essentially zero already.  Higher values (20–50) clean up
noisy sources but may suppress quiet passages.

With `-g 0`, all 256 codebook entries are trained on the actual data.
A silence entry is still ensured (replacing the least-used code) so
that zero-padded bank tails decode cleanly.

#### DeltaLZ (Lossless)

Delta-encodes the sample stream (each value = current − previous, mod 256),
then applies an LZ-style compressor with a 16 KB sliding window. The
6502 decompressor runs inside the IRQ handler as a state machine, decoding
one byte per interrupt. Typical compression ratio on music: ~1.3×.

#### RAW (No Compression)

One byte per sample, no processing. Simplest player, lowest CPU usage,
but shortest duration per megabyte of RAM.


## The 6502 Player

### XEX Loading Sequence

The XEX file loads in multiple stages:

1. **INIT 1 — ROM Copy** (`copy_rom.asm`): Copies OS ROM ($C000–$CFFF,
   $D800–$FFFF) to underlying RAM. This is necessary because the player
   disables ROM (PORTB bit 0 = 0) to access extended RAM banks, which
   would otherwise hide the character set at $E000 and the IRQ/NMI vectors
   at $FFFA–$FFFF. The copy uses `DEC PORTB` / `INC PORTB` to toggle
   between ROM-read and RAM-write for each byte.

2. **INIT 2 — Memory Detection** (`mem_detect.asm`): Probes all 64
   possible extended RAM banks by writing a unique signature to each bank
   and reading them back. Banks that return a different signature are
   aliases (mirrors of physical RAM) and are skipped. Results are stored
   in `TAB_MEM_BANKS` ($0480): slot 0 = $FF sentinel, slots 1–64 = PORTB
   values for each detected physical bank. The table is zeroed first so
   that undetected slots read as $00 (the splash screen memory check relies
   on this).

3. **INIT 3..N — Bank Loading** (`banks.asm`): For each bank of audio
   data, a small INIT stub switches PORTB to the target extended bank,
   then the XEX loader fills $4000–$7FFF with the bank's codebook and
   index data. A post-load INIT stub switches back to main RAM.

4. **RUN — Main Player** (`start` in `splash.asm`): Disables OS interrupts,
   sets up NMI/IRQ vectors, checks detected memory against requirements,
   shows splash screen, waits for SPACE.

### Memory Map

```
$0000-$007F   (OS zero page)
$0080-$008F   Player zero page (VQ state: bank_idx, vec_ptr, cached, etc.)
$0480-$04C1   TAB_MEM_BANKS (detected bank PORTB values)
$0600-$060F   INIT stub area (reused during XEX loading)
$2000-$3FFF   Player code + AUDC lookup tables + splash data
$4000-$7FFF   Extended RAM bank window (switched via PORTB)
$8000-$BFFF   LZ decode buffer (DeltaLZ mode only)
$C000-$CFFF   RAM (copy of OS ROM)
$D000-$D7FF   Hardware I/O (POKEY, ANTIC, GTIA, PIA)
$D800-$FFFF   RAM (copy of OS ROM, includes charset at $E000)
```

### IRQ Handler — VQ Mode (`irq_vq.asm`)

The IRQ fires at the sample rate (~8 kHz) and executes this sequence:

```
1. Save A, X to zero page                              6 cy
2. Acknowledge POKEY IRQ (clear+set IRQEN)             12 cy
3. Check playing flag                                   5 cy
4. Load cached codebook value → AUDC LUT → write POKEY
     2ch: LDX cached + 2×(LDA tab,X + STA AUDCn)      19 cy
5. Bank in: LDX bank_idx, LDA portb_table,X, STA PORTB 11 cy
6. Read next codebook byte: LDY vec_pos, LDA (ptr),Y    8 cy
7. Advance vec_pos, check boundary                      6 cy
8. Bank out: LDA #$FE, STA PORTB                        6 cy
9. Restore A, X, RTI                                   12 cy
                                              Total:  ~85 cy
```

The "write-first-then-read" strategy ensures POKEY gets its new value at
a fixed cycle offset from the IRQ entry, regardless of whether this is a
fast-path or boundary-path iteration. This prevents timing jitter that
would be audible as noise.

#### Fast Path vs Boundary Path

Most IRQs (255 out of every 256 for vec_size=4, or 3 out of every 4
samples within a vector) are fast-path: read the next byte from the
current codebook vector and advance the position counter.

Every `vec_size` IRQs, the vector position wraps to 0 and the handler
takes the boundary path: reads the next index byte from the index stream
(the bank is already switched in from step 5), looks up the new vector
address from `vq_lo_tab` / `vq_hi_tab`, and advances the index pointer.

The boundary path also checks for bank exhaustion using a BMI optimization:
when `INC idx_ptr_hi` causes the high byte to reach $80 (the end of the
$4000–$7FFF bank window), the N flag is set and BMI branches to signal the
main loop. This saves 8 cycles on the common case compared to the naive
`LDA / CMP #$80 / BCC` approach, because the common case (low byte didn't
wrap) skips the check entirely.

#### Bank Transitions

When the IRQ signals bank exhaustion (sets `ZP_NEED_BANK = $FF`), the
main loop handles the transition:

```
SEI                          ; Atomic: prevent IRQ seeing inconsistent state
  clear NEED_BANK
  increment BANK_IDX
  check if all banks consumed → stop
  reset IDX_PTR to start of new bank's index stream
CLI
```

This is the "direct bank read" architecture: the codebook lives in banked
memory and the IRQ switches banks on every single sample. The alternative
(copying the codebook to main RAM at each bank transition) would require
disabling interrupts for 5–20 ms during the copy, causing audible clicks.
The direct approach adds ~11 cycles per IRQ for bank switching but
eliminates transition artifacts entirely.

### AUDC Lookup Tables

Each POKEY channel needs a specific AUDC register value for a given sample
level. The mapping is precomputed into 256-byte tables (one per channel),
allowing the IRQ to convert a codebook value to register writes with a
single `LDA tab,X` per channel — no runtime computation.

The tables encode the single-step allocation: for 2-channel mode with
index 17 mapping to volumes (2, 15), the table stores `audc1_tab[17] =
$12` (volume-only | 2) and `audc2_tab[17] = $1F` (volume-only | 15).

### Splash Screen

A simple two-line ANTIC Mode 2 display showing the player configuration:

```
  STREAM PLAYER  -  [SPACE] TO PLAY
          2CH  7988HZ  VQ4  80KB
```

Before showing the splash, the player checks if enough extended RAM banks
were detected. If not, it shows an error screen with a red border and
halts. This prevents crashes on machines with insufficient memory.

The OS ROM charset has already been copied to RAM by the INIT segment, so
ANTIC can read character definitions even though ROM is disabled.


## The Assembly Architecture

The encoder generates a self-contained assembly project for each song.
The project combines static player code (in the `asm/` directory, shared
across all songs) with generated data files (per-song configuration,
AUDC tables, bank data).

### Static Files (in `asm/`, version-controlled)

| File | Purpose |
|------|---------|
| `stream_player.asm` | Master file, includes everything via `icl` |
| `atari.inc` | Hardware register definitions and constants |
| `zeropage_{vq,lz,raw}.inc` | Zero page variable allocations ($80–$8F) |
| `copy_rom.asm` | INIT: copy OS ROM to RAM (with PORTB toggle) |
| `mem_detect.asm` | INIT: detect extended RAM banks |
| `splash.asm` | Startup, memory check, splash screen, key wait |
| `player_{vq,lz,raw}.asm` | Play init, main loop, bank transitions |
| `irq_{vq,lz,raw}.asm` | IRQ handler with conditional assembly |
| `pokey_setup.asm` | POKEY timer and channel initialization |

### Generated Files (per-song, from `asm_project.py`)

| File | Purpose |
|------|---------|
| `config.asm` | Constants: N_BANKS, VEC_SIZE, POKEY_CHANNELS, etc. |
| `audc_tables.asm` | 256-byte AUDC lookup tables per channel |
| `portb_table.asm` | PORTB bank-select table (filled at runtime) |
| `vq_tables.asm` | VQ_LO/VQ_HI: codebook index → address lookup (VQ only) |
| `splash_data.asm` | Screen text in ANTIC Mode 2 screen codes |
| `bank_XX.asm` | Per-bank codebook + index data as `.byte` arrays |
| `banks.asm` | INIT stubs for loading banks into extended RAM |

### Conditional Assembly

The IRQ handler uses MADS `.if` directives to compile only the code needed
for the configured number of channels:

```asm
.if POKEY_CHANNELS >= 2
    lda audc2_tab,x
    sta AUDC2
.endif
```

This means the same `irq_vq.asm` source handles all 1–4 channel
configurations. Similarly, `stream_player.asm` includes the correct
player/IRQ/zeropage files based on the `COMPRESS_MODE` constant.


## Key Design Decisions

### Why Multiple Channels for Mono Audio?

POKEY has 4 channels, each with only 16 volume levels. Playing mono audio
through a single channel gives terrible 4-bit quality. By writing
coordinated volumes to 2–4 channels simultaneously, the analog outputs
sum to give 31–61 effective levels. This is the same principle as
multi-bit delta-sigma DACs.

The tradeoff is CPU time: each additional channel costs ~8 cycles per IRQ
(one LDA + one STA to an absolute address).

### Why Noise Shaping is Disabled for VQ

Noise shaping (error diffusion) works beautifully for direct playback —
it pushes quantization noise above the audible range. But VQ clusters
similar vectors together, and noise-shaped vectors have high-frequency
patterns that increase the effective diversity of the vector space.
K-means with 256 codes cannot represent this diversity, so each codebook
entry becomes a compromise that matches nothing well. The result is
spectrally flat white noise at about -15 dB.

Without noise shaping, samples are quantized to the nearest level. This
produces many identical or near-identical vectors (e.g., [15, 15, 15, 15]
during sustained notes) that get perfect codebook matches. The measured
improvement: 14.5 dB → 17.9 dB SNR for vec_size=4.

### Why Direct Bank Read Instead of Codebook Copy

An alternative architecture would copy each bank's codebook (256 × vec_size
bytes, up to 4 KB) to main RAM when switching banks, so the IRQ could read
from a fixed address without bank switching. This would require disabling
interrupts during the copy — a 5–20 ms gap that causes an audible click
on every bank transition.

The current architecture banks in on every IRQ, reads from $4000+, then
banks out. This costs ~11 extra cycles per IRQ but produces zero transition
artifacts. The bank switch is safe because `PORTB AND #$FE` keeps OS ROM
disabled, so the IRQ vector at $FFFE stays in RAM regardless of which
extended bank is mapped.

### Why the BMI Optimization Works

The bank window spans $4000–$7FFF. When the index pointer's high byte is
incremented past $7F to $80, bit 7 is set, which sets the 6502's N
(negative) flag. BMI (Branch if Minus) tests exactly this flag. So instead
of:

```asm
LDA idx_ptr_hi    ; 3 cy
CMP #$80          ; 2 cy   ← always runs
BCC continue      ; 3 cy
```

We use:

```asm
INC idx_ptr_hi    ; 5 cy   ← sets N flag from result
BMI exhausted     ; 2 cy   ← tests N directly
```

The INC that we already need to do for pointer arithmetic also gives us
the comparison for free. On the common path (low byte didn't wrap, 255
out of 256 boundary calls), the entire check is skipped — saving 8 cycles.

### Why PORTB Toggle in ROM Copy

On Atari XL/XE, addresses $C000–$FFFF are shared between ROM and RAM.
Which one responds depends on PORTB bit 0: set = ROM visible, clear = RAM
visible. A naive `LDA (ptr),Y / STA (ptr),Y` reads and writes the same
layer — if ROM is visible, you read ROM and write ROM (which is a no-op).

The correct technique toggles PORTB between the read and write:

```asm
LDA (ptr),Y       ; Read from ROM  (bit 0 = 1)
DEC PORTB         ; Switch to RAM  (bit 0 = 0)
STA (ptr),Y       ; Write to RAM
INC PORTB         ; Switch back to ROM
```

`DEC`/`INC` is faster than `LDA`/`AND`/`ORA`/`STA` and preserves all
other PORTB bits.


## File Reference

### Python Modules (`src/stream_player/`)

| Module | Purpose |
|--------|---------|
| `cli.py` | Command-line interface, orchestrates the pipeline |
| `audio.py` | Load, resample, DC block, normalize, quantize |
| `tables.py` | POKEY voltage tables, multi-channel allocation, quantizers |
| `vq.py` | VQ encoder: k-means, per-bank codebook, noise gate |
| `compress.py` | DeltaLZ compressor with 16 KB window tracking |
| `enhance.py` | Treble pre-emphasis FIR |
| `asm_project.py` | Assembly project generator + MADS/built-in dispatch |
| `layout.py` | Bank packing, PORTB tables, DBANK probe order |
| `splash_utils.py` | Screen code conversion for splash display |
| `errors.py` | Exception hierarchy |

### Built-in Assembler (`src/stream_player/simple_mads/`)

| Module | Purpose |
|--------|---------|
| `parser.py` | Phase 1: source → flat statement list (parse once) |
| `assembler.py` | Phase 2–3: resolve symbols → emit XEX binary |
| `expressions.py` | Expression evaluator (arithmetic, lo/hi byte) |
| `encoder.py` | 6502 instruction encoder (all addressing modes) |
| `opcodes.py` | Opcode table (56 instructions × all modes) |
| `xex.py` | Atari XEX binary format builder |

### Assembly Files (`asm/`)

| File | Purpose |
|------|---------|
| `stream_player.asm` | Master, includes all others via `icl` |
| `atari.inc` | Hardware registers, constants |
| `zeropage_{vq,lz,raw}.inc` | ZP variable layout per compression mode |
| `copy_rom.asm` | ROM→RAM copy (INIT segment) |
| `mem_detect.asm` | Bank detection (INIT segment) |
| `splash.asm` | UI + startup + memory check |
| `player_{vq,lz,raw}.asm` | Player init + main loop per mode |
| `irq_{vq,lz,raw}.asm` | IRQ handler per mode |
| `pokey_setup.asm` | POKEY timer and channel initialization |


## Building and Running

### Prerequisites

- Python 3.8+ with `numpy`, `scipy`, `soundfile`
- Optional: MADS assembler (placed next to encoder or in PATH)
- Atari 800XL/XE with 128KB+ RAM, or emulator (Altirra recommended)

### Quick Start

```bash
pip install -r requirements.txt
./encode.sh song.mp3                 # → outputs/song.xex (VQ4, 2ch, 8kHz)
./encode.sh song.mp3 -a             # → outputs/song_asm/ (ASM project only)
./encode.sh song.mp3 -c lz          # DeltaLZ lossless
./encode.sh song.mp3 -s 2           # VQ2 near-transparent
./encode.sh song.mp3 -n 4 -e        # 4ch + treble boost
./encode.sh song.mp3 -g 0           # VQ with noise gate off
./encode.sh song.mp3 -g 20          # VQ with stronger noise gate
```

### Standalone Executable

Build a single-file `encode` / `encode.exe` with PyInstaller:

```bash
./build.sh                            # Linux/macOS → dist/encode
build.bat                             # Windows → dist\encode.exe
```

### Duration Estimates (8 kHz, mono, 2ch)

| RAM | RAW | DeltaLZ | VQ4 | VQ2 |
|-----|-----|---------|-----|-----|
| 130XE (64KB) | ~8s | ~10s | ~32s | ~16s |
| 256KB | ~32s | ~42s | ~2:08 | ~1:04 |
| 512KB | 1:05 | ~1:25 | ~4:16 | ~2:08 |
| 1MB | 2:11 | ~2:50 | ~8:32 | ~4:16 |
