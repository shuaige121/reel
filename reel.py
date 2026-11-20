#!/usr/bin/env python3
"""reel 工具入口（Mac 主控）。用法见 python3 reel.py -h；每个子命令输出一个 JSON。"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reelcore.cli import main  # noqa: E402

sys.exit(main())
