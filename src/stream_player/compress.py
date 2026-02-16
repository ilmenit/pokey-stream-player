"""DeltaLZ — Audio-aware compression for POKEY sample streams.

Pipeline:
  1. Input: array of POKEY channel indices (0-60 quad, 0-30 dual)
  2. Delta encode: d[i] = (idx[i] - idx[i-1]) & 0xFF
  3. LZ compress with buffer-aware matching

The compressor tracks the 6502's 16 KB decode buffer position.
buf_pos is DERIVED from pos (not manually tracked) to prevent drift:

    buf_pos = (initial_buf_pos + pos) % buf_size

Guarantees for the 6502 decompressor:
  - Match source range never wraps (lz_match needs no wrap check)
  - Match offset never reaches past the last buffer wrap
  - Literal runs and match copies MAY cross the destination wrap boundary
    (the 6502 checks lz_dst wrap per byte, handling this correctly)

Token format:
  $00         End of block
  $01-$7F     Literal run: N delta bytes follow (N = 1-127)
  $80-$BF     Short match: length = (token & $3F) + 3, offset = next byte
  $C0-$FF     Long match:  length = (token & $3F) + 3, offset = next 2 bytes LE

Bank format:
  Byte 0:     Initial index value (delta accumulator seed)
  Byte 1+:    Compressed delta stream (DeltaLZ tokens)
"""

from .errors import CompressionError

# LZ parameters
MIN_MATCH = 3
MAX_MATCH = 66       # (0x3F + 3)
MAX_SHORT_OFF = 255
MAX_LONG_OFF = 16383
HASH_SIZE = 8192
CHAIN_LEN = 96
MAX_LITERAL = 127

# Block header: 1 byte (delta_acc seed)
HEADER_SIZE = 1

# Decode buffer size on 6502 ($8000-$BFFF)
DECODE_BUF_SIZE = 16384


def compress_bank(indices: bytes, prev_value: int = 0,
                  buf_pos: int = 0, use_delta: bool = True) -> tuple:
    """Compress one bank's worth of POKEY indices using LZ (optionally with delta).

    Args:
        indices: Raw index values for this bank
        prev_value: Last index value from previous bank (delta continuity)
        buf_pos: Current position in the 6502 decode buffer (0-16383)
        use_delta: True for DeltaLZ (scalar mode), False for raw LZ (1CPS mode)

    Returns:
        (compressed_data, new_buf_pos) — bank bytes and updated buffer position
    """
    if not indices:
        return bytes([prev_value & 0xFF, 0x00]), buf_pos

    if use_delta:
        # Delta encode
        deltas = bytearray(len(indices))
        deltas[0] = (indices[0] - prev_value) & 0xFF
        for i in range(1, len(indices)):
            deltas[i] = (indices[i] - indices[i - 1]) & 0xFF
        to_compress = bytes(deltas)
    else:
        # Raw LZ — no delta preprocessing
        to_compress = bytes(indices)

    compressed, new_buf_pos = _lz_compress(to_compress, buf_pos)

    header = bytes([prev_value & 0xFF])
    return header + compressed, new_buf_pos


def compress_banks(indices: bytes, bank_size: int = 16384,
                   max_banks: int = 64, progress_fn=None,
                   use_delta: bool = True) -> tuple:
    """Split indices into banks, filling each bank as full as possible.

    Uses binary search to find the maximum chunk size that compresses
    to fit in each bank.  The decode buffer position carries across banks.

    Args:
        indices: Raw index values (0-60 for quad, 0-30 for dual, or 1CPS packed)
        bank_size: Physical bank size in bytes (16384)
        max_banks: Maximum number of banks
        progress_fn: Optional callback(samples_done, total, bank_count)
        use_delta: True for DeltaLZ (scalar), False for raw LZ (1CPS)

    Returns:
        (banks, samples_compressed)
    """
    if not indices:
        return [], 0

    total = len(indices)
    banks = []
    pos = 0
    prev_val = 0
    buf_pos = 0

    # Initial compression ratio estimate
    sample = indices[:min(bank_size, total)]
    sample_comp, _ = compress_bank(sample, 0, 0, use_delta)
    est_ratio = len(sample_comp) / len(sample) if len(sample) > 0 else 0.5
    chunk_guess = max(bank_size, int(bank_size / max(est_ratio, 0.05)))

    while pos < total and len(banks) < max_banks:
        remaining = total - pos

        # Quick check: does everything remaining fit in one bank?
        comp_all, bp_all = compress_bank(
            indices[pos:pos + remaining], prev_val, buf_pos, use_delta)
        if len(comp_all) <= bank_size:
            banks.append(comp_all)
            prev_val = indices[pos + remaining - 1]
            buf_pos = bp_all
            pos += remaining
            if progress_fn:
                progress_fn(pos, total, len(banks))
            break

        # Binary search for the maximum chunk that fits in bank_size.
        # Invariant: lo always fits, hi never fits.
        lo = 1024
        hi = min(chunk_guess * 2, remaining)

        # Ensure lo fits
        comp_lo, bp_lo = compress_bank(
            indices[pos:pos + lo], prev_val, buf_pos, use_delta)
        while len(comp_lo) > bank_size and lo > 64:
            lo = lo // 2
            comp_lo, bp_lo = compress_bank(
                indices[pos:pos + lo], prev_val, buf_pos, use_delta)
        best_comp, best_bp, best_len = comp_lo, bp_lo, lo

        # Ensure hi doesn't fit (find upper bound)
        comp_hi, bp_hi = compress_bank(
            indices[pos:pos + hi], prev_val, buf_pos, use_delta)
        if len(comp_hi) <= bank_size:
            # hi fits — keep expanding
            best_comp, best_bp, best_len = comp_hi, bp_hi, hi
            while hi < remaining:
                hi2 = min(hi + bank_size, remaining)
                if hi2 == hi:
                    break
                comp2, bp2 = compress_bank(
                    indices[pos:pos + hi2], prev_val, buf_pos, use_delta)
                if len(comp2) > bank_size:
                    hi = hi2  # found a value that doesn't fit
                    break
                best_comp, best_bp, best_len = comp2, bp2, hi2
                hi = hi2
            else:
                # Reached end of data without overflowing — handled above
                pass

        # Binary search between best_len (fits) and hi (doesn't fit)
        if hi > best_len + 256:
            search_lo = best_len
            search_hi = hi
            while search_hi - search_lo > 64:
                mid = (search_lo + search_hi) // 2
                comp, bp = compress_bank(
                    indices[pos:pos + mid], prev_val, buf_pos, use_delta)
                if len(comp) <= bank_size:
                    search_lo = mid
                    best_comp, best_bp, best_len = comp, bp, mid
                else:
                    search_hi = mid

            # Fine-tune near the boundary
            for try_len in range(best_len, min(best_len + 512, remaining + 1), 16):
                comp, bp = compress_bank(
                    indices[pos:pos + try_len], prev_val, buf_pos, use_delta)
                if len(comp) <= bank_size:
                    best_comp, best_bp, best_len = comp, bp, try_len
                else:
                    break

        banks.append(best_comp)
        prev_val = indices[pos + best_len - 1]
        buf_pos = best_bp
        pos += best_len

        # Update guess for next bank
        if best_len > 0 and len(best_comp) > 0:
            chunk_guess = int(best_len * bank_size / len(best_comp))

        if progress_fn:
            progress_fn(pos, total, len(banks))

    return banks, pos


def decompress_bank(data: bytes, use_delta: bool = True) -> bytes:
    """Decompress one bank (for verification).

    Returns:
        Reconstructed index values
    """
    if len(data) < HEADER_SIZE + 1:
        raise CompressionError(f"Bank data too short: {len(data)} bytes")

    compressed = data[HEADER_SIZE:]
    raw = _lz_decompress(compressed)

    if use_delta:
        initial_value = data[0]
        out = bytearray(len(raw))
        acc = initial_value
        for i in range(len(raw)):
            acc = (acc + raw[i]) & 0xFF
            out[i] = acc
        return bytes(out)
    else:
        # Raw LZ — data is already the final values
        return bytes(raw)


# ═══════════════════════════════════════════════════════════════════════
# Buffer-aware LZ Compressor
# ═══════════════════════════════════════════════════════════════════════

def _lz_compress(data: bytes, initial_buf_pos: int = 0) -> tuple:
    """LZ compress with decode-buffer awareness.

    buf_pos is always DERIVED: buf_pos = (initial_buf_pos + pos) % buf_size.
    No manual tracking, no drift.

    The 6502 decompressor checks lz_dst wrap per byte, so literal runs
    and match copies CAN cross the destination boundary safely.
    Only the match SOURCE must not wrap (lz_match has no wrap check).

    Returns:
        (compressed_bytes, new_buf_pos)
    """
    n = len(data)
    buf_size = DECODE_BUF_SIZE
    if n == 0:
        return bytes([0x00]), initial_buf_pos % buf_size

    def bp_at(p):
        return (initial_buf_pos + p) % buf_size

    heads = [[] for _ in range(HASH_SIZE)]
    output = bytearray()
    literal_buf = bytearray()
    pos = 0

    while pos < n:
        bp = bp_at(pos)

        # How far back can matches reach?
        # After a wrap, only data written since the wrap is valid.
        # match_window = bp (bytes since the last wrap boundary).
        # If bp==0 and pos>0, a wrap just happened: window is 0.
        match_window = bp

        best_len = 0
        best_off = 0

        if pos + MIN_MATCH <= n:
            hv = _hash3(data, pos, n)
            chain = heads[hv]
            max_len = min(MAX_MATCH, n - pos)

            for cand in reversed(chain[-CHAIN_LEN:]):
                offset = pos - cand
                if offset < 1:
                    continue
                if offset > MAX_LONG_OFF:
                    continue
                # Can't reach back past the last buffer wrap
                if offset > match_window:
                    continue
                if data[cand] != data[pos]:
                    continue
                # Match source: buffer positions [bp-offset, bp-offset+len).
                # Must stay within [0, buf_size) — no source wrapping.
                # bp - offset >= 0 is guaranteed by offset <= match_window = bp.
                # Upper bound: bp - offset + length <= buf_size
                #   → length <= buf_size - bp + offset
                max_src_len = buf_size - bp + offset
                lim = min(max_len, max_src_len)
                length = 0
                while length < lim and data[cand + length] == data[pos + length]:
                    length += 1
                if length > best_len or (length == best_len and offset < best_off):
                    best_len = length
                    best_off = offset
                    if length == max_len:
                        break

            heads[hv].append(pos)
            if len(heads[hv]) > CHAIN_LEN:
                heads[hv] = heads[hv][-CHAIN_LEN:]

        match_cost = 2 if (best_off <= MAX_SHORT_OFF) else 3
        if best_len >= MIN_MATCH and best_len > match_cost:
            # Flush pending literals before emitting match token
            if literal_buf:
                _flush_literals(output, literal_buf)
                literal_buf = bytearray()

            enc_len = best_len - 3
            if best_off <= MAX_SHORT_OFF:
                output.append(0x80 | (enc_len & 0x3F))
                output.append(best_off & 0xFF)
            else:
                output.append(0xC0 | (enc_len & 0x3F))
                output.append(best_off & 0xFF)
                output.append((best_off >> 8) & 0xFF)

            for k in range(1, best_len):
                p = pos + k
                if p + 2 < n:
                    hv2 = _hash3(data, p, n)
                    heads[hv2].append(p)
                    if len(heads[hv2]) > CHAIN_LEN:
                        heads[hv2] = heads[hv2][-CHAIN_LEN:]
            pos += best_len
        else:
            # Accumulate literal
            literal_buf.append(data[pos])
            pos += 1
            if len(literal_buf) >= MAX_LITERAL:
                _flush_literals(output, literal_buf)
                literal_buf = bytearray()

    # Flush remaining literals
    if literal_buf:
        _flush_literals(output, literal_buf)

    output.append(0x00)
    return bytes(output), bp_at(pos)


def _flush_literals(output: bytearray, buf: bytearray):
    """Flush literal buffer as one or more literal tokens (max 127 each)."""
    p = 0
    while p < len(buf):
        chunk = min(len(buf) - p, MAX_LITERAL)
        output.append(chunk)
        output.extend(buf[p:p + chunk])
        p += chunk


def _lz_decompress(data: bytes) -> bytes:
    output = bytearray()
    pos = 0
    while pos < len(data):
        token = data[pos]; pos += 1
        if token == 0x00:
            break
        elif token <= 0x7F:
            count = token
            if pos + count > len(data):
                raise CompressionError(f"Literal overflows at {pos}")
            output.extend(data[pos:pos + count])
            pos += count
        elif token <= 0xBF:
            length = (token & 0x3F) + 3
            if pos >= len(data):
                raise CompressionError(f"Missing short offset at {pos}")
            offset = data[pos]; pos += 1
            _copy_match(output, offset, length)
        else:
            length = (token & 0x3F) + 3
            if pos + 1 >= len(data):
                raise CompressionError(f"Missing long offset at {pos}")
            offset = data[pos] | (data[pos + 1] << 8); pos += 2
            _copy_match(output, offset, length)
    return bytes(output)


def _copy_match(output: bytearray, offset: int, length: int):
    if offset == 0 or offset > len(output):
        raise CompressionError(f"Invalid offset {offset} (output size {len(output)})")
    src = len(output) - offset
    for i in range(length):
        output.append(output[src + i])


def _hash3(data: bytes, pos: int, n: int) -> int:
    if pos + 2 >= n:
        return 0
    return ((data[pos] * 2654435761 + data[pos + 1]) * 31 + data[pos + 2]) % HASH_SIZE


def estimate_ratio(indices: bytes) -> float:
    sample = indices[:min(4096, len(indices))]
    comp, _ = compress_bank(sample, 0, 0)
    return len(comp) / len(sample)
