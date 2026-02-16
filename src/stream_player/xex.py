"""Build Atari XEX (DOS II binary load format) files.

XEX format:
    $FF $FF                       — Binary file header
    start_lo start_hi end_lo end_hi   — Segment address range
    [data bytes]                  — Segment data (end - start + 1 bytes)
    [optional $FF $FF]            — Separator between segments
    ...more segments...

Special segments:
    $02E0-$02E1: RUN address (auto-executed after loading)
    $02E2-$02E3: INIT address (executed during loading)
"""

from .errors import XEXBuildError
from .layout import BANK_BASE, BANK_END, DBANK_TABLE


class XEXBuilder:
    """Build an Atari XEX binary with segments."""
    
    def __init__(self):
        self._segments = []  # [(start_addr, data_bytes)]
        self._run_addr = None
    
    def add_segment(self, start_addr: int, data: bytes):
        """Add a data segment at the given address."""
        if not data:
            return
        if start_addr < 0 or start_addr > 0xFFFF:
            raise XEXBuildError(f"Invalid segment address: ${start_addr:04X}")
        end = start_addr + len(data) - 1
        if end > 0xFFFF:
            raise XEXBuildError(
                f"Segment at ${start_addr:04X} overflows: "
                f"{len(data)} bytes would reach ${end:04X}")
        self._segments.append((start_addr, bytes(data)))
    
    def add_init_segment(self, code: bytes, run_addr: int):
        """Add an INIT segment: code at $2E00, INIT vector at $02E2.
        
        The code is loaded, then the INIT vector causes it to execute
        during the load process. Used for bank switching during load.
        """
        # First: the code itself
        code_addr = 0x2E00  # Temp location for init stubs
        self.add_segment(code_addr, code)
        # Then: INIT vector pointing to the code
        self.add_segment(0x02E2, bytes([code_addr & 0xFF, (code_addr >> 8) & 0xFF]))
    
    def set_run_address(self, addr: int):
        """Set the RUN address (executed after all segments loaded)."""
        self._run_addr = addr
    
    def add_bank_data(self, bank_idx: int, data: bytes):
        """Add a bank switching stub + data load for one bank.
        
        Creates:
        1. An INIT stub that switches PORTB to the target bank
        2. A data segment at $4000-$7FFF with the bank's data
        """
        if bank_idx >= len(DBANK_TABLE):
            raise XEXBuildError(f"Bank index {bank_idx} exceeds maximum")
        
        portb = DBANK_TABLE[bank_idx]
        
        # INIT stub: switch to this bank
        # LDA #portb / STA $D301 / RTS
        stub = bytes([0xA9, portb, 0x8D, 0x01, 0xD3, 0x60])
        self.add_init_segment(stub, 0x2E00)
        
        # Data at bank window
        self.add_segment(BANK_BASE, data)
    
    def build(self) -> bytes:
        """Assemble all segments into a complete XEX binary.
        
        Returns:
            Complete XEX file as bytes
        """
        if not self._segments and self._run_addr is None:
            raise XEXBuildError("No segments to build")
        
        out = bytearray()
        
        for i, (start, data) in enumerate(self._segments):
            end = start + len(data) - 1
            
            if i == 0:
                # First segment includes $FF $FF header
                out.append(0xFF)
                out.append(0xFF)
            
            # Segment header
            out.append(start & 0xFF)
            out.append((start >> 8) & 0xFF)
            out.append(end & 0xFF)
            out.append((end >> 8) & 0xFF)
            
            # Segment data
            out.extend(data)
        
        # RUN segment
        if self._run_addr is not None:
            out.append(0xE0)
            out.append(0x02)
            out.append(0xE1)
            out.append(0x02)
            out.append(self._run_addr & 0xFF)
            out.append((self._run_addr >> 8) & 0xFF)
        
        return bytes(out)
    
    @property
    def segment_count(self) -> int:
        return len(self._segments)
    
    @property
    def total_size(self) -> int:
        """Total size of all data (excluding headers)."""
        return sum(len(d) for _, d in self._segments)


def build_xex(player_code: bytes, player_origin: int,
              bank_data: list, run_addr: int,
              charset_init: bytes = None) -> bytes:
    """Build a complete XEX with player code and banked data.
    
    Args:
        player_code: Assembled player machine code
        player_origin: Origin address of player code
        bank_data: List of bytes objects, one per bank
        run_addr: Address to execute after loading
        charset_init: Optional INIT code to copy charset ROM→RAM.
                      If provided, replaces the simple PORTB restore.
        
    Returns:
        Complete XEX binary
    """
    xex = XEXBuilder()
    
    # Player code segment (first, before any bank switching)
    xex.add_segment(player_origin, player_code)
    
    # Bank data segments (each with INIT stub to switch banks)
    for bank_idx, data in enumerate(bank_data):
        if data:
            # Pad to full bank size if needed (some loaders expect it)
            xex.add_bank_data(bank_idx, data)
    
    # After all banks loaded: copy charset from ROM to RAM, then
    # leave PORTB=$FE (ROM disabled). The charset copy INIT does both.
    if charset_init:
        xex.add_init_segment(charset_init, 0x2E00)
    elif bank_data:
        # Fallback: simple PORTB=$FE restore (no splash screen)
        # LDA #$FE / STA $D301 / RTS
        restore_stub = bytes([0xA9, 0xFE, 0x8D, 0x01, 0xD3, 0x60])
        xex.add_init_segment(restore_stub, 0x2E00)
    
    xex.set_run_address(run_addr)
    
    return xex.build()
