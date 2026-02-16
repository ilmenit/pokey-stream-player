"""Bank packing for Atari XL/XE extended memory.

Bank window: $4000-$7FFF (16KB per bank)
PORTB at $D301 controls which bank is mapped.
"""

from .errors import BankOverflowError

BANK_SIZE = 16384     # 16KB per bank
BANK_BASE = 0x4000    # Start of bank window
BANK_END = 0x8000     # End of bank window
MAX_BANKS = 64        # Maximum possible banks (1MB)

# PORTB values for each bank (matches MADS @MEM_DETECT ordering)
DBANK_TABLE = [
    0xE3, 0xC3, 0xA3, 0x83, 0x63, 0x43, 0x23, 0x03,
    0xE7, 0xC7, 0xA7, 0x87, 0x67, 0x47, 0x27, 0x07,
    0xEB, 0xCB, 0xAB, 0x8B, 0x6B, 0x4B, 0x2B, 0x0B,
    0xEF, 0xCF, 0xAF, 0x8F, 0x6F, 0x4F, 0x2F, 0x0F,
    0xED, 0xCD, 0xAD, 0x8D, 0x6D, 0x4D, 0x2D, 0x0D,
    0xE9, 0xC9, 0xA9, 0x89, 0x69, 0x49, 0x29, 0x09,
    0xE5, 0xC5, 0xA5, 0x85, 0x65, 0x45, 0x25, 0x05,
    0xE1, 0xC1, 0xA1, 0x81, 0x61, 0x41, 0x21, 0x01,
]


def split_into_banks(data: bytes, max_banks: int = MAX_BANKS) -> list:
    """Split data into 16KB bank-sized chunks.
    
    Args:
        data: Raw or compressed audio data
        max_banks: Maximum number of banks allowed
        
    Returns:
        List of bytes objects, each up to BANK_SIZE bytes
        
    Raises:
        BankOverflowError if data exceeds available banks
    """
    n_banks_needed = (len(data) + BANK_SIZE - 1) // BANK_SIZE
    
    if n_banks_needed > max_banks:
        total_kb = len(data) // 1024
        max_kb = max_banks * BANK_SIZE // 1024
        raise BankOverflowError(
            f"Audio data ({total_kb}KB) exceeds available memory "
            f"({max_kb}KB in {max_banks} banks).\n"
            f"Try: lower sample rate, shorter audio, or enable compression.")
    
    if n_banks_needed == 0:
        return [data] if data else []
    
    banks = []
    pos = 0
    while pos < len(data):
        chunk = data[pos:pos + BANK_SIZE]
        banks.append(chunk)
        pos += BANK_SIZE
    
    return banks


def bank_portb_table(n_banks: int) -> list:
    """Get PORTB values for the first n_banks."""
    if n_banks > len(DBANK_TABLE):
        raise BankOverflowError(
            f"Requested {n_banks} banks but only {len(DBANK_TABLE)} available")
    return DBANK_TABLE[:n_banks]


def format_bank_info(banks: list, sample_rate: float, stereo: bool) -> str:
    """Format human-readable bank usage info."""
    n_banks = len(banks)
    total_bytes = sum(len(b) for b in banks)
    bytes_per_sec = sample_rate * (2 if stereo else 1)
    duration = total_bytes / bytes_per_sec if bytes_per_sec > 0 else 0
    
    lines = [
        f"  Banks: {n_banks} (of {MAX_BANKS} max)",
        f"  Memory: {total_bytes:,} bytes ({total_bytes // 1024}KB)",
        f"  Duration: {duration:.1f}s",
    ]
    
    if n_banks > 0:
        last_bank_pct = len(banks[-1]) * 100 // BANK_SIZE
        lines.append(f"  Last bank fill: {last_bank_pct}%")
    
    return '\n'.join(lines)
