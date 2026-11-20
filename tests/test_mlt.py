#!/usr/bin/env python3
"""MLT project reading and repeated footage: python3 tests/test_mlt.py (a minimal synthetic MLT, no external project needed)."""
import json, os, shutil, subprocess, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from reelcore import mlt  # noqa: E402
from reelcore.errors import ReelError  # noqa: E402

T = tempfile.mkdtemp(prefix="reel_mlt_")
XML = """<?xml version="1.0" encoding="utf-8"?>
<mlt LC_NUMERIC="C" producer="main">
 <profile frame_rate_num="25" frame_rate_den="1" width="1920" height="1080"/>
 <producer id="p0"><property name="resource">clips/a.mp4</property><property name="mlt_service">avformat</property>
  <property name="shotcut:caption">A · paper crush</property></producer>
 <producer id="p1"><property name="resource">clips/b.mov</property><property name="mlt_service">avformat</property></producer>
 <producer id="p2"><property name="resource">clips/a.mp4</property><property name="mlt_service">avformat</property>
  <property name="shotcut:caption">A again</property></producer>
 <producer id="c0"><property name="resource">0x00000000</property><property name="mlt_service">color</property></producer>
 <producer id="s0"><property name="resource">gfx/footer.svg</property></producer>
 <producer id="s1"><property name="resource">gfx/footer.svg</property></producer>
 <playlist id="V1"><property name="shotcut:name">V1 main</property><property name="shotcut:video">1</property>
  <entry producer="p0" in="0" out="49"><filter><property name="mlt_service">frei0r.cairoblend</property>
   <property name="shotcut:filter">sizePosition</property><property name="rect">0 0 1920 1080 1</property></filter></entry>
  <blank length="25"/>
  <entry producer="p1" in="00:00:01.000" out="74"/>
  <entry producer="p2" in="25" out="99"/>
 </playlist>
 <playlist id="V2"><property name="shotcut:name">V2 overlay</property><property name="shotcut:video">1</property>
  <entry producer="c0" in="0" out="99"/><entry producer="c0" in="0" out="99"/>
  <entry producer="s0" in="0" out="99"/><entry producer="s1" in="0" out="99"/>
 </playlist>
 <playlist id="A1"><property name="shotcut:name">A1 music</property><property name="shotcut:audio">1</property>
  <entry producer="p0" in="0" out="49"/></playlist>
 <tractor id="main"><track producer="V1"/><track producer="V2"/><track producer="A1" hide="video"/></tractor>
</mlt>"""
p = os.path.join(T, "proj.mlt"); open(p, "w").write(XML)
bad = 0
def check(n, c, d=""):
    global bad; print(("✅ " if c else "❌ ") + n + ("" if c else f"  {d}")); bad += 0 if c else 1

tl = mlt.load(p)
v1 = next(t for t in tl["tracks"] if t["id"] == "V1")
check("fps", tl["fps"] == 25, tl["fps"])
check("entry frames and blank advance", [(e["start_f"], e["end_f"]) for e in v1["events"]] == [(0, 50), (75, 125), (125, 200)],
      [(e["start_f"], e["end_f"]) for e in v1["events"]])
check("timecode in point", v1["events"][1]["in_f"] == 25, v1["events"][1]["in_f"])
check("resource resolved relative to project", v1["events"][0]["resource"] == os.path.join(T, "clips/a.mp4"))
check("filter recorded", v1["events"][0]["filters"][0]["filter"] == "sizePosition")
check("audio track kind", next(t for t in tl["tracks"] if t["id"] == "A1")["kind"] == "audio")
d = mlt.duplicates(tl)
check("duplicate footage found once (a.mp4 frames 25-49 used twice)", len(d) == 1 and d[0]["resource"] == "a.mp4"
      and d[0]["overlap_s"] == 1.0, d)
check("color placeholders and svg layers ignored", all(x["resource"] == "a.mp4" for x in d), d)
txt = mlt.text(tl)
check("text timeline has captions", "A · paper crush" in txt and "fx: sizePosition" in txt, txt[:300])
open(os.path.join(T, "empty.mlt"), "w").close()
try:
    mlt.load(os.path.join(T, "empty.mlt")); check("0-byte iCloud placeholder rejected", False)
except ReelError as e:
    check("0-byte iCloud placeholder rejected", e.code == "MLT_INVALID", e.code)
r = json.loads(subprocess.run([sys.executable, os.path.join(ROOT, "reel.py"), "mlt", p, "--dupes"], capture_output=True, text=True).stdout)
check("cli mlt --dupes", r["ok"] and len(r["result"]["duplicates"]) == 1, r)
print(f"\n{'all passed' if not bad else f'{bad} failed'}")
shutil.rmtree(T, ignore_errors=True)
sys.exit(1 if bad else 0)
