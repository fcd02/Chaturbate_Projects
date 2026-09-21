#!/usr/bin/env python3
"""v2.15.3 regression: pending model-open queues cannot become permanent orphans."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = (ROOT / 'ctbrec_mobile_server.py').read_text(encoding='utf-8')
INDEX = (ROOT / 'static' / 'index.html').read_text(encoding='utf-8')
SW = (ROOT / 'static' / 'service-worker.js').read_text(encoding='utf-8')

assert 'CTBRecMobile/2.15.10' in SERVER
assert '?v=21510' in INDEX
assert 'ctbrec-shell-v21510' in SW
assert 'def _sync_pending_model_task' in SERVER
assert 'targets.difference_update(processed_targets)' in SERVER
assert 'self.priority_generation_targets.pop(key, None)' in SERVER
assert 'Recovering model-open indexing' in SERVER
assert 'queue_data["prepare_attempts"]' in SERVER

sys.path.insert(0, str(ROOT))
from ctbrec_mobile_server import MobileReviewerState, TaskRecord  # noqa: E402


def fresh_state():
    state = object.__new__(MobileReviewerState)
    state.lock = threading.RLock()
    state.queues = {}
    state.tasks = {}
    state.active_sort_queue_id = 'q1'
    state.sort_session_active_until = time.time() + 300
    return state

# Reproduce the v2.15.2 orphan: the shared indexing task completed and reported
# that it found chunks, but this late-attached queue never received them.
state = fresh_state()
queue = {
    'id': 'q1', 'mode': 'original', 'model': 'demo', 'drives': ['E'],
    'chunks': [], 'history': [], 'preparing_model': True, 'open_error': '',
    'priority_generation_task_id': 'old', 'prepare_attempts': 1,
    'current_mosaic_status': {'state': 'running', 'signature': '__pending__', 'message': 'Opening…'},
}
state.queues['q1'] = queue
record = TaskRecord('old', 'Open-model index: demo')
record.status = 'done'
record.result = {'model': 'demo', 'indexed': 7}
state.tasks['old'] = record
calls = []
def restart(mode, model, drives, queue_id=''):
    calls.append((mode, model, set(drives), queue_id))
    return 'new-task'
state.prioritize_model_mosaics = restart
state._sync_pending_model_task('q1', queue)
assert calls == [('original', 'demo', {'E'}, 'q1')], calls
assert queue['prepare_attempts'] == 2
assert queue['priority_generation_task_id'] == 'new-task'
assert 'Recovering model-open indexing' in queue['current_mosaic_status']['message']

# A legitimately running task must not be duplicated; its real progress should
# replace the vague permanent "locating first chunk" message on the phone.
state = fresh_state()
queue = {
    'id': 'q1', 'mode': 'original', 'model': 'demo', 'drives': ['E'],
    'chunks': [], 'history': [], 'preparing_model': True, 'open_error': '',
    'priority_generation_task_id': 'running', 'prepare_attempts': 1,
    'current_mosaic_status': {'state': 'running', 'signature': '__pending__', 'message': 'Opening…'},
}
state.queues['q1'] = queue
record = TaskRecord('running', 'Open-model index: demo')
record.status = 'running'
record.progress = 'Scanning recording metadata 17/42…'
state.tasks['running'] = record
state.prioritize_model_mosaics = lambda *a, **k: (_ for _ in ()).throw(AssertionError('must not restart a running task'))
state._sync_pending_model_task('q1', queue)
assert queue['current_mosaic_status']['message'] == 'Scanning recording metadata 17/42…'

# A completed task that truly found zero chunks is a real terminal state, not an
# orphan race to retry forever.
state = fresh_state()
queue = {
    'id': 'q1', 'mode': 'original', 'model': 'empty', 'drives': ['E'],
    'chunks': [], 'history': [], 'preparing_model': True, 'open_error': '',
    'priority_generation_task_id': 'empty-task', 'prepare_attempts': 1,
    'current_mosaic_status': {'state': 'running', 'signature': '__pending__', 'message': 'Opening…'},
}
state.queues['q1'] = queue
record = TaskRecord('empty-task', 'Open-model index: empty')
record.status = 'done'
record.result = {'model': 'empty', 'indexed': 0}
state.tasks['empty-task'] = record
state.prioritize_model_mosaics = lambda *a, **k: (_ for _ in ()).throw(AssertionError('zero chunks must not retry'))
state._sync_pending_model_task('q1', queue)
assert queue['preparing_model'] is False
assert queue['current_mosaic_status']['state'] == 'error'
assert 'No sortable chunks' in queue['current_mosaic_status']['message']

print('PASS: v2.15.3 pending model-open recovery/self-healing')
