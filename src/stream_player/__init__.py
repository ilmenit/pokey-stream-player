"""stream-player: Convert audio to Atari XEX with 4-channel POKEY playback.

Supports:
  - Any audio format (via soundfile: MP3, FLAC, OGG, WAV, AIFF)
  - 1CPS encoding (1-Channel-Per-Sample, zero wobble, 95-cycle IRQ)
  - Scalar encoding (legacy 4-register writes, 122-cycle IRQ)
  - Extended memory banking (up to 64 banks = 1MB)
  - RAW mode (uncompressed streaming from banks)
  - Compressed mode (LZ streaming with real-time 6502 decompression)
"""

__version__ = '2.0.0'
