"""Stream Player CLI — Convert audio to Atari 8-bit with POKEY playback.

Usage:
    encode song.mp3                        Default: VQ4 → XEX
    encode song.mp3 -a                     ASM project only
    encode song.mp3 -x -a                  Both XEX and ASM project
    encode song.mp3 -c lz                  DeltaLZ compression

Pipeline:
    1. Load & resample audio
    2. Encode to POKEY indices
    3. Compress (VQ / DeltaLZ / raw)
    4. Generate assembly project
    5. Assemble → .xex binary
"""

import argparse
import os
import shutil
import sys
import tempfile
import time

from .errors import StreamPlayerError, AudioLoadError, CompressionError
from .audio import (load_audio, resample, encode_audio, encode_indices,
                    find_best_divisor)
from .compress import compress_banks, decompress_bank
from .layout import split_into_banks, MAX_BANKS
from .tables import max_level
from .asm_project import generate_project, try_assemble


def _fmt_duration(seconds: float) -> str:
    """Format seconds as m:ss or h:mm:ss."""
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


def _compress_progress(comp_size, n_samples, n_banks):
    """Progress callback for DeltaLZ compression."""
    if n_banks > 0:
        print(f"\r  {n_banks} banks, {comp_size:,} bytes, "
              f"{n_samples:,} samples", end='', flush=True)


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog='encode',
        description='Convert audio to Atari XEX with POKEY playback.',
        epilog="""Examples:
  encode song.mp3                    VQ4, 2-channel, 8kHz (default)
  encode song.mp3 -c lz             DeltaLZ compression
  encode song.mp3 -c off            Uncompressed (max ~2s per bank)
  encode song.mp3 -s 2              VQ2 (highest quality, less compression)
  encode song.mp3 -n 4              4-channel (louder, rougher)
  encode song.mp3 -g 0              VQ with noise gate disabled
  encode song.mp3 -g 20             VQ with stronger noise gate
  encode song.mp3 -a                ASM project only (no XEX)
  encode song.mp3 -x -a             Both XEX and ASM project""")

    parser.add_argument('input',
                        help='Input audio file (WAV, MP3, FLAC, OGG, MOD, ...)')

    # Output targets: default = XEX only; -a = ASM only; -x -a = both
    parser.add_argument('-x', '--xex', action='store_true', default=False,
                        help='Generate .xex binary (default when -a is not given)')
    parser.add_argument('-a', '--asm', action='store_true', default=False,
                        help='Generate assembly project (if alone: ASM only)')

    parser.add_argument('-o', '--output', default=None,
                        help='Output base name (default: outputs/<input>, adds .xex / _asm)')

    # Compression
    parser.add_argument('-c', '--compression', choices=['off', 'lz', 'vq'], default='vq',
                        help='Compression: vq (default), lz (DeltaLZ), off (raw)')
    parser.add_argument('-s', '--vec-size', type=int, choices=[2, 4, 8, 16], default=4,
                        help='VQ vector size (default: 4). Smaller = better quality, less compression')
    parser.add_argument('-g', '--gate', type=int, default=5, metavar='N',
                        help='VQ noise gate strength 0-100%% (default: 5). '
                             '0 = off, higher = more aggressive silence gating')

    # Audio
    parser.add_argument('-r', '--rate', type=int, default=8000,
                        help='Sample rate in Hz (default: 8000). Lower = longer duration')
    parser.add_argument('-n', '--channels', type=int, choices=[1, 2, 3, 4], default=2,
                        help='POKEY channels (1-4, default 2). More = louder but rougher')
    parser.add_argument('-e', '--enhance', action='store_true',
                        help='Treble pre-emphasis to compensate POKEY DAC rolloff')
    parser.add_argument('-m', '--mode', choices=['1cps', 'scalar'], default='scalar',
                        help='LZ encoding: scalar (default) or 1cps (1 write/IRQ, 12+ kHz)')

    # Advanced
    parser.add_argument('-b', '--max-banks', type=int, default=MAX_BANKS,
                        help=f'Max extended memory banks (default: {MAX_BANKS})')
    parser.add_argument('--no-noise-shaping', action='store_true',
                        help='Disable noise shaping (slightly faster, lower quality)')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Show compression verification details')

    args = parser.parse_args(argv)

    # Output logic: no flags → XEX; -a alone → ASM only; -x -a → both
    if not args.xex and not args.asm:
        args.xex = True

    # Validate gate range
    if not 0 <= args.gate <= 100:
        parser.error(f"--gate must be 0-100, got {args.gate}")

    try:
        return run(args)
    except StreamPlayerError as e:
        print(f"\nError: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"\nUnexpected error: {type(e).__name__}: {e}", file=sys.stderr)
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 2


def _derive_paths(args):
    """Derive XEX and ASM output paths from args.

    If -o is given, output goes where specified.
    Otherwise, outputs go into an 'outputs/' directory.
    """
    if args.output:
        base = args.output
        if base.lower().endswith('.xex'):
            base = base[:-4]
        if base.endswith('_asm'):
            base = base[:-4]
    else:
        output_dir = 'outputs'
        os.makedirs(output_dir, exist_ok=True)
        base = os.path.join(output_dir,
                            os.path.splitext(os.path.basename(args.input))[0])

    xex_path = base + '.xex' if args.xex else None
    asm_path = base + '_asm' if args.asm else None
    return xex_path, asm_path


def run(args) -> int:
    """Execute the conversion pipeline."""
    t0 = time.time()

    xex_path, asm_path = _derive_paths(args)

    # Show what we'll generate
    targets = []
    if xex_path:
        targets.append(os.path.basename(xex_path))
    if asm_path:
        targets.append(asm_path + '/')
    print(f"Output: {', '.join(targets)}")

    # ── 1. Load audio ──
    print(f"\nLoading: {args.input}")
    audio, src_rate, n_channels = load_audio(args.input)

    n_samples = audio.shape[0]
    input_duration = n_samples / src_rate
    ch_str = f"{n_channels} channel{'s' if n_channels > 1 else ''}"
    print(f"  Format: {src_rate} Hz, {ch_str}")
    print(f"  Duration: {_fmt_duration(input_duration)} ({n_samples:,} samples)")

    if input_duration < 0.1:
        raise AudioLoadError("Audio too short (< 0.1 seconds)")

    # ── 2. Find POKEY divisor ──
    divisor, actual_rate, audctl = find_best_divisor(args.rate)
    clk_name = "1.77MHz" if (audctl & 0x40) else "64kHz"
    print(f"\nPOKEY timer:")
    print(f"  Requested: {args.rate} Hz \u2192 divisor ${divisor:02X}, AUDCTL=${audctl:02X} ({clk_name})")
    print(f"  Actual: {actual_rate:.1f} Hz")

    # ── 3. Resample ──
    if abs(src_rate - actual_rate) / actual_rate > 0.001:
        print(f"\nResampling {src_rate} Hz \u2192 {actual_rate:.0f} Hz...")
        audio_rs = resample(audio, src_rate, int(actual_rate))
        print(f"  Output: {audio_rs.shape[0]:,} samples")
    else:
        audio_rs = audio
        print(f"\n  Sample rate matches, no resampling needed.")

    # ── 4. Encode to POKEY format ──
    noise_shaping = not args.no_noise_shaping
    bytes_per_sec = actual_rate
    compress_mode = args.compression  # 'vq', 'lz', or 'off'

    if compress_mode == 'vq':
        banks, encoded_duration, truncated, mode_label = \
            _encode_vq(args, audio_rs, n_channels, actual_rate, bytes_per_sec,
                       input_duration)
        vec_size = args.vec_size

    elif compress_mode == 'lz':
        banks, encoded_duration, truncated, mode_label = \
            _encode_lz(args, audio_rs, n_channels, actual_rate, bytes_per_sec,
                       noise_shaping, input_duration)
        vec_size = 4

    else:
        banks, encoded_duration, truncated, mode_label = \
            _encode_raw(args, audio_rs, n_channels, actual_rate, bytes_per_sec,
                        noise_shaping, input_duration)
        vec_size = 4
        compress_mode = 'raw'

    # ── 5. Generate ASM project ──
    source_name = os.path.basename(args.input)

    if asm_path:
        project_dir = asm_path
    elif xex_path:
        project_dir = tempfile.mkdtemp(prefix='stream_player_')
    else:
        return 0

    print(f"\nGenerating assembly project ({mode_label}, {len(banks)} banks)...")
    generate_project(
        output_dir=project_dir,
        banks=banks,
        compress_mode=compress_mode,
        divisor=divisor,
        audctl=audctl,
        actual_rate=actual_rate,
        pokey_channels=args.channels,
        vec_size=vec_size if compress_mode == 'vq' else 4,
        source_name=source_name,
        duration=encoded_duration,
        stereo=False,
    )

    if asm_path:
        n_files = len([f for f in os.listdir(project_dir)
                       if f.endswith('.asm') or f.endswith('.inc')])
        print(f"  {project_dir}/ ({n_files} source files)")

    # ── 6. Assemble (if XEX requested) ──
    if xex_path:
        print(f"\nAssembling...")
        assembled_xex, method = try_assemble(project_dir)

        if assembled_xex:
            shutil.copy2(assembled_xex, xex_path)
            xex_kb = os.path.getsize(xex_path) / 1024
            print(f"  {os.path.basename(xex_path)} ({xex_kb:.1f} KB) [{method}]")
        else:
            print(f"\n  Assembly failed: {method}", file=sys.stderr)
            if not asm_path:
                fallback_base = os.path.splitext(os.path.basename(args.input))[0]
                fallback_dir = os.path.join('outputs', fallback_base + '_asm')
                os.makedirs('outputs', exist_ok=True)
                if os.path.exists(fallback_dir):
                    shutil.rmtree(fallback_dir)
                shutil.copytree(project_dir, fallback_dir)
                print(f"\n  ASM project saved to: {fallback_dir}/")

    # Clean up temp dir if it was only for XEX
    if not asm_path and xex_path:
        shutil.rmtree(project_dir, ignore_errors=True)

    # ── 7. Summary ──
    elapsed = time.time() - t0
    n = len(banks)
    ram_kb = n * 16 + 64
    if n == 0:
        config = "64KB (base XL/XE)"
    elif n <= 4:
        config = "128KB (130XE)"
    elif n <= 16:
        config = "320KB (Rambo/Compy-Shop)"
    elif n <= 32:
        config = "576KB (512KB expansion)"
    else:
        config = "1088KB (1MB expansion)"

    print(f"\n{'=' * 50}")
    print(f"  {mode_label}, {args.channels}ch, {actual_rate:.0f} Hz")
    print(f"  {n} banks, {ram_kb}KB ({config})")
    if truncated:
        print(f"  Encoded: {_fmt_duration(encoded_duration)} "
              f"of {_fmt_duration(input_duration)} (truncated)")
    else:
        print(f"  Duration: {_fmt_duration(encoded_duration)}")
    print(f"  Completed in {elapsed:.1f}s")
    print(f"{'=' * 50}")

    return 0


# ══════════════════════════════════════════════════════════════════════
# Encoding helpers
# ══════════════════════════════════════════════════════════════════════

def _encode_vq(args, audio_rs, n_channels, actual_rate, bytes_per_sec,
               input_duration):
    """Encode audio with VQ compression."""
    from .vq import vq_encode_banks
    vs = args.vec_size

    ns_label = 'nearest (VQ-optimal)'
    if args.enhance:
        ns_label += '+enhanced'
    if args.gate > 0:
        ns_label += f'+gate{args.gate}%'
    print(f"\nEncoding (mono, {args.channels}-channel, {ns_label})...")
    indices = encode_indices(audio_rs, n_channels, False, False,
                            sample_rate=int(actual_rate),
                            pokey_channels=args.channels, mode='scalar',
                            enhance=args.enhance)
    print(f"  {len(indices):,} samples at {bytes_per_sec:,.0f} samples/sec")

    print(f"\nVQ encoding (vec_size={vs}, 256 codes per bank)...")

    def vq_progress(done, total, n_banks):
        pct = done / total if total else 1
        bar_w = 30
        filled = int(bar_w * pct)
        bar = '\u2588' * filled + '\u2591' * (bar_w - filled)
        print(f"\r  [{bar}] {pct*100:.1f}%  {done:,}/{total:,} samples, "
              f"{n_banks} banks", end='', flush=True)

    vq_banks, samples_compressed = vq_encode_banks(
        indices, vec_size=vs, max_banks=args.max_banks,
        max_level=max_level(args.channels), n_iter=20,
        gate=args.gate, progress_fn=vq_progress)
    print()

    encoded_duration = samples_compressed / bytes_per_sec
    truncated = samples_compressed < len(indices)

    if truncated:
        lost = len(indices) - samples_compressed
        lost_sec = lost / bytes_per_sec
        print(f"  Filled {len(vq_banks)} banks ({args.max_banks} max), "
              f"encoded {_fmt_duration(encoded_duration)} "
              f"of {_fmt_duration(input_duration)}")
        print(f"  Truncated {_fmt_duration(lost_sec)} "
              f"({lost:,} samples) to fit available memory.")
    else:
        compression = samples_compressed / (len(vq_banks) * 16384) if vq_banks else 1
        print(f"  {len(vq_banks)} banks, "
              f"{compression:.1f}\u00d7 compression (vec_size={vs})")

    return vq_banks, encoded_duration, truncated, f'VQ{vs}'


def _encode_lz(args, audio_rs, n_channels, actual_rate, bytes_per_sec,
               noise_shaping, input_duration):
    """Encode audio with DeltaLZ compression."""
    enc_mode = args.mode
    mode_label_enc = "1CPS" if enc_mode == '1cps' else f"{args.channels}-channel"
    ns_label = 'noise-shaped' if noise_shaping else 'nearest'
    if args.enhance:
        ns_label += '+enhanced'
    print(f"\nEncoding (mono, {mode_label_enc}, {ns_label})...")
    indices = encode_indices(audio_rs, n_channels, False, noise_shaping,
                            sample_rate=int(actual_rate),
                            pokey_channels=args.channels,
                            mode=enc_mode, enhance=args.enhance)
    print(f"  {len(indices):,} samples at {bytes_per_sec:,.0f} samples/sec")

    use_delta = (enc_mode != '1cps')
    lz_label = 'DeltaLZ' if use_delta else 'RawLZ'
    print(f"\nCompressing ({lz_label})...")
    compressed_banks, samples_compressed = compress_banks(
        indices, bank_size=16384, max_banks=args.max_banks,
        progress_fn=_compress_progress, use_delta=use_delta)
    print()

    comp_size = sum(len(b) for b in compressed_banks)
    encoded_duration = samples_compressed / bytes_per_sec
    truncated = samples_compressed < len(indices)

    if truncated:
        lost = len(indices) - samples_compressed
        print(f"  Filled {len(compressed_banks)} banks "
              f"({args.max_banks} max), encoded {_fmt_duration(encoded_duration)} "
              f"of {_fmt_duration(input_duration)}")
    else:
        ratio = comp_size / samples_compressed if samples_compressed > 0 else 1.0
        print(f"  {len(compressed_banks)} banks, "
              f"{comp_size:,} bytes, ratio {ratio:.0%}")

    if args.verbose:
        print(f"  Verifying decompression...")
        result = bytearray()
        for bank_data in compressed_banks:
            result.extend(decompress_bank(bank_data, use_delta))
        expected = indices[:samples_compressed]
        if bytes(result) != bytes(expected):
            raise CompressionError(
                f"Verification failed: expected {len(expected)}, "
                f"got {len(result)}")
        print(f"  OK ({len(result):,} samples)")

    mode_label = '1CPS-DeltaLZ' if enc_mode == '1cps' else 'DeltaLZ'
    return compressed_banks, encoded_duration, truncated, mode_label


def _encode_raw(args, audio_rs, n_channels, actual_rate, bytes_per_sec,
                noise_shaping, input_duration):
    """Encode audio uncompressed."""
    ns_label = 'noise-shaped' if noise_shaping else 'nearest'
    if args.enhance:
        ns_label += '+enhanced'
    print(f"\nEncoding (mono, {args.channels}-channel, {ns_label})...")
    encoded = encode_audio(audio_rs, n_channels, False, noise_shaping,
                           sample_rate=int(actual_rate),
                           pokey_channels=args.channels,
                           enhance=args.enhance)
    print(f"  {len(encoded):,} bytes ({len(encoded) // 1024}KB) "
          f"at {bytes_per_sec:,.0f} bytes/sec")

    max_raw = args.max_banks * 16384
    truncated = len(encoded) > max_raw
    if truncated:
        encoded = encoded[:max_raw]
        encoded_duration = max_raw / bytes_per_sec
        print(f"\n  Truncated to {_fmt_duration(encoded_duration)} "
              f"of {_fmt_duration(input_duration)} "
              f"to fit {args.max_banks} banks ({max_raw // 1024}KB).")
    else:
        encoded_duration = len(encoded) / bytes_per_sec

    banks = split_into_banks(encoded, args.max_banks)
    print(f"\n  {len(banks)} banks, {sum(len(b) for b in banks):,} bytes")

    return banks, encoded_duration, truncated, 'RAW'
