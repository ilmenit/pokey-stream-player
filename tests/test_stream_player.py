"""Tests for the stream-player package.

Tests cover: POKEY tables, audio encoding, DeltaLZ compression, bank layout,
VQ encoding, audio enhancement, ASM project generation, and end-to-end CLI.

XEX building requires MADS assembler and is tested separately if available.
"""

import os
import sys
import struct
import wave
import shutil
import tempfile
import unittest
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

from stream_player.tables import (quantize_single, quantize_dual, pack_dual_byte,
                                   VOLTAGE_TABLE_SINGLE, VOLTAGE_TABLE_DUAL)
from stream_player.audio import (load_audio, resample, encode_mono_dual,
                                  encode_stereo_dual, find_best_divisor)
from stream_player.compress import compress_bank, decompress_bank, compress_banks
from stream_player.layout import split_into_banks, bank_portb_table, BANK_SIZE
from stream_player.errors import *


# ═══════════════════════════════════════════════════════════════════════
# POKEY Tables Tests
# ═══════════════════════════════════════════════════════════════════════

class TestPokeyTables(unittest.TestCase):

    def test_single_table_size(self):
        self.assertEqual(len(VOLTAGE_TABLE_SINGLE), 16)

    def test_dual_table_size(self):
        self.assertEqual(len(VOLTAGE_TABLE_DUAL), 31)

    def test_tables_monotonic(self):
        for i in range(1, len(VOLTAGE_TABLE_SINGLE)):
            self.assertGreater(VOLTAGE_TABLE_SINGLE[i], VOLTAGE_TABLE_SINGLE[i-1])
        for i in range(1, len(VOLTAGE_TABLE_DUAL)):
            self.assertGreater(VOLTAGE_TABLE_DUAL[i], VOLTAGE_TABLE_DUAL[i-1])

    def test_quantize_silence(self):
        audio = np.zeros(100, dtype=np.float32) - 1.0
        idx = quantize_single(audio, noise_shaping=False)
        self.assertTrue(np.all(idx == 0))

    def test_quantize_max(self):
        audio = np.ones(100, dtype=np.float32)
        idx = quantize_single(audio, noise_shaping=False)
        self.assertTrue(np.all(idx == 15))

    def test_dual_pack(self):
        self.assertEqual(pack_dual_byte(0), 0x00)
        self.assertEqual(pack_dual_byte(1), 0x01)
        self.assertEqual(pack_dual_byte(30), 0xFF)


# ═══════════════════════════════════════════════════════════════════════
# Audio Tests
# ═══════════════════════════════════════════════════════════════════════

class TestAudio(unittest.TestCase):

    def _make_wav(self, path, sr=44100, duration=0.5, freq=440, channels=1):
        t = np.linspace(0, duration, int(sr * duration), dtype=np.float32)
        mono = (np.sin(2 * np.pi * freq * t) * 0.8).astype(np.float32)
        samples = (mono * 32767).astype(np.int16)
        if channels == 2:
            samples = np.column_stack([samples, samples]).flatten()
        with wave.open(path, 'wb') as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(samples.tobytes())

    def test_load_wav_mono(self):
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
            path = f.name
        try:
            self._make_wav(path, sr=22050, channels=1)
            audio, sr, ch = load_audio(path)
            self.assertEqual(sr, 22050)
            self.assertEqual(ch, 1)
            self.assertEqual(audio.ndim, 1)
        finally:
            os.unlink(path)

    def test_load_wav_stereo(self):
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
            path = f.name
        try:
            self._make_wav(path, sr=44100, channels=2)
            audio, sr, ch = load_audio(path)
            self.assertEqual(ch, 2)
            self.assertEqual(audio.ndim, 2)
        finally:
            os.unlink(path)

    def test_resample(self):
        audio = np.sin(np.linspace(0, 10, 1000, dtype=np.float32))
        out = resample(audio, 44100, 15000)
        expected_len = int(1000 * 15000 / 44100)
        self.assertAlmostEqual(len(out), expected_len, delta=2)

    def test_find_divisor(self):
        div, rate, audctl = find_best_divisor(15000)
        self.assertGreater(rate, 10000)
        self.assertLess(rate, 20000)
        self.assertIn(audctl, [0x00, 0x40])

    def test_encode_mono(self):
        audio = np.sin(np.linspace(0, 10, 1000, dtype=np.float32)) * 0.5
        encoded = encode_mono_dual(audio)
        self.assertEqual(len(encoded), 1000)
        for b in encoded:
            self.assertLessEqual((b >> 4) & 0x0F, 15)
            self.assertLessEqual(b & 0x0F, 15)

    def test_encode_stereo(self):
        audio = np.column_stack([
            np.sin(np.linspace(0, 10, 500, dtype=np.float32)) * 0.5,
            np.cos(np.linspace(0, 10, 500, dtype=np.float32)) * 0.5,
        ])
        encoded = encode_stereo_dual(audio)
        self.assertEqual(len(encoded), 1000)


# ═══════════════════════════════════════════════════════════════════════
# Compression Tests
# ═══════════════════════════════════════════════════════════════════════

class TestCompression(unittest.TestCase):

    def test_roundtrip_silence(self):
        indices = bytes([15] * 500)
        comp, _ = compress_bank(indices, 0)
        dec = decompress_bank(comp)
        self.assertEqual(dec, indices)
        self.assertLess(len(comp), len(indices) // 2)

    def test_roundtrip_ramp(self):
        indices = bytes(list(range(31)) * 10)
        comp, _ = compress_bank(indices, 0)
        dec = decompress_bank(comp)
        self.assertEqual(dec, indices)

    def test_roundtrip_random(self):
        np.random.seed(42)
        indices = bytes(np.random.randint(0, 31, 4096, dtype=np.uint8))
        comp, _ = compress_bank(indices, 0)
        dec = decompress_bank(comp)
        self.assertEqual(dec, indices)

    def test_roundtrip_audio_like(self):
        np.random.seed(42)
        t = np.linspace(0, 10, 4096, dtype=np.float32)
        sig = np.sin(t) * 12 + 15 + np.random.randn(4096) * 0.5
        indices = bytes(np.clip(sig, 0, 30).astype(np.uint8))
        comp, _ = compress_bank(indices, 0)
        dec = decompress_bank(comp)
        self.assertEqual(dec, indices)
        self.assertLess(len(comp), len(indices) * 0.6)

    def test_bank_continuity(self):
        indices = bytes([10, 11, 12, 13, 14, 15])
        b1, _ = compress_bank(indices[:3], prev_value=0)
        d1 = decompress_bank(b1)
        self.assertEqual(d1, indices[:3])
        b2, _ = compress_bank(indices[3:], prev_value=indices[2])
        d2 = decompress_bank(b2)
        self.assertEqual(d2, indices[3:])

    def test_compress_banks(self):
        np.random.seed(42)
        indices = bytes(np.random.randint(0, 31, 50000, dtype=np.uint8))
        banks, pos = compress_banks(indices, bank_size=2048, max_banks=64)
        self.assertGreaterEqual(len(banks), 1)
        self.assertEqual(pos, len(indices))
        for b in banks:
            self.assertLessEqual(len(b), 2048)
        result = bytearray()
        for bank_data in banks:
            result.extend(decompress_bank(bank_data))
        self.assertEqual(bytes(result), indices)


# ═══════════════════════════════════════════════════════════════════════
# Bank Layout Tests
# ═══════════════════════════════════════════════════════════════════════

class TestLayout(unittest.TestCase):

    def test_single_bank(self):
        data = bytes(range(256))
        banks = split_into_banks(data)
        self.assertEqual(len(banks), 1)
        self.assertEqual(banks[0], data)

    def test_multi_bank(self):
        data = bytes([0xFF]) * (BANK_SIZE * 3 + 100)
        banks = split_into_banks(data)
        self.assertEqual(len(banks), 4)
        self.assertEqual(len(banks[0]), BANK_SIZE)
        self.assertEqual(len(banks[-1]), 100)

    def test_overflow(self):
        data = bytes([0x00]) * (BANK_SIZE * 65)
        with self.assertRaises(BankOverflowError):
            split_into_banks(data, max_banks=64)

    def test_portb_table(self):
        portb = bank_portb_table(4)
        self.assertEqual(len(portb), 4)
        self.assertEqual(portb[0], 0xE3)


# ═══════════════════════════════════════════════════════════════════════
# VQ Tests
# ═══════════════════════════════════════════════════════════════════════

class TestVQ(unittest.TestCase):

    def test_roundtrip(self):
        from stream_player.vq import vq_encode_banks, vq_decode_banks
        t = np.linspace(0, 2*np.pi*10, 2000)
        indices = np.clip(np.round(15 + 14*np.sin(t)), 0, 30).astype(np.uint8)

        for vs in [4, 8, 16]:
            banks, n_enc = vq_encode_banks(indices, vec_size=vs,
                                           max_level=30, n_iter=10)
            decoded = vq_decode_banks(banks, vs, total_samples=n_enc)
            rmse = np.sqrt(np.mean((indices[:n_enc].astype(float)
                                    - decoded.astype(float))**2))
            self.assertLess(rmse, 2.0, f"VQ vec={vs} RMSE too high: {rmse}")

    def test_bank_geometry(self):
        from stream_player.vq import vq_bank_geometry
        for vs, expected_cb in [(4, 1024), (8, 2048), (16, 4096)]:
            cb, ipb, spb = vq_bank_geometry(vs)
            self.assertEqual(cb, expected_cb)
            self.assertEqual(ipb, 16384 - expected_cb)
            self.assertEqual(spb, ipb * vs)

    def test_accepts_bytes_input(self):
        from stream_player.vq import vq_encode_banks
        data = bytes(range(32)) * 4
        banks, n_enc = vq_encode_banks(data, vec_size=8,
                                       max_level=31, n_iter=5)
        self.assertGreater(n_enc, 0)
        self.assertGreater(len(banks), 0)


# ═══════════════════════════════════════════════════════════════════════
# ASM Project Generation Tests
# ═══════════════════════════════════════════════════════════════════════

class TestAsmProject(unittest.TestCase):
    """Test MADS assembly project generation for all modes."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='test_asm_')

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_vq_project_structure(self):
        """VQ project generates all required files."""
        from stream_player.asm_project import generate_project
        from stream_player.vq import vq_encode_banks
        from stream_player.tables import max_level

        data = np.random.randint(0, 31, 8000, dtype=np.uint8)
        banks, _ = vq_encode_banks(data, vec_size=4, max_level=30)
        outdir = os.path.join(self.tmpdir, 'vq')

        generate_project(outdir, banks, 'vq', 0xDD, 0x40, 7988.5,
                         pokey_channels=2, vec_size=4)

        # All static files present
        for f in ['stream_player.asm', 'atari.inc', 'splash.asm',
                  'player_vq.asm', 'irq_vq.asm', 'pokey_setup.asm',
                  'copy_rom.asm', 'mem_detect.asm', 'zeropage_vq.inc']:
            self.assertTrue(os.path.exists(os.path.join(outdir, f)),
                            f"Missing: {f}")

        # Generated data files present
        for f in ['config.asm', 'audc_tables.asm', 'portb_table.asm',
                  'vq_tables.asm', 'splash_data.asm', 'banks.asm']:
            self.assertTrue(os.path.exists(os.path.join(outdir, f)),
                            f"Missing generated: {f}")

        # Bank data files present
        for i in range(len(banks)):
            self.assertTrue(os.path.exists(
                os.path.join(outdir, f'bank_{i:02d}.asm')))

        # Config has correct mode
        with open(os.path.join(outdir, 'config.asm')) as fh:
            cfg = fh.read()
        self.assertIn('COMPRESS_MODE   = 2', cfg)
        self.assertIn('VEC_SIZE        = 4', cfg)
        self.assertIn(f'N_BANKS         = {len(banks)}', cfg)

    def test_lz_project_structure(self):
        """LZ project generates correct files (no vq_tables)."""
        from stream_player.asm_project import generate_project

        data = bytes(np.random.randint(0, 31, 8000, dtype=np.uint8))
        banks, _ = compress_banks(data, bank_size=16384, max_banks=64)
        outdir = os.path.join(self.tmpdir, 'lz')

        generate_project(outdir, banks, 'lz', 0xDD, 0x40, 7988.5,
                         pokey_channels=4)

        self.assertTrue(os.path.exists(os.path.join(outdir, 'player_lz.asm')))
        self.assertTrue(os.path.exists(os.path.join(outdir, 'irq_lz.asm')))
        self.assertFalse(os.path.exists(os.path.join(outdir, 'vq_tables.asm')))

        with open(os.path.join(outdir, 'config.asm')) as fh:
            cfg = fh.read()
        self.assertIn('COMPRESS_MODE   = 1', cfg)
        self.assertIn('POKEY_CHANNELS  = 4', cfg)
        self.assertNotIn('VEC_SIZE', cfg)

    def test_raw_project_structure(self):
        """RAW project generates correct files."""
        from stream_player.asm_project import generate_project

        data = bytes(np.random.randint(0, 31, 8000, dtype=np.uint8))
        banks = split_into_banks(data)
        outdir = os.path.join(self.tmpdir, 'raw')

        generate_project(outdir, banks, 'raw', 0xDD, 0x40, 7988.5,
                         pokey_channels=2)

        self.assertTrue(os.path.exists(os.path.join(outdir, 'player_raw.asm')))
        self.assertTrue(os.path.exists(os.path.join(outdir, 'irq_raw.asm')))

        with open(os.path.join(outdir, 'config.asm')) as fh:
            cfg = fh.read()
        self.assertIn('COMPRESS_MODE   = 0', cfg)

    def test_audc_tables_content(self):
        """AUDC tables have correct values for 2-channel mode."""
        from stream_player.asm_project import generate_project
        from stream_player.tables import index_to_volumes, max_level

        data = bytes([15] * 100)
        banks = split_into_banks(data)
        outdir = os.path.join(self.tmpdir, 'audc')

        generate_project(outdir, banks, 'raw', 0xDD, 0x40, 7988.5,
                         pokey_channels=2)

        with open(os.path.join(outdir, 'audc_tables.asm')) as fh:
            content = fh.read()

        # Should have 2 tables
        self.assertIn('audc1_tab:', content)
        self.assertIn('audc2_tab:', content)
        # No table for channels 3-4
        self.assertNotIn('audc3_tab:', content)

    def test_all_channel_vec_combinations(self):
        """All pokey_channels × vec_size combinations generate valid projects."""
        from stream_player.asm_project import generate_project
        from stream_player.vq import vq_encode_banks
        from stream_player.tables import max_level

        data = np.random.randint(0, 15, 4000, dtype=np.uint8)

        for nch in [1, 2, 3, 4]:
            ml = max_level(nch)
            clipped = np.minimum(data, ml)
            for vs in [2, 4, 8, 16]:
                banks, _ = vq_encode_banks(clipped, vec_size=vs,
                                           max_level=ml, n_iter=5)
                outdir = os.path.join(self.tmpdir, f'ch{nch}_vs{vs}')
                generate_project(outdir, banks, 'vq', 0xDD, 0x40, 7988.5,
                                 pokey_channels=nch, vec_size=vs)

                # Verify key files exist
                self.assertTrue(os.path.exists(
                    os.path.join(outdir, 'stream_player.asm')))
                self.assertTrue(os.path.exists(
                    os.path.join(outdir, 'config.asm')))

    def test_builtin_assembler(self):
        """Built-in assembler produces valid XEX from generated project."""
        from stream_player.asm_project import generate_project, try_assemble

        data = bytes([15] * 100)
        banks = split_into_banks(data)
        outdir = os.path.join(self.tmpdir, 'build')

        generate_project(outdir, banks, 'raw', 0xDD, 0x40, 7988.5)

        xex_path, method = try_assemble(outdir)
        self.assertIsNotNone(xex_path, f"Assembly failed: {method}")
        self.assertEqual(method, 'built-in')
        self.assertTrue(os.path.exists(xex_path))

        # Verify XEX structure
        with open(xex_path, 'rb') as f:
            xex = f.read()
        self.assertTrue(xex.startswith(b'\xFF\xFF'))
        self.assertGreater(len(xex), 1000)


# ═══════════════════════════════════════════════════════════════════════
# End-to-End Tests (ASM project generation via CLI)
# ═══════════════════════════════════════════════════════════════════════

class TestEndToEnd(unittest.TestCase):

    def _make_test_wav(self, path, duration=0.5, sr=44100):
        t = np.linspace(0, duration, int(sr * duration), dtype=np.float32)
        samples = (np.sin(2 * np.pi * 440 * t) * 0.5 * 32767).astype(np.int16)
        with wave.open(path, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(samples.tobytes())

    def test_pipeline_vq_asm(self):
        """CLI generates VQ ASM project."""
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as wf:
            wav_path = wf.name
        asm_dir = wav_path.replace('.wav', '_asm')

        try:
            self._make_test_wav(wav_path, duration=0.5)
            from stream_player.cli import main
            result = main([wav_path, '--no-xex', '-a', '-o',
                           wav_path.replace('.wav', '')])
            self.assertEqual(result, 0)
            self.assertTrue(os.path.isdir(asm_dir))
            self.assertTrue(os.path.exists(
                os.path.join(asm_dir, 'stream_player.asm')))
            self.assertTrue(os.path.exists(
                os.path.join(asm_dir, 'config.asm')))
        finally:
            if os.path.exists(wav_path):
                os.unlink(wav_path)
            if os.path.isdir(asm_dir):
                shutil.rmtree(asm_dir)

    def test_pipeline_lz_asm(self):
        """CLI generates LZ ASM project."""
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as wf:
            wav_path = wf.name
        asm_dir = wav_path.replace('.wav', '_asm')

        try:
            self._make_test_wav(wav_path, duration=0.5)
            from stream_player.cli import main
            result = main([wav_path, '--no-xex', '-a', '-c', 'lz', '-o',
                           wav_path.replace('.wav', '')])
            self.assertEqual(result, 0)
            self.assertTrue(os.path.isdir(asm_dir))

            with open(os.path.join(asm_dir, 'config.asm')) as fh:
                cfg = fh.read()
            self.assertIn('COMPRESS_MODE   = 1', cfg)
        finally:
            if os.path.exists(wav_path):
                os.unlink(wav_path)
            if os.path.isdir(asm_dir):
                shutil.rmtree(asm_dir)

    def test_pipeline_raw_asm(self):
        """CLI generates RAW ASM project."""
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as wf:
            wav_path = wf.name
        asm_dir = wav_path.replace('.wav', '_asm')

        try:
            self._make_test_wav(wav_path, duration=0.5)
            from stream_player.cli import main
            result = main([wav_path, '--no-xex', '-a', '-c', 'off', '-o',
                           wav_path.replace('.wav', '')])
            self.assertEqual(result, 0)
            self.assertTrue(os.path.isdir(asm_dir))

            with open(os.path.join(asm_dir, 'config.asm')) as fh:
                cfg = fh.read()
            self.assertIn('COMPRESS_MODE   = 0', cfg)
        finally:
            if os.path.exists(wav_path):
                os.unlink(wav_path)
            if os.path.isdir(asm_dir):
                shutil.rmtree(asm_dir)


# ═══════════════════════════════════════════════════════════════════════
# Enhancement Tests
# ═══════════════════════════════════════════════════════════════════════

class TestEnhance(unittest.TestCase):

    def test_compress_dynamics(self):
        from stream_player.enhance import compress_dynamics
        loud = np.ones(100, dtype=np.float32) * 0.9
        quiet = np.ones(100, dtype=np.float32) * 0.05
        audio = np.concatenate([loud, quiet])
        compressed = compress_dynamics(audio, strength=0.5)
        ratio_before = np.mean(np.abs(audio[:100])) / np.mean(np.abs(audio[100:]))
        ratio_after = np.mean(np.abs(compressed[:100])) / np.mean(np.abs(compressed[100:]))
        self.assertLess(ratio_after, ratio_before)

    def test_zoh_preemphasis(self):
        from stream_player.enhance import apply_zoh_preemphasis
        sr = 8000
        t = np.linspace(0, 0.1, int(sr * 0.1))
        audio = np.sin(2 * np.pi * 3000 * t).astype(np.float32)
        boosted = apply_zoh_preemphasis(audio, sr)
        self.assertGreater(np.sqrt(np.mean(boosted**2)),
                           np.sqrt(np.mean(audio**2)))

    def test_enhance_bypass_at_zero(self):
        from stream_player.enhance import compress_dynamics
        audio = np.random.randn(500).astype(np.float32) * 0.5
        result = compress_dynamics(audio, strength=0.0)
        np.testing.assert_array_equal(audio, result)


if __name__ == '__main__':
    unittest.main(verbosity=2)
