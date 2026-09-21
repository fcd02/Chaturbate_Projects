#!/usr/bin/env python3
"""v2.14.6 regression: per-session Keep Last across drives + tagged filenames."""
from __future__ import annotations
import importlib.util
import os
import tempfile
import time
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('keep_last_v2146', ROOT / 'ctbrec_keep_last.py')
mod = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

# Keep the v2.14.5 parser improvement: timestamp may appear anywhere around tags.
cases = {
    'model_2026.09.14_12.34.56_tail_15m03s_est.mp4': ('2026-09-14T12:34:56', None),
    'model_tail_15m03s_est_2026.09.14_12.34.56.mp4': ('2026-09-14T12:34:56', None),
    'model_2026-09-14_12-34-56_tail_15m03s_EST.ts': ('2026-09-14T12:34:56', None),
    'model_20260914_123456_endminus_15m00s_to_0m00s.mp4': ('2026-09-14T12:34:56', None),
    'model_2026.09.14_12.34.56_tail_15m03s_est_segment_7.ts': ('2026-09-14T12:34:56', 7),
}
for name, (iso, segment) in cases.items():
    dt, seg = mod.parse_time(Path(name))
    assert dt is not None and dt.isoformat() == iso, (name, dt)
    assert seg == segment, (name, seg)

# One model spans E: and F:.  Drive boundaries must NOT split sessions.
# Session 1: 10:00/10:12/10:24 (cross-drive, contiguous coverage)
# Session 2: 12:00/12:12 (gap >30m after session 1 ends)
# 15-minute rule should retain the last two 10m files of EACH session.
tmp = Path(tempfile.mkdtemp(prefix='v2146-keeplast-'))
root_e = tmp / 'E'; root_f = tmp / 'F'
folder_e = root_e / 'samplemodel'; folder_f = root_f / 'samplemodel'
folder_e.mkdir(parents=True); folder_f.mkdir(parents=True)
files = [
    (folder_e, 'samplemodel_2026.09.14_10.00.00_tail_10m00s_est.mp4'),
    (folder_f, 'samplemodel_tail_10m00s_est_2026.09.14_10.12.00.mp4'),
    (folder_e, 'samplemodel_20260914_102400_endminus_10m00s_to_0m00s.mp4'),
    (folder_f, 'samplemodel_2026.09.14_12.00.00_tail_10m00s_est.mp4'),
    (folder_e, 'samplemodel_2026.09.14_12.12.00_tail_10m00s_est.mp4'),
]
old = time.time() - 3600
for folder, name in files:
    p = folder / name
    p.write_bytes(b'x' * 100)
    os.utime(p, (old, old))

plan = mod.build_plan([root_e, root_f], {'samplemodel': 15.0}, gap_minutes=30, recent_write_grace_seconds=0)
kept = [Path(x['source']).name for x in plan['review_moves']]
deleted = [Path(x['source']).name for x in plan['delete_moves']]
assert plan['sessions'] == 2, plan
assert len(kept) == 4, kept
assert len(deleted) == 1, deleted
assert '10.00.00' in deleted[0], deleted
for token in ('10.12.00', '102400', '12.00.00', '12.12.00'):
    assert any(token in name for name in kept), (token, kept)

# Exactly 30 minutes of uncovered gap is still the SAME session; only >30 splits.
rows = [
    mod.Row('m', Path('.'), Path('a'), mod.datetime(2026, 9, 14, 10, 0), None, 1, old, 600),
    mod.Row('m', Path('.'), Path('b'), mod.datetime(2026, 9, 14, 10, 40), None, 1, old, 600), # 30m after 10:10 end
    mod.Row('m', Path('.'), Path('c'), mod.datetime(2026, 9, 14, 11, 21), None, 1, old, 600), # 31m after 10:50 end
]
blocks = mod._session_blocks(rows, 30)
assert [len(b) for b in blocks] == [2, 1], [len(b) for b in blocks]

# Same-base segmented recordings extend coverage consecutively for gap detection.
segrows = [
    mod.Row('m', Path('.'), Path('seg1'), mod.datetime(2026, 9, 14, 14, 0), 1, 1, old, 900),
    mod.Row('m', Path('.'), Path('seg2'), mod.datetime(2026, 9, 14, 14, 0), 2, 1, old, 900),
    mod.Row('m', Path('.'), Path('next'), mod.datetime(2026, 9, 14, 15, 0), None, 1, old, 900),
]
# Segment coverage reaches 14:30, so 15:00 is exactly a 30m uncovered gap => same session.
assert len(mod._session_blocks(segrows, 30)) == 1

print('PASS v2.14.6 Keep Last per-session/cross-drive regression suite')
