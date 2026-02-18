# stream-player

Converts audio files to playable Atari 8-bit XEX binaries using
POKEY PCM from banked extended memory (XL/XE 130KB–1MB).

## Setup

**Prerequisites:** Python 3.8+. No external binaries needed — all audio decoding
is handled by the `soundfile` package (bundled libsndfile).

**Supported formats:** WAV, MP3, FLAC, OGG/Vorbis, AIFF.
Tracker formats (MOD, XM, S3M, IT) require [ffmpeg](https://ffmpeg.org).

```bash
pip install -r requirements.txt
```

## Usage

```
./encode.sh song.mp3                                # Linux/macOS
encode song.mp3                                      # Windows
python -m stream_player song.mp3                     # any platform (from project root, PYTHONPATH=src)
```

Running without arguments shows the full help screen.

### Examples

```bash
# Basic: convert MP3 to XEX (default: VQ4, 2ch, 8kHz)
./encode.sh song.mp3

# Perceptual enhancement — better on real hardware
./encode.sh song.mp3 -e

# VQ with smallest vectors — highest quality, least compression
./encode.sh song.mp3 -s 2

# DeltaLZ compression (lossless, lower compression ratio)
./encode.sh song.mp3 -c lz

# No compression (fastest encode, least audio fits)
./encode.sh song.mp3 -c off

# 4 channels (louder, slight roughness from write artifacts)
./encode.sh song.mp3 -n 4

# Custom output name
./encode.sh song.mp3 -o my_song.xex

# Lower sample rate = longer recording fits in memory
./encode.sh song.mp3 -r 6000

# Generate assembly project alongside the XEX
./encode.sh song.mp3 -a

# Assembly project only (no XEX)
./encode.sh song.mp3 --no-xex -a -o my_project
```

### Options

**Output targets:**

| Option | Default | Description |
|---|---|---|
| `-x, --xex` | ON | Generate .xex binary. |
| `--no-xex` | — | Skip .xex generation. Mutually exclusive with `-x`. |
| `-a, --asm` | OFF | Generate assembly project (compatible with MADS assembler). |
| `-o, --output FILE` | `outputs/<input>` | Output base name. Adds `.xex` and/or `_asm` suffix automatically. |

**Compression:**

| Option | Default | Description |
|---|---|---|
| `-c, --compression {off,lz,vq}` | `vq` | Compression mode. `vq` = Vector Quantization (lossy, high ratio), `lz` = DeltaLZ (lossless), `off` = raw samples. |
| `-s, --vec-size {2,4,8,16}` | `4` | VQ vector size. Smaller = better quality, less compression. Only used with `-c vq`. |

**Audio:**

| Option | Default | Description |
|---|---|---|
| `-r, --rate RATE` | `8000` | Sample rate in Hz. Lower = longer duration, less treble. |
| `-n, --channels {1,2,3,4}` | `2` | POKEY channels. More = louder but rougher. |
| `-e, --enhance` | OFF | Treble pre-emphasis to compensate POKEY DAC rolloff. See below. |
| `--mode {scalar,1cps}` | `scalar` | LZ encoding mode. `scalar` = multi-channel writes per IRQ. `1cps` = single write per IRQ (for 12+ kHz rates). Only used with `-c lz`. |

**Advanced:**

| Option | Default | Description |
|---|---|---|
| `--max-banks N` | `64` | Max extended memory banks (64 = 1MB). |
| `--no-noise-shaping` | OFF | Disable noise shaping. Slightly faster, lower quality. |
| `-v, --verbose` | OFF | Show compression verification details. |

### Compression modes

| Mode | Ratio | Quality | Description |
|---|---|---|---|
| `vq` (default) | ~3.8× (vec=4) | -1 dB | Vector Quantization with per-bank codebook |
| `lz` (DeltaLZ) | ~1.3× | Lossless | Delta encoding + LZ compression |
| `off` | 1× | Lossless | Raw sample stream, no compression |

VQ uses 256-entry codebooks trained per bank via k-means. Each codebook
index selects a fixed-length vector of samples, so the player only reads
one index byte per `vec_size` samples — most IRQs need no bank switch.

VQ compression ratio depends on vec_size: vec=2 gives ~1.9×, vec=4 gives
~3.8×, vec=8 gives ~7×, vec=16 gives ~12×. Larger vectors compress more
but sacrifice quality.

### Perceptual enhancement (`-e`)

The `--enhance` flag applies treble pre-emphasis that compensates for
POKEY's sample-and-hold output characteristic.

POKEY holds each sample as a constant voltage until the next IRQ write.
This zero-order hold creates a sinc(f/fs) rolloff: -0.9 dB at 2 kHz,
-2.1 dB at 3 kHz, -3.9 dB at Nyquist. The result sounds muffled
compared to the input. Pre-emphasis boosts treble by the inverse amount,
so the combined POKEY output is perceptually flat.

Uses a short (15-tap) FIR filter at 70% strength — the measured sweet
spot that improves 1-3 kHz SNR by +0.7 dB with zero increase in
quantization artifacts. Encode-time only; the player code is unchanged.

### Assembly and MADS

The `--asm` flag outputs a complete assembly project that can be built
with the [MADS](https://mads.atari8.info/) assembler:

```bash
mads stream_player.asm -o:output.xex
```

When generating a `.xex`, the tool first looks for MADS in the project
folder and system PATH. If found, it uses MADS. If not, it falls back
to the built-in assembler (a MADS-compatible subset). The method used is
shown in the output: `[mads]` or `[built-in]`.

### Audio formats

WAV, MP3, FLAC, OGG/Vorbis, and AIFF are handled by the `soundfile`
package (bundled libsndfile) — no external binaries needed on any platform.
Tracker formats (MOD, XM, S3M, IT) require [ffmpeg](https://ffmpeg.org).

## How much audio fits?

At 8 kHz mono (1 byte per sample = 8 KB/s):

| Memory | Raw | DeltaLZ (~1.3×) | VQ vec=4 (~3.8×) | VQ vec=8 (~7×) |
|---|---|---|---|---|
| 130XE stock (64KB) | ~8s | ~10s | ~30s | ~57s |
| 256KB expanded | ~32s | ~42s | ~2:01 | ~3:48 |
| 512KB expanded | ~1:05 | ~1:25 | ~4:06 | ~7:36 |
| 1MB expanded | ~2:11 | ~2:50 | ~8:18 | ~15:12 |

Lower the sample rate (`-r 6000`) for longer recordings at the cost of
reduced high-frequency content.

## Channels vs quality

Each POKEY channel adds 15 quantization levels but requires an extra
register write per sample. Sequential writes create brief intermediate
voltages (hardware limitation — registers can't be written atomically).

| Channels | Levels | Voltage range | IRQ cycles | Sound |
|---|---|---|---|---|
| 1 | 16 | 0.55V | ~80 | Clean, quiet |
| 2 (default) | 31 | 1.09V | ~88 | Clean, moderate volume |
| 3 | 46 | 1.64V | ~96 | Slight roughness |
| 4 | 61 | 2.18V | ~104 | Louder, noticeable roughness on vocals |

## How it works

The encoder quantizes audio to N-channel POKEY voltage levels using a
single-step allocation table (each consecutive level changes exactly one
AUDC register), applies noise shaping to push quantization error above the
audible range, then compresses the stream with VQ or DeltaLZ. The Atari
player decompresses in real-time inside the POKEY timer IRQ handler — no
double buffering, no audio gaps.

## Project structure

```
stream-player/
├── encode.sh           Linux/macOS dev launcher
├── encode.bat          Windows dev launcher
├── build.sh            Linux/macOS build script (→ standalone executable)
├── build.bat           Windows build script (→ standalone executable)
├── encode.spec         PyInstaller spec file
├── encode_entry.py     Entry point for frozen executable
├── requirements.txt    Python dependencies (numpy, scipy, soundfile)
├── README.md
├── asm/                Static assembly templates (copied into projects)
├── src/
│   └── stream_player/  Python package
│       ├── cli.py          Command-line interface
│       ├── audio.py        Audio loading, resampling, encoding
│       ├── enhance.py      Perceptual enhancement (dynamics, ZOH pre-emphasis)
│       ├── tables.py       POKEY voltage tables & quantization
│       ├── compress.py     DeltaLZ compressor/decompressor
│       ├── vq.py           VQ encoder/decoder (per-bank codebook)
│       ├── layout.py       Bank memory layout
│       ├── asm_project.py  Assembly project generator + MADS/built-in dispatch
│       ├── errors.py       Exception classes
│       └── simple_mads/    Built-in MADS-compatible 6502 assembler
│           ├── parser.py       Phase 1: source → flat statement list
│           ├── assembler.py    Phase 2-3: resolve symbols → emit XEX
│           ├── expressions.py  Expression evaluator (arithmetic, lo/hi byte)
│           ├── encoder.py      6502 instruction encoder (all addressing modes)
│           ├── opcodes.py      Opcode table (56 instructions × all modes)
│           └── xex.py          Atari XEX binary format builder
└── tests/
    └── test_stream_player.py
```

## Building standalone executable

You can build a single-file `encode` / `encode.exe` binary that requires
no Python installation to run. Uses [PyInstaller](https://pyinstaller.org).

### Quick build

```bash
# Windows
build.bat

# Linux / macOS
chmod +x build.sh
./build.sh
```

The executable appears in `dist/encode` (or `dist\encode.exe` on Windows).

### Build commands

| Command | Description |
|---|---|
| `build.bat` / `./build.sh` | Build the executable |
| `build.bat dist` / `./build.sh dist` | Build + create distribution zip/tar.gz |
| `build.bat clean` / `./build.sh clean` | Remove build artifacts |
| `build.bat check` / `./build.sh check` | Check dependencies without building |
| `build.bat install` / `./build.sh install` | Install Python dependencies only |

### Distribution contents

Running `build dist` creates a release folder with:

```
pokey-stream-player/
├── encode.exe       The standalone executable
├── mads.exe         MADS assembler (if found in bin/platform/)
├── README.md
└── LICENSE
```

Place `mads` / `mads.exe` next to `encode` to use the external MADS
assembler. Without it, the built-in assembler handles everything.

### Build requirements

Python 3.8+ with: `numpy`, `scipy`, `soundfile`, `pyinstaller`.
The build script installs these automatically.

## Running tests

```bash
cd stream-player
PYTHONPATH=src python -m unittest tests.test_stream_player -v
```

## License

Public domain. Use freely for any purpose.
