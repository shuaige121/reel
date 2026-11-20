#!/usr/bin/env python3
"""Multilingual regression of lyrics comparison: python3 tests/test_lyrics_norm.py"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from reelcore.lyrics import norm

assert norm("第一句，歌词 A1！") == "第一句歌词a1"
assert norm("เลิกงานแล้ว ขี่กลับบ้านช้า ๆ") == "เลิกงานแล้วขี่กลับบ้านช้าๆ"
assert norm("ไป") != norm("ไม้"), "different Thai words must not compare equal"
assert norm("ก่อน") != norm("ก้อน"), "tone marks must count"
assert norm("...!?，。 ") == ""
print("ok")
