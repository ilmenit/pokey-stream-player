"""Shared splash screen utilities for player_code.py and asm_gen_vq.py."""


def to_screen_codes(text: str) -> list:
    """Convert ASCII text to ANTIC Mode 2 screen codes (40 chars).

    ANTIC Mode 2 uses internal character codes, not ASCII:
      ASCII $20-$5F → screen code $00-$3F (space through underscore)
      ASCII $60-$7F → screen code $60-$7F (lowercase, kept as-is)
    """
    codes = []
    for ch in text[:40]:
        v = ord(ch)
        if 0x20 <= v <= 0x5F:
            codes.append(v - 0x20)
        elif 0x60 <= v <= 0x7F:
            codes.append(v)
        else:
            codes.append(0x00)
    # Pad to 40 characters
    codes.extend([0x00] * (40 - len(codes)))
    return codes


def format_info_line(pokey_channels, sample_rate, compress_mode='vq',
                     vec_size=4, ram_kb=64):
    """Format 40-column info line for splash screen.

    Args:
        pokey_channels: Number of POKEY channels (1-4)
        sample_rate: Actual sample rate in Hz
        compress_mode: 'vq', 'lz', or 'off'
        vec_size: VQ vector size (only used when compress_mode='vq')
        ram_kb: Total RAM required in KB
    """
    ch_str = f"{pokey_channels}CH"
    rate_str = f"{int(round(sample_rate))}HZ"

    if compress_mode == 'vq':
        comp_str = f"VQ{vec_size}"
    elif compress_mode == 'lz':
        comp_str = "DELTALZ"
    else:
        comp_str = "RAW"

    ram_str = f"{ram_kb}KB"
    line = f"{ch_str}  {rate_str}  {comp_str}  {ram_str}"
    return line.upper().center(40)[:40]
