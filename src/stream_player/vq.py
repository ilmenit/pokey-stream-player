"""VQ (Vector Quantization) compression with per-bank codebook.

Encodes POKEY sample indices as fixed-length vectors (4, 8, or 16 samples).
Each 16 KB bank stores its own 256-entry codebook + index stream.

Bank format:
  [codebook: 256 * vec_size bytes]  — codebook vectors (POKEY indices 0-N)
  [indices:  remaining bytes]       — 1 byte per vector (codebook index)

Each index byte selects a codebook vector -> vec_size output samples.

  vec_size=4:   codebook=1024B   indices/bank=15360  samples/bank= 61,440
  vec_size=8:   codebook=2048B   indices/bank=14336  samples/bank=114,688
  vec_size=16:  codebook=4096B   indices/bank=12288  samples/bank=196,608

The player copies each bank's codebook to main RAM at bank start,
then reads samples via VQ_LO/VQ_HI address lookup tables.
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

    for iteration in range(n_iter):
        # Assign vectors to nearest centroid
        assignments = np.empty(n_vecs, dtype=np.int32)
        for s in range(0, n_vecs, chunk_size):
            e = min(s + chunk_size, n_vecs)
            d = np.sum((vf[s:e, None, :] - codebook[None, :, :]) ** 2, axis=2)
            assignments[s:e] = np.argmin(d, axis=1)

        # Update centroids
        new_cb = codebook.copy()
        for c in range(n_codes):
            members = vf[assignments == c]
            if len(members) > 0:
                new_cb[c] = np.mean(members, axis=0)

        if np.allclose(new_cb, codebook, atol=0.01):
            codebook = new_cb
            break
        codebook = new_cb

    # Quantize codebook to integers
    codebook = np.clip(np.round(codebook), 0, max_level).astype(np.uint8)

    # Final assignment with integer codebook
    cb_f = codebook.astype(np.float32)
    assignments = np.empty(n_vecs, dtype=np.uint8)
    for s in range(0, n_vecs, chunk_size):
        e = min(s + chunk_size, n_vecs)
        d = np.sum((vf[s:e, None, :] - cb_f[None, :, :]) ** 2, axis=2)
        assignments[s:e] = np.argmin(d, axis=1).astype(np.uint8)

    return codebook, assignments


def _silence_threshold(max_level, vec_size):
    """Determine per-sample threshold for near-silent vectors.

    A vector is "near-silent" if ALL its samples are <= this threshold.
    Scales with max_level (which depends on pokey_channels) and vec_size:
      - Higher max_level → wider dynamic range → slightly higher threshold
      - Larger vec_size  → more samples must ALL be low → threshold can be
        a bit more generous since random chance of all-low is tiny.

    Returns per-sample threshold (integer).
    """
    # Base threshold: ~7% of max_level, minimum 1
    base = max(1, max_level // 15)
    # Slight boost for larger vectors (harder for noise to be all-low)
    if vec_size >= 16:
        base = max(base, 2)
    return base


def vq_encode_bank(indices, vec_size, max_level=30, n_iter=20):
    """Encode a chunk of POKEY indices into one VQ bank.

    If near-silent vectors exist (all samples <= threshold), codebook
    index 0 is reserved for the silence vector [0,0,...,0].  This
    guarantees:
      - Perfect silence playback in quiet sections
      - Clean padding at end of last bank (padding = 0x00 = silence)

    If no near-silent vectors are present in the chunk, all 256
    codebook entries are used for the actual signal — no waste.

    Returns:
        (bank_data, n_samples_encoded)
    """
    indices = np.asarray(indices, dtype=np.uint8)
    n_vecs = len(indices) // vec_size
    if n_vecs == 0:
        raise CompressionError("Not enough samples for one vector")

    used = n_vecs * vec_size
    vectors = indices[:used].reshape(n_vecs, vec_size)

    # Check for near-silent vectors
    thresh = _silence_threshold(max_level, vec_size)
    near_silent_mask = np.all(vectors <= thresh, axis=1)
    n_near_silent = int(near_silent_mask.sum())

    if n_near_silent > 0:
        # ── Reserve codebook[0] for silence ──
        non_silent = vectors[~near_silent_mask]

        if len(non_silent) == 0:
            # Entire chunk is near-silent: single silence entry suffices
            codebook = np.zeros((N_CODES, vec_size), dtype=np.uint8)
            assignments = np.zeros(n_vecs, dtype=np.uint8)
        else:
            # Train 255 codes on non-silent vectors
            cb_rest, _ = _kmeans(non_silent, n_codes=N_CODES - 1,
                                 n_iter=n_iter, max_level=max_level)

            # Build codebook: index 0 = silence, 1..255 = trained codes
            codebook = np.zeros((N_CODES, vec_size), dtype=np.uint8)
            codebook[1:N_CODES] = cb_rest[:N_CODES - 1]

            # Final assignment: all vectors against full 256-entry codebook
            # Near-silent vectors are assigned to index 0 (silence),
            # others are matched against entries 1-255
            cb_f = codebook.astype(np.float32)
            vf = vectors.astype(np.float32)
            chunk_size = min(50000, n_vecs)
            assignments = np.empty(n_vecs, dtype=np.uint8)
            for s in range(0, n_vecs, chunk_size):
                e = min(s + chunk_size, n_vecs)
                d = np.sum((vf[s:e, None, :] - cb_f[None, :, :]) ** 2,
                           axis=2)
                assignments[s:e] = np.argmin(d, axis=1).astype(np.uint8)
    else:
        # ── No near-silent vectors: use all 256 codes ──
        codebook, assignments = _kmeans(vectors, N_CODES, n_iter, max_level)

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
                    max_level=30, n_iter=20, progress_fn=None):
    """Encode all POKEY indices into VQ banks with per-bank codebooks.

    Args:
        indices: POKEY level indices (bytes or numpy array)

    Returns:
        (banks, samples_encoded)
    """
    if vec_size not in (4, 8, 16):
        raise CompressionError(f"vec_size must be 4, 8, or 16, got {vec_size}")

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
            chunk, vec_size, max_level, n_iter)

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
