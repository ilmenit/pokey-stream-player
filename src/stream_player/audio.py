"""Audio loading, resampling, and encoding to POKEY nibble format."""

import os
import struct
import wave
import numpy as np
import scipy.signal

from .errors import AudioLoadError
from .tables import quantize_dual, quantize_nch, quantize_1cps

# PAL POKEY base clock
PAL_CLOCK = 1773447
CLK_64K = PAL_CLOCK // 28   # ~63337 Hz (default timer clock, AUDCTL=$00)
CLK_179 = PAL_CLOCK          # 1.77MHz (AUDCTL bit 6 set)


def dc_block(audio: np.ndarray, cutoff_hz: float = 20.0,
             sample_rate: int = 8000) -> np.ndarray:
    """Remove DC offset and subsonic content with a high-pass filter.

    Uses 2nd-order Butterworth HPF. This is critical for low-bitdepth
    quantization: any DC offset wastes dynamic range and creates a
    constant noise floor.

    Args:
        audio: float32 audio array (mono or multichannel)
        cutoff_hz: HPF cutoff frequency (default 20 Hz)
        sample_rate: sample rate of the audio

    Returns:
        Filtered audio (same shape)
    """
    nyquist = sample_rate / 2.0
    if cutoff_hz >= nyquist:
        return audio
    sos = scipy.signal.butter(2, cutoff_hz / nyquist, btype='high', output='sos')
    if audio.ndim == 1:
        return scipy.signal.sosfiltfilt(sos, audio).astype(np.float32)
    # Per-channel filtering
    out = np.empty_like(audio)
    for ch in range(audio.shape[1]):
        out[:, ch] = scipy.signal.sosfiltfilt(sos, audio[:, ch])
    return out.astype(np.float32)


def load_audio(path: str) -> tuple:
    """Load audio file and return (samples_float32, sample_rate, n_channels).

    Loading priority:
      1. soundfile (handles WAV, MP3, FLAC, OGG, AIFF — no external binaries)
      2. Native WAV loader (stdlib, always available)
      3. ffmpeg fallback (for tracker formats: MOD, XM, S3M, IT, SID, etc.)

    Returns:
        (audio_data, sample_rate, n_channels)
        Stereo: audio_data shape (N, 2). Mono: (N,).
    """
    if not os.path.exists(path):
        raise AudioLoadError(f"File not found: {path}")

    # Try soundfile first (handles MP3, FLAC, OGG, WAV, AIFF, etc.)
    try:
        return _load_via_soundfile(path)
    except _SoundfileUnavailable:
        pass
    except AudioLoadError:
        # soundfile is installed but can't read this format — try fallbacks
        pass

    ext = os.path.splitext(path)[1].lower()

    # Try native WAV loader (stdlib, no dependencies)
    if ext == '.wav':
        try:
            return _load_wav(path)
        except AudioLoadError:
            pass

    # Last resort: ffmpeg (for tracker formats, exotic codecs, etc.)
    return _load_via_ffmpeg(path)


class _SoundfileUnavailable(Exception):
    """Raised when soundfile is not installed."""
    pass


def _load_via_soundfile(path: str) -> tuple:
    """Load audio via the soundfile library (wraps libsndfile).

    soundfile bundles libsndfile for Windows/macOS/Linux — no external
    binaries needed. Since libsndfile 1.1.0, supports:
      WAV (all subtypes), FLAC, OGG/Vorbis, MP3, AIFF, and more.
    """
    try:
        import soundfile as sf
    except ImportError:
        raise _SoundfileUnavailable()

    try:
        data, sample_rate = sf.read(path, dtype='float32', always_2d=True)
    except Exception as e:
        raise AudioLoadError(f"soundfile cannot read '{path}': {e}")

    n_channels = data.shape[1]
    if n_channels == 1:
        data = data[:, 0]  # squeeze to 1D for mono

    if len(data) == 0:
        raise AudioLoadError(f"No audio samples in: {path}")

    return data, sample_rate, n_channels


def _load_via_ffmpeg(path: str) -> tuple:
    """Load audio via ffmpeg (fallback for tracker formats, exotic codecs)."""
    import subprocess
    import shutil

    ffmpeg = shutil.which('ffmpeg')
    if ffmpeg is None:
        ext = os.path.splitext(path)[1].lower()
        raise AudioLoadError(
            f"Cannot load '{ext}' files.\n"
            f"Install the soundfile package (handles MP3/FLAC/OGG/WAV):\n"
            f"  pip install soundfile\n"
            f"Or install ffmpeg for tracker formats (MOD/XM/S3M/IT):\n"
            f"  https://ffmpeg.org")
    
    # Probe channel count and sample rate first
    try:
        probe = subprocess.run(
            [ffmpeg, '-i', path, '-hide_banner'],
            capture_output=True, text=True, timeout=10)
        # Parse stderr for stream info (ffmpeg prints info to stderr)
        info = probe.stderr
    except (subprocess.TimeoutExpired, OSError) as e:
        raise AudioLoadError(f"ffmpeg probe failed: {e}")
    
    # Extract channel count and sample rate from ffmpeg output
    import re
    # Look for pattern like "44100 Hz, stereo" or "48000 Hz, mono"
    m = re.search(r'(\d+)\s*Hz.*?(mono|stereo|(\d+)\s*channels)', info)
    if m:
        src_rate = int(m.group(1))
        ch_str = m.group(2)
        if ch_str == 'mono':
            n_channels = 1
        elif ch_str == 'stereo':
            n_channels = 2
        elif m.group(3):
            n_channels = int(m.group(3))
        else:
            n_channels = 1
    else:
        src_rate = 44100
        n_channels = 1
    
    # Decode to raw 16-bit PCM via pipe
    try:
        result = subprocess.run(
            [ffmpeg, '-i', path,
             '-f', 's16le',        # raw 16-bit little-endian PCM
             '-acodec', 'pcm_s16le',
             '-ar', str(src_rate),  # keep original rate
             '-ac', str(n_channels),
             '-v', 'error',
             'pipe:1'],
            capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise AudioLoadError(f"ffmpeg decoding timed out (file too large?)")
    except OSError as e:
        raise AudioLoadError(f"ffmpeg failed: {e}")
    
    if result.returncode != 0:
        err = result.stderr.decode('utf-8', errors='replace').strip()
        raise AudioLoadError(f"ffmpeg decode error: {err}")
    
    raw_data = result.stdout
    if len(raw_data) < 4:
        raise AudioLoadError(f"ffmpeg produced no audio data from: {path}")
    
    samples = np.frombuffer(raw_data, dtype=np.int16).astype(np.float32) / 32768.0
    
    if n_channels > 1:
        # Ensure we have complete frames
        samples = samples[:len(samples) - (len(samples) % n_channels)]
        samples = samples.reshape(-1, n_channels)
    
    if len(samples) == 0:
        raise AudioLoadError(f"No audio samples decoded from: {path}")
    
    return samples, src_rate, n_channels


def _load_wav(path: str) -> tuple:
    """Load a WAV file using the standard library."""
    try:
        with wave.open(path, 'rb') as wf:
            n_channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            sample_rate = wf.getframerate()
            n_frames = wf.getnframes()
            
            if n_frames == 0:
                raise AudioLoadError(f"WAV file is empty: {path}")
            
            raw_data = wf.readframes(n_frames)
    except wave.Error as e:
        raise AudioLoadError(f"Invalid WAV file: {e}")
    
    # Convert raw bytes to float32
    if sample_width == 1:
        # 8-bit unsigned
        samples = np.frombuffer(raw_data, dtype=np.uint8).astype(np.float32)
        samples = (samples - 128.0) / 128.0
    elif sample_width == 2:
        # 16-bit signed
        samples = np.frombuffer(raw_data, dtype=np.int16).astype(np.float32)
        samples = samples / 32768.0
    elif sample_width == 3:
        # 24-bit signed (read as bytes, convert manually)
        n_samples = len(raw_data) // 3
        samples = np.zeros(n_samples, dtype=np.float32)
        for i in range(n_samples):
            b = raw_data[i*3:(i+1)*3]
            val = struct.unpack('<i', b + (b'\xff' if b[2] & 0x80 else b'\x00'))[0]
            samples[i] = val / 8388608.0
    elif sample_width == 4:
        # 32-bit: could be int32 or float32
        # Try int32 first (more common for WAV), then check for float32
        raw_float = np.frombuffer(raw_data, dtype=np.float32).copy()
        # Heuristic: float32 WAV samples should be in [-1, 1] (or at least [-10, 10])
        if len(raw_float) > 0 and np.max(np.abs(raw_float)) <= 10.0 and np.isfinite(raw_float).all():
            samples = raw_float
        else:
            # Treat as int32
            samples = np.frombuffer(raw_data, dtype=np.int32).astype(np.float32)
            samples = samples / 2147483648.0
    else:
        raise AudioLoadError(f"Unsupported sample width: {sample_width} bytes")
    
    # Reshape for multi-channel
    if n_channels > 1:
        samples = samples.reshape(-1, n_channels)
    
    return samples, sample_rate, n_channels


def resample(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Resample audio from src_rate to dst_rate Hz."""
    if src_rate == dst_rate:
        return audio
    
    if audio.ndim == 1:
        n_out = int(len(audio) * dst_rate / src_rate)
        return scipy.signal.resample(audio, n_out).astype(np.float32)
    else:
        # Multi-channel: resample each channel
        n_out = int(audio.shape[0] * dst_rate / src_rate)
        out = np.zeros((n_out, audio.shape[1]), dtype=np.float32)
        for ch in range(audio.shape[1]):
            out[:, ch] = scipy.signal.resample(audio[:, ch], n_out).astype(np.float32)
        return out


def calc_pokey_rate(divisor: int, audctl: int = 0x40) -> float:
    """Calculate actual timer 1 IRQ rate from AUDF1 and AUDCTL.
    
    POKEY timer underflows at: clock / (AUDF + 1).
    The traditional formula clock/(2*(N+1)) gives the audio TONE
    frequency (square wave toggles each underflow).  For IRQ-driven
    sample playback we need the raw underflow rate — no /2.
    
    Args:
        divisor: AUDF1 value (0-255)
        audctl: AUDCTL register value ($40 = 1.77MHz, $00 = 64kHz)
    """
    clock = CLK_179 if (audctl & 0x40) else CLK_64K
    return clock / (divisor + 1)


def find_best_divisor(target_rate: float) -> tuple:
    """Find the POKEY timer divisor and AUDCTL closest to the target rate.
    
    Prefers 1.77MHz clock (AUDCTL=$40) for precision.  Falls back to
    64kHz clock (AUDCTL=$00) if the divisor exceeds 8 bits (rates
    below ~6928 Hz).
    
    Returns:
        (divisor, actual_rate, audctl)
    """
    # Try 1.77MHz clock first (fine granularity, ~6928–1773447 Hz)
    raw_div = CLK_179 / target_rate - 1
    if 0 <= raw_div <= 255:
        div = max(0, min(255, round(raw_div)))
        rate = CLK_179 / (div + 1)
        return div, rate, 0x40
    
    # Fall back to 64kHz clock (coarser, ~248–63337 Hz)
    raw_div = CLK_64K / target_rate - 1
    div = max(0, min(255, round(raw_div)))
    rate = CLK_64K / (div + 1)
    return div, rate, 0x00


def _pack_dual_indices(indices):
    """Vectorized dual-channel packing: index → (v1<<4)|v2 byte.

    Balanced split: v1 = idx // 2, v2 = idx - v1.
    """
    v1 = indices // 2
    v2 = indices - v1
    return (v1.astype(np.uint8) << 4) | v2.astype(np.uint8)


def encode_mono_dual(audio: np.ndarray, noise_shaping: bool = True,
                     sample_rate: int = 8000) -> bytes:
    """Encode mono audio for dual-POKEY playback (31 levels).

    Each sample → 1 byte: (v1 << 4) | v2 where v1+v2 produces
    the target amplitude via POKEY's non-linear mixing.
    """
    if audio.ndim > 1:
        audio = audio.mean(axis=1)

    audio = dc_block(audio, cutoff_hz=20.0, sample_rate=sample_rate)
    audio = normalize(audio)
    audio = np.clip(audio, -1.0, 1.0)
    indices = quantize_dual(audio, noise_shaping)
    return bytes(_pack_dual_indices(indices))


def encode_stereo_dual(audio: np.ndarray, noise_shaping: bool = True,
                       sample_rate: int = 8000) -> bytes:
    """Encode stereo audio for dual-POKEY playback.

    Left channel → POKEY1 (AUDC1 + AUDC2, 31 levels)
    Right channel → POKEY2 (AUDC1 + AUDC2, 31 levels)

    Output: 2 bytes per sample (left_packed, right_packed).
    """
    if audio.ndim == 1:
        audio = np.column_stack([audio, audio])
    elif audio.shape[1] > 2:
        audio = audio[:, :2]

    audio = dc_block(audio, cutoff_hz=20.0, sample_rate=sample_rate)
    audio = normalize(audio)
    audio = np.clip(audio, -1.0, 1.0)
    left_idx = quantize_dual(audio[:, 0], noise_shaping)
    right_idx = quantize_dual(audio[:, 1], noise_shaping)

    out = np.empty(len(left_idx) * 2, dtype=np.uint8)
    out[0::2] = _pack_dual_indices(left_idx)
    out[1::2] = _pack_dual_indices(right_idx)
    return bytes(out)


def normalize(audio: np.ndarray, headroom_db: float = 0.5) -> np.ndarray:
    """Peak-normalize audio to use full dynamic range.

    Critical for low-bitdepth quantization: music typically peaks at
    -3 to -6 dBFS, wasting 30-50% of available levels. Normalization
    ensures the full 61-level range is utilized.

    Args:
        audio: float32 audio (mono or multichannel), after dc_block
        headroom_db: headroom below 0 dBFS (default 0.5 dB, ~5%)

    Returns:
        Normalized audio with peak at -(headroom_db) dBFS
    """
    peak = float(np.max(np.abs(audio)))
    if peak < 1e-6:
        return audio  # silence
    target = 10.0 ** (-headroom_db / 20.0)  # ~0.944 for 0.5 dB
    gain = target / peak
    if gain > 1.0:
        return audio * gain
    return audio  # already loud enough


def _make_quantizer(pokey_channels, noise_shaping, mode='scalar'):
    """Create quantization function based on settings."""
    if mode == '1cps':
        return lambda audio: quantize_1cps(audio, noise_shaping)
    return lambda audio: quantize_nch(audio, pokey_channels, noise_shaping)


def _preprocess(audio, n_channels, stereo, noise_shaping, sample_rate,
                enhance):
    """Shared preprocessing: mix, dc_block, normalize, enhance, clip."""
    if audio.ndim > 1 and (not stereo or n_channels < 2):
        audio = audio.mean(axis=1)

    audio = dc_block(audio, cutoff_hz=20.0, sample_rate=sample_rate)
    audio = normalize(audio)

    if enhance:
        from .enhance import enhance_audio as _enhance
        audio = _enhance(audio, sample_rate)
        audio = normalize(audio)

    return np.clip(audio, -1.0, 1.0)


def _quantize_and_pack(audio, stereo, qfn):
    """Apply quantizer, handle stereo interleaving."""
    if stereo and audio.ndim > 1:
        left_idx = qfn(audio[:, 0])
        right_idx = qfn(audio[:, 1])
        out = np.empty(len(left_idx) * 2, dtype=np.uint8)
        out[0::2] = left_idx
        out[1::2] = right_idx
        return bytes(out)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    return bytes(qfn(audio))


def encode_audio(audio: np.ndarray, n_channels: int, stereo: bool,
                 noise_shaping: bool = True, sample_rate: int = 8000,
                 pokey_channels: int = 4, enhance: bool = False) -> bytes:
    """Encode audio to POKEY byte stream (N-channel for RAW mode).

    Each sample → 1 byte: channel index (0 to 15*pokey_channels).
    """
    audio = _preprocess(audio, n_channels, stereo, noise_shaping,
                        sample_rate, enhance)
    qfn = _make_quantizer(pokey_channels, noise_shaping)
    return _quantize_and_pack(audio, stereo, qfn)


def encode_indices(audio: np.ndarray, n_channels: int, stereo: bool,
                   noise_shaping: bool = True, sample_rate: int = 8000,
                   pokey_channels: int = 4, mode: str = '1cps',
                   enhance: bool = False) -> bytes:
    """Encode audio to POKEY index stream (for DeltaLZ/VQ compression).

    Like encode_audio but supports 1CPS mode selection.
    """
    audio = _preprocess(audio, n_channels, stereo, noise_shaping,
                        sample_rate, enhance)
    qfn = _make_quantizer(pokey_channels, noise_shaping, mode)
    return _quantize_and_pack(audio, stereo, qfn)
