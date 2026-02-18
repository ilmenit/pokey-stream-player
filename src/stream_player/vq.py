"""VQ (Vector Quantization) compression with per-bank codebook.

Encodes POKEY sample indices as fixed-length vectors (2, 4, 8, or 16 samples).
Each 16 KB bank stores its own 256-entry codebook + index stream.

Bank format:
  [codebook: 256 * vec_size bytes]  — codebook vectors (POKEY indices 0-N)
  [indices:  remaining bytes]       — 1 byte per vector (codebook index)

Each index byte selects a codebook vector -> vec_size output samples.

  vec_size=2:   codebook= 512B   indices/bank=15872  samples/bank= 31,744
  vec_size=4:   codebook=1024B   indices/bank=15360  samples/bank= 61,440
  vec_size=8:   codebook=2048B   indices/bank=14336  samples/bank=114,688
  vec_size=16:  codebook=4096B   indices/bank=12288  samples/bank=196,608

The player reads codebook entries directly from banked memory via
VQ_LO/VQ_HI address lookup tables (no codebook copy needed).

IMPORTANT: Input indices should be encoded WITHOUT noise shaping.
Noise shaping spreads quantization error into patterns that k-means
cannot efficiently represent, degrading VQ SNR by ~3 dB.  Plain
rounding produces vectors like [15,15,15,15] that get exact codebook
matches.  Noise-shaped [14,16,15,14] gets poorly approximated.

Noise gate (``gate=1..100``):  Codebook index 0 is reserved for silence
and vectors where every sample falls below ``max_level * gate / 100`` are
excluded from training — they snap to true zero.  Higher values gate more
aggressively.  Default is 5 (very mild, cleans up near-zero noise).

No gate (``gate=0``):  All 256 entries are trained by k-means on the
actual data.  A silence entry is still ensured (replacing the least-used
code) so zero-padded bank tails decode cleanly.
"""

import numpy as np
from .errors import CompressionError


N_CODES = 256
BANK_SIZE = 16384


def vq_bank_geometry(vec_size):
    """Return (codebook_bytes, indices_per_bank, samples_per_bank)."""
    cb_bytes = N_CODES * vec_size
    idx_per_bank = BANK_SIZE - cb_bytes
    samp_per_bank = idx_per_bank * vec_size
    return cb_bytes, idx_per_bank, samp_per_bank


def _kmeans(vectors, n_codes=256, n_iter=20, max_level=30):
    """Train codebook via k-means on integer vectors.

    Returns:
        codebook: (n_codes, vec_size) uint8 array
        assignments: (N,) uint8 array of codebook indices
    """
    n_vecs, vec_size = vectors.shape
    vf = vectors.astype(np.float32)

    if n_vecs <= n_codes:
        codebook = np.zeros((n_codes, vec_size), dtype=np.float32)
        codebook[:n_vecs] = vf
        for i in range(n_vecs, n_codes):
            codebook[i] = vf[np.random.randint(n_vecs)]
        assignments = np.arange(n_vecs, dtype=np.uint8)
        codebook = np.clip(np.round(codebook), 0, max_level).astype(np.uint8)
        return codebook, assignments

    # k-means++ initialization
    rng = np.random.RandomState(42)
    indices = [rng.randint(n_vecs)]
    for _ in range(1, min(n_codes, n_vecs)):
        cb_so_far = vf[indices]
        dists = np.min(
            np.sum((vf[:, None, :] - cb_so_far[None, :, :]) ** 2, axis=2),
            axis=1)
        total = dists.sum()
        if total < 1e-30:
            probs = np.ones(n_vecs) / n_vecs
        else:
            probs = dists / total
            probs = probs / probs.sum()  # ensure exact sum to 1
        indices.append(rng.choice(n_vecs, p=probs))

    codebook = vf[indices].copy()
    chunk_size = min(50000, n_vecs)

    def _assign(cb):
        """Chunked nearest-centroid assignment."""
        out = np.empty(n_vecs, dtype=np.int32)
        for s in range(0, n_vecs, chunk_size):
            e = min(s + chunk_size, n_vecs)
            d = np.sum((vf[s:e, None, :] - cb[None, :, :]) ** 2, axis=2)
            out[s:e] = np.argmin(d, axis=1)
        return out

    for iteration in range(n_iter):
        assignments = _assign(codebook)

        # Vectorized centroid update via bincount
        new_cb = codebook.copy()
        counts = np.bincount(assignments, minlength=n_codes).astype(np.float32)
        mask = counts > 0
        for dim in range(vec_size):
            sums = np.bincount(assignments, weights=vf[:, dim],
                               minlength=n_codes)
            new_cb[mask, dim] = sums[mask] / counts[mask]

        if np.allclose(new_cb, codebook, atol=0.01):
            codebook = new_cb
            break
        codebook = new_cb

    # Quantize codebook to integers
    codebook = np.clip(np.round(codebook), 0, max_level).astype(np.uint8)

    # Final assignment with integer codebook
    assignments = _assign(codebook.astype(np.float32)).astype(np.uint8)

    return codebook, assignments


def _chunked_assign(vectors_f, codebook_f, chunk_size=50000):
    """Assign vectors to nearest codebook entry in chunks."""
    n_vecs = len(vectors_f)
    out = np.empty(n_vecs, dtype=np.uint8)
    for s in range(0, n_vecs, chunk_size):
        e = min(s + chunk_size, n_vecs)
        d = np.sum((vectors_f[s:e, None, :] - codebook_f[None, :, :]) ** 2,
                   axis=2)
        out[s:e] = np.argmin(d, axis=1).astype(np.uint8)
    return out


def _gate_threshold(max_level, gate_pct):
    """Per-sample threshold for noise gate.

    Args:
        max_level: Maximum POKEY index (depends on channel count).
        gate_pct:  Gate strength 1–100 (percentage of max_level).

    A vector is "near-silent" if ALL its samples are <= this threshold.
    Returns integer threshold (minimum 1 when gate is active).
    """
    return max(1, max_level * gate_pct // 100)


def vq_encode_bank(indices, vec_size, max_level=30, n_iter=20, gate=5):
    """Encode a chunk of POKEY indices into one VQ bank.

    ``gate`` controls noise gate strength (0–100, percentage of dynamic
    range).  When ``gate > 0``, codebook index 0 is reserved for
    ``[0,0,...,0]`` (silence) and vectors where every sample is below the
    threshold are excluded from k-means training — they snap to true zero.
    Higher values gate more aggressively.

    When ``gate == 0``, all 256 codebook entries are trained by k-means
    on the actual data.  A silence entry is still ensured (replacing the
    least-used code) so zero-padded bank tails decode cleanly.

    Returns:
        (bank_data, n_samples_encoded)
    """
    indices = np.asarray(indices, dtype=np.uint8)
    n_vecs = len(indices) // vec_size
    if n_vecs == 0:
        raise CompressionError("Not enough samples for one vector")

    used = n_vecs * vec_size
    vectors = indices[:used].reshape(n_vecs, vec_size)

    if gate > 0:
        # ── Gated mode: reserve index 0 for silence, train 255 codes ──
        thresh = _gate_threshold(max_level, gate)
        near_silent_mask = np.all(vectors <= thresh, axis=1)
        non_silent = vectors[~near_silent_mask]

        if len(non_silent) == 0:
            codebook = np.zeros((N_CODES, vec_size), dtype=np.uint8)
            assignments = np.zeros(n_vecs, dtype=np.uint8)
        else:
            cb_rest, _ = _kmeans(non_silent, n_codes=N_CODES - 1,
                                 n_iter=n_iter, max_level=max_level)
            codebook = np.zeros((N_CODES, vec_size), dtype=np.uint8)
            codebook[1:N_CODES] = cb_rest[:N_CODES - 1]
            assignments = _chunked_assign(
                vectors.astype(np.float32), codebook.astype(np.float32))
    else:
        # ── Natural mode: k-means gets all 256 entries ────────────────
        codebook, assignments = _kmeans(vectors, n_codes=N_CODES,
                                        n_iter=n_iter, max_level=max_level)

        # Ensure index 0 = silence so zero-padded bank tails are clean.
        # Replace the least-used codebook entry.
        silence = np.zeros(vec_size, dtype=np.uint8)
        if not np.any(np.all(codebook == silence, axis=1)):
            counts = np.bincount(assignments, minlength=N_CODES)
            victim = int(np.argmin(counts))
            codebook[victim] = silence
            if counts[victim] > 0:
                assignments = _chunked_assign(
                    vectors.astype(np.float32),
                    codebook.astype(np.float32))

    cb_bytes = N_CODES * vec_size
    bank = bytearray(cb_bytes + n_vecs)

    # Write codebook (256 contiguous vectors)
    for i in range(N_CODES):
        offset = i * vec_size
        bank[offset:offset + vec_size] = codebook[i].tobytes()

    # Write index stream
    bank[cb_bytes:cb_bytes + n_vecs] = assignments.tobytes()

    return bytes(bank), used


def vq_encode_banks(indices, vec_size=8, max_banks=64,
                    max_level=30, n_iter=20, gate=5, progress_fn=None):
    """Encode all POKEY indices into VQ banks with per-bank codebooks.

    Args:
        indices: POKEY level indices (bytes or numpy array)
        gate: Noise gate strength 0–100 (0 = off, default 5).

    Returns:
        (banks, samples_encoded)
    """
    if vec_size not in (2, 4, 8, 16):
        raise CompressionError(f"vec_size must be 2, 4, 8, or 16, got {vec_size}")

    # Accept bytes or numpy array
    if isinstance(indices, (bytes, bytearray)):
        indices = np.frombuffer(indices, dtype=np.uint8)
    indices = np.asarray(indices, dtype=np.uint8)

    cb_bytes, idx_per_bank, samp_per_bank = vq_bank_geometry(vec_size)
    total = len(indices)
    banks = []
    pos = 0

    while pos < total and len(banks) < max_banks:
        remaining = total - pos
        chunk_size = min(samp_per_bank, remaining)
        chunk_size = (chunk_size // vec_size) * vec_size
        if chunk_size == 0:
            break

        chunk = indices[pos:pos + chunk_size]
        bank_data, n_encoded = vq_encode_bank(
            chunk, vec_size, max_level, n_iter, gate=gate)

        # Pad bank to BANK_SIZE
        if len(bank_data) < BANK_SIZE:
            bank_data = bank_data + bytes(BANK_SIZE - len(bank_data))

        banks.append(bank_data)
        pos += n_encoded

        if progress_fn:
            progress_fn(pos, total, len(banks))

    return banks, pos


def vq_decode_bank(bank_data, vec_size, n_vectors=None):
    """Decode one VQ bank to POKEY indices."""
    cb_bytes = N_CODES * vec_size
    if len(bank_data) < cb_bytes:
        raise CompressionError(f"Bank too short: {len(bank_data)}")

    codebook = np.frombuffer(
        bank_data[:cb_bytes], dtype=np.uint8).reshape(N_CODES, vec_size)

    idx_data = bank_data[cb_bytes:]
    if n_vectors is not None:
        idx_data = idx_data[:n_vectors]
    else:
        # Strip trailing padding zeros by finding actual index count
        max_idx = BANK_SIZE - cb_bytes
        idx_data = idx_data[:max_idx]

    idx_stream = np.frombuffer(idx_data, dtype=np.uint8)
    return codebook[idx_stream].reshape(-1)


def vq_decode_banks(banks, vec_size, total_samples=None):
    """Decode all VQ banks to POKEY indices."""
    cb_bytes, idx_per_bank, samp_per_bank = vq_bank_geometry(vec_size)
    result = []
    remaining = total_samples

    for bank_data in banks:
        if remaining is not None:
            n_vecs = min(idx_per_bank,
                         (remaining + vec_size - 1) // vec_size)
        else:
            n_vecs = idx_per_bank

        decoded = vq_decode_bank(bank_data, vec_size, n_vecs)
        result.append(decoded)

        if remaining is not None:
            remaining -= len(decoded)
            if remaining <= 0:
                break

    output = np.concatenate(result)
    if total_samples is not None:
        output = output[:total_samples]
    return output


def vq_measure_snr(original, decoded, voltage_table):
    """Measure SNR between original and decoded POKEY indices."""
    n = min(len(original), len(decoded))
    orig_v = voltage_table[original[:n]].astype(np.float64)
    dec_v = voltage_table[decoded[:n]].astype(np.float64)
    sig = np.mean(orig_v ** 2)
    noise = np.mean((orig_v - dec_v) ** 2)
    if noise < 1e-30:
        return 999.0
    return 10.0 * np.log10(sig / noise)
