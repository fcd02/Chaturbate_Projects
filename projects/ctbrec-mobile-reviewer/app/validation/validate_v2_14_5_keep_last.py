#!/usr/bin/env python3
"""v2.14.5 regression: tagged timestamp parsing + truly model-wide Keep Last."""
from __future__ import annotations
import importlib.util
import os
import tempfile
import time
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('keep_last_v2145', ROOT / 'ctbrec_keep_last.py')
mod = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

# Timestamp can be in the middle, before or after tail/end-relative metadata.
cases = {
    'model_2026.09.14_12.34.56_tail_15m03s_est.mp4': ('2026-09-14T12:34:56', None),
    'model_tail_15m03s_est_2026.09.14_12.34.56.mp4': ('2026-09-14T12:34:56', None),
    'model_2026-09-14_12-34-56_tail_15m03s_EST.ts': ('2026-09-14T12:34:56', None),
    'model_20260914_123456_endminus_15m00s_to_0m00s.mp4': ('2026-09-14T12:34:56', None),
    'model_2026.09.14_12.34.56_tail_15m03s_est_segment_7.ts': ('2026-09-14T12:34:56', 7),
    'model_segment_8_tail_15m03s_est_2026.09.14_12.34.56.ts': ('2026-09-14T12:34:56', 8),
}
for name, (iso, segment) in cases.items():
    dt, seg = mod.parse_time(Path(name))
    assert dt is not None and dt.isoformat() == iso, (name, dt)
    assert seg == segment, (name, seg)

assert mod.labelled_duration(Path('x_2026.09.14_12.34.56_tail_1h2m03s_est.mp4')) == 3723.0
assert mod.labelled_duration(Path('x_2026.09.14_12.34.56_tail_1h_2m_03s_EST.mp4')) == 3723.0
assert mod.labelled_duration(Path('x_20260914_123456_endminus_15m00s_to_0m00s.mp4')) == 900.0
assert mod.labelled_duration(Path('x_20260914_123456_endminus_15m_00s_to_0m_00s.mp4')) == 900.0

# v2.14.6 superseded v2.14.5's model-wide retention scope at the user's
# explicit request.  This historical suite now protects only the tagged-filename
# parser/duration behavior introduced in v2.14.5; session semantics live in
# validate_v2_14_6_keep_last_sessions.py.

print('PASS v2.14.5 tagged-filename parser regression suite (retention scope superseded by v2.14.6)')
