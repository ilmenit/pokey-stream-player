"""POKEY hardware voltage tables for audio quantization.

Tables derived from measured AMI C012294 POKEY chip output.
Supports 1-4 channel configurations with single-step allocation:
each consecutive level changes exactly ONE AUDC register, guaranteeing
zero intermediate voltage states during sequential register writes.

Channel configurations:
  1 ch: 16 levels, 0.546V range, ~80cy IRQ
  2 ch: 31 levels, 1.091V range, ~88cy IRQ
  3 ch: 46 levels, 1.637V range, ~96cy IRQ
  4 ch: 61 levels, 2.183V range, ~104cy IRQ

All configurations have a 1.70x step ratio — the physical minimum
for POKEY's nonlinear voltage ladder. Noise shaping pushes this
nonuniformity above the audible range.
"""

import numpy as np

# Single channel: 16 levels (AUDC volume 0-15)
VOLTAGE_TABLE_SINGLE = np.array([
    0.000000, 0.032677, 0.068621, 0.101298, 0.143778, 0.176455,
    0.212399, 0.245076, 0.300626, 0.333303, 0.369247, 0.401924,
    0.444404, 0.477081, 0.513025, 0.545702,
], dtype=np.float32)


def build_nch_table(n_channels):
    """Build single-step voltage table for N POKEY channels."""
    if n_channels < 1 or n_channels > 4:
        raise ValueError(f"n_channels must be 1-4, got {n_channels}")

    V = VOLTAGE_TABLE_SINGLE
    max_steps = 15 * n_channels
    n_levels = max_steps + 1
    max_voltage = float(n_channels * V[15])

    channels = [0] * n_channels
    alloc = [tuple(channels)]
    voltages_list = [0.0]

    for k in range(1, n_levels):
        target_v = k * max_voltage / max_steps
        best_ch = None
        best_dist = float('inf')
        for ch in range(n_channels):
            if channels[ch] < 15:
                trial = list(channels)
                trial[ch] += 1
                trial_v = sum(float(V[trial[j]]) for j in range(n_channels))
                d = abs(trial_v - target_v)
                if d < best_dist:
                    best_dist = d
                    best_ch = ch
        channels[best_ch] += 1
        v = sum(float(V[channels[j]]) for j in range(n_channels))
        voltages_list.append(v)
        alloc.append(tuple(channels))

    return np.array(voltages_list, dtype=np.float32), alloc


def max_level(n_channels):
    """Maximum level index for N channels (0-based)."""
    return 15 * n_channels


def n_levels(n_channels):
    """Number of quantization levels for N channels."""
    return max_level(n_channels) + 1


# Pre-built tables
VOLTAGE_TABLE_QUAD, _QUAD_ALLOC = build_nch_table(4)
QUAD_MAX_LEVEL = max_level(4)  # 60

# Legacy dual-channel table (balanced split, used by old encode_mono_dual)
VOLTAGE_TABLE_DUAL = np.array([
    0.000000, 0.032677, 0.065354, 0.101298, 0.137242, 0.169919,
    0.202596, 0.245076, 0.287556, 0.320232, 0.352909, 0.388853,
    0.424798, 0.457475, 0.490151, 0.545702, 0.573477, 0.589816,
    0.606154, 0.624126, 0.642098, 0.658437, 0.674775, 0.696015,
    0.717255, 0.733593, 0.749932, 0.767904, 0.785876, 0.802215,
    0.818553,
], dtype=np.float32)

# Cache for dynamically-built tables
_TABLE_CACHE = {4: (VOLTAGE_TABLE_QUAD, _QUAD_ALLOC)}


def get_table(n_channels):
    """Get (voltages, allocations) for N channels, with caching."""
    if n_channels not in _TABLE_CACHE:
        _TABLE_CACHE[n_channels] = build_nch_table(n_channels)
    return _TABLE_CACHE[n_channels]


def index_to_volumes(idx, n_channels=4):
    """Convert level index to per-channel volume tuple."""
    _, alloc = get_table(n_channels)
    max_idx = len(alloc) - 1
    idx = max(0, min(idx, max_idx))
    return alloc[idx]


# Backward-compatible aliases
def quad_index_to_volumes(idx):
    return index_to_volumes(idx, 4)

def quantize_nch(audio, n_channels, noise_shaping=True):
    """Quantize float audio to N-channel POKEY indices."""
    table, _ = get_table(n_channels)
    return _quantize(audio, table, noise_shaping)

def quantize_single(audio, noise_shaping=True):
    return _quantize(audio, VOLTAGE_TABLE_SINGLE, noise_shaping)

def quantize_dual(audio, noise_shaping=True):
    return _quantize(audio, VOLTAGE_TABLE_DUAL, noise_shaping)

def dual_index_to_pair(idx):
    v1 = idx // 2
    v2 = idx - v1
    return (v1, v2)

def pack_dual_byte(idx):
    v1, v2 = dual_index_to_pair(idx)
    return (v1 << 4) | v2

def quantize_quad(audio, noise_shaping=True):
    return _quantize(audio, VOLTAGE_TABLE_QUAD, noise_shaping)


def quantize_1cps(audio, noise_shaping=True):
    """Quantize using 1-Channel-Per-Sample encoding."""
    V = VOLTAGE_TABLE_SINGLE
    max_voltage = float(4 * V[15])
    scaled = ((audio + 1.0) / 2.0) * max_voltage

    out = np.zeros(len(scaled), dtype=np.uint8)
    state = [0, 0, 0, 0]
    error = 0.0
    vf = [float(V[i]) for i in range(16)]

    for i in range(len(scaled)):
        target = scaled[i] + error if noise_shaping else scaled[i]
        target = max(0.0, min(target, max_voltage))
        best_ch = 0; best_val = 0; best_err = float('inf')
        base_v = sum(vf[state[j]] for j in range(4))
        for ch in range(4):
            old_v = vf[state[ch]]
            for val in range(16):
                trial_v = base_v - old_v + vf[val]
                err = abs(trial_v - target)
                if err < best_err:
                    best_err = err; best_ch = ch; best_val = val
                    best_total_v = trial_v
        state[best_ch] = best_val
        out[i] = (best_ch << 4) | best_val
        if noise_shaping:
            error = scaled[i] + error - best_total_v
    return out

def unpack_1cps(packed):
    return (packed >> 4) & 0x03, packed & 0x0F


def _quantize(audio, table, noise_shaping):
    """Core quantization with optional noise shaping."""
    table_max = table[-1]
    n_lvls = len(table)
    last_idx = n_lvls - 1
    scaled = ((audio + 1.0) / 2.0) * table_max

    if not noise_shaping:
        indices = np.searchsorted(table, scaled)
        indices = np.clip(indices, 0, last_idx)
        left = np.clip(indices - 1, 0, last_idx)
        err_right = np.abs(scaled - table[indices])
        err_left = np.abs(scaled - table[left])
        return np.where(err_left < err_right, left, indices).astype(np.uint8)

    out = np.zeros(len(scaled), dtype=np.uint8)
    error = 0.0
    for i in range(len(scaled)):
        val = scaled[i] + error
        val = max(0.0, min(val, table_max))
        idx = int(np.searchsorted(table, val))
        if idx > last_idx:
            idx = last_idx
        elif idx > 0 and abs(val - table[idx - 1]) < abs(val - table[idx]):
            idx -= 1
        out[i] = idx
        error = scaled[i] + error - table[idx]
    return out
