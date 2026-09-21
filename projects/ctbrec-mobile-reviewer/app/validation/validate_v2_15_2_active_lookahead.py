#!/usr/bin/env python3
"""v2.15.2 regression: bounded automatic active-model look-ahead refill."""
from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SERVER_TEXT = (ROOT / 'ctbrec_mobile_server.py').read_text(encoding='utf-8')
APP = (ROOT / 'static' / 'app.js').read_text(encoding='utf-8')
INDEX = (ROOT / 'static' / 'index.html').read_text(encoding='utf-8')
SW = (ROOT / 'static' / 'service-worker.js').read_text(encoding='utf-8')

assert 'CTBRecMobile/2.15.10' in SERVER_TEXT
assert '?v=21510' in INDEX
assert 'ctbrec-shell-v21510' in SW
assert 'Automatic Look-ahead' in APP
assert 'queue_data["preparing_model"] = True' in SERVER_TEXT
assert 'self.schedule_prefetch(target)' in SERVER_TEXT
assert SERVER_TEXT.count('self.schedule_prefetch(queue_id)') >= 5
assert 'if active_queue_id:\n                    self.schedule_prefetch(active_queue_id)' in SERVER_TEXT

# Import only after static identity checks.
import sys
sys.path.insert(0, str(ROOT))
from ctbrec_mobile_server import MobileReviewerState  # noqa: E402


class DoneFuture:
    def done(self):
        return True


class ImmediateExecutor:
    def submit(self, fn):
        fn()
        return DoneFuture()


state = object.__new__(MobileReviewerState)
state.lock = threading.RLock()
state.stop_event = threading.Event()
state.active_sort_queue_id = 'q1'
state.sort_session_active_until = 10**12
state.prefetch_futures = {}
state.interactive_futures = {}
state.prefetch_executor = ImmediateExecutor()
state.queues = {}
state.config = {
    'background_mosaics': {
        'original': {'enabled': True, 'upcoming_count': 3, 'idle_only': False, 'idle_minutes': 0},
        'review': {'enabled': True, 'upcoming_count': 3, 'idle_only': False, 'idle_minutes': 0},
    }
}
state._prune_finished_scheduler_futures = lambda: None
state.sort_session_active = lambda: True
state.background_settings_for_mode = lambda mode: dict(state.config['background_mosaics']['original'])
state._chunk_mosaic_ready = lambda queue, chunk: bool(chunk.ready)

generated = []
def ensure(queue, chunk, force, progress):
    generated.append(chunk.signature)
    chunk.ready = True
state.ensure_chunk_mosaic = ensure

chunks = [SimpleNamespace(signature=f'c{i}', ready=(i <= 2)) for i in range(6)]
queue = {
    'id': 'q1', 'mode': 'original', 'model': 'demo', 'chunks': list(chunks),
    'preparing_model': False, 'prefetch_cancel_requested': False, 'prefetch_status': {},
}
state.queues['q1'] = queue

# User scenario: 3 mosaics total are initially ready (current + 2 future).
# With upcoming_count=3, the missing third *future* mosaic must be generated,
# but the fourth future mosaic must remain untouched.
state.schedule_prefetch('q1')
assert generated == ['c3'], generated
assert chunks[3].ready is True
assert chunks[4].ready is False
assert queue['prefetch_status']['ready'] == 3
assert queue['prefetch_status']['target'] == 3

# After sorting/removing the current chunk, the window slides. The same bounded
# refill must generate exactly one new tail chunk and keep the next one untouched.
queue['chunks'].pop(0)
state.schedule_prefetch('q1')
assert generated == ['c3', 'c4'], generated
assert chunks[4].ready is True
assert chunks[5].ready is False
assert queue['prefetch_status']['ready'] == 3
assert queue['prefetch_status']['target'] == 3

# Leaving/switching models still forbids hidden automatic generation.
state.active_sort_queue_id = 'other'
queue['chunks'].pop(0)
state.schedule_prefetch('q1')
assert generated == ['c3', 'c4'], generated

print('PASS: v2.15.2 bounded active-model look-ahead refill')
