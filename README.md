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
# Basic: convert MP3 to XEX (default: 2ch, DeltaLZ compressed)
./encode.sh song.mp3

# Perceptual enhancement — better on real hardware
./encode.sh song.mp3 -e

# VQ compression — fits ~7x more audio (lossy, ~-3dB)
./encode.sh song.mp3 -c vq

# VQ + enhanced — best quality per byte on real Atari
./encode.sh song.mp3 -c vq -e

# No compression (fastest encode, least audio fits)
./encode.sh song.mp3 -c off

# 4 channels (louder, slight roughness from write artifacts)
./encode.sh song.mp3 -n 4

# Custom output name
./encode.sh song.mp3 -o my_song.xex

# Lower sample rate = longer recording fits in memory
./encode.sh song.mp3 -r 6000

# Generate MADS assembly project (for manual builds / customization)
./encode.sh song.mp3 --asm -o my_project

```

### Options

| Option | Description |
|---|---|
| `-c off\|lz\|vq` | Compression mode (default: lz). See below. |
| `-s N` | VQ vector size: 4, 8, 16 (default: 8). Only with `-c vq`. |
| `-n N` | POKEY channels: 1–4 (default: 2). More = louder but rougher. |
| `-e, --enhance` | Treble pre-emphasis for real hardware. See below. |
| `-o FILE` | Output .xex file or directory for `--asm`. Default: `<input>.xex` |
| `-r RATE` | Sample rate in Hz (default: 8000). Lower = longer duration. |
| `--asm` | Output a MADS assembly project instead of a ready XEX. |
| `--max-banks N` | Max extended memory banks (default: 64 = 1MB). |
| `-v` | Verbose — shows compression verification. |

### Compression modes

| Mode | Ratio | Quality | Description |
|---|---|---|---|
| `lz` (DeltaLZ) | ~1.3× | Lossless | Delta encoding + LZ compression |
| `vq` | ~7× (vec=8) | -3 dB | Vector Quantization with per-bank codebook |
| `off` | 1× | Lossless | Raw sample stream, no compression |

VQ uses 256-entry codebooks trained per bank via k-means. Each codebook
index selects a fixed-length vector of samples, so the player only reads
one index byte per `vec_size` samples — most IRQs need no bank switch.

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

### Audio formats

WAV, MP3, FLAC, OGG/Vorbis, and AIFF are handled by the `soundfile`
package (bundled libsndfile) — no external binaries needed on any platform.
Tracker formats (MOD, XM, S3M, IT) require [ffmpeg](https://ffmpeg.org).

## How much audio fits?

At 8 kHz mono (1 byte per sample = 8 KB/s):

| Memory | Raw | DeltaLZ (~1.3×) | VQ vec=8 (~7×) |
|---|---|---|---|
| 130XE stock (64KB) | ~8s | ~10s | ~57s |
| 256KB expanded | ~32s | ~42s | ~3:48 |
| 512KB expanded | ~1:05 | ~1:25 | ~7:36 |
| 1MB expanded | ~2:11 | ~2:50 | ~15:12 |

VQ compression ratio depends on vec_size: vec=4 gives ~3.8×, vec=8 gives
~7×, vec=16 gives ~12×. Larger vectors compress more but sacrifice quality
(RMSE increases from ~0.5 to ~0.7 levels on a 31-level scale).

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
audible range, then compresses the stream with DeltaLZ. The Atari player
decompresses in real-time inside the POKEY timer IRQ handler — no double
buffering, no audio gaps.

## Project structure

```
stream-player/
├── encode.sh           Linux/macOS launcher
├── encode.bat          Windows launcher
├── requirements.txt    Python dependencies (numpy, scipy, soundfile)
├── README.md
├── src/
│   └── stream_player/  Python package
│       ├── cli.py          Command-line interface
│       ├── audio.py        Audio loading, resampling, encoding
│       ├── enhance.py      Perceptual enhancement (dynamics, ZOH pre-emphasis)
│       ├── tables.py       POKEY voltage tables & quantization
│       ├── compress.py     DeltaLZ compressor/decompressor
│       ├── vq.py           VQ encoder/decoder (per-bank codebook)
│       ├── player_code.py  6502 machine code generator
│       ├── asm_output.py   MADS assembly project generator
│       ├── asm6502.py      Minimal 6502 assembler
│       ├── xex.py          Atari XEX binary builder
│       ├── layout.py       Bank memory layout
│       └── errors.py       Exception classes
└── tests/
    └── test_stream_player.py
```

## Running tests

```bash
cd stream-player
PYTHONPATH=src python -m unittest tests.test_stream_player -v
```

## License

Public domain. Use freely for any purpose.
