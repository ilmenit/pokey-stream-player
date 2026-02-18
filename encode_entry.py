#!/usr/bin/env python3
"""Entry point for PyInstaller-frozen executable."""

import sys
import os

# When frozen, ensure the bundled data directory is findable
if getattr(sys, 'frozen', False):
    os.chdir(os.path.dirname(sys.executable))

from stream_player.cli import main
sys.exit(main())
