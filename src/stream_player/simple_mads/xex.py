"""Atari XEX (DOS binary) format builder.

XEX format:
  $FF $FF              - Binary file signature
  start_lo start_hi    - Segment start address
  end_lo   end_hi      - Segment end address
  data bytes...        - Segment data (end - start + 1 bytes)
  [repeat for more segments]

Special addresses:
  $02E0-$02E1  RUNAD  - Run address (jumped to after loading)
  $02E2-$02E3  INITAD - Init address (called after each segment that sets it)
"""

import struct


class Segment:
    """One contiguous block of assembled code/data."""
    __slots__ = ('start', 'data')

    def __init__(self, start):
        self.start = start
        self.data = bytearray()

    @property
    def end(self):
        return self.start + len(self.data) - 1

    def __repr__(self):
        return f"Seg(${self.start:04X}-${self.end:04X}, {len(self.data)}b)"


def build_xex(segments):
    """Build XEX binary from a list of Segments.

    Returns:
        bytes: Complete XEX file data.
    """
    out = bytearray()
    for seg in segments:
        if not seg.data:
            continue
        out.extend(b'\xFF\xFF')
        start = seg.start & 0xFFFF
        end = (start + len(seg.data) - 1) & 0xFFFF
        out.extend(struct.pack('<HH', start, end))
        out.extend(seg.data)
    return bytes(out)


def make_init_segment(addr):
    """Create an INITAD ($02E2) segment pointing to addr."""
    seg = Segment(0x02E2)
    seg.data = bytearray(struct.pack('<H', addr & 0xFFFF))
    return seg
