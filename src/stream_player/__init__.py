"""stream-player: Convert audio to Atari XEX with POKEY PCM playback.

Supports:
  - Any audio format (via soundfile: MP3, FLAC, OGG, WAV, AIFF)
  - 1-4 POKEY channel configurations (16-61 quantization levels)
  - VQ compression (vector quantization, ~4x, default)
  - DeltaLZ compression (lossless, ~1.3x)
  - RAW mode (uncompressed streaming from banks)
  - Extended memory banking (up to 64 banks = 1MB)
  - Assembly project generation (all modes)

Architecture:
  Python encoder generates per-song data files (.asm).
  Static hand-written .asm files contain all player logic.
  Built-in assembler produces the final .xex binary.
"""

__version__ = '3.2.0'
