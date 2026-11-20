#!/usr/bin/env python3
"""reel tool entry point (Mac controller). See python3 reel.py -h for usage; every subcommand prints one JSON object."""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reelcore.cli import main  # noqa: E402

sys.exit(main())
