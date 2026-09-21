#!/usr/bin/env python3
"""v2.15.4 regression: visible queue polls keep active-model generation alive."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SERVER = (ROOT / 'ctbrec_mobile_server.py').read_text(encoding='utf-8')
INDEX = (ROOT / 'static' / 'index.html').read_text(encoding='utf-8')
SW = (ROOT / 'static' / 'service-worker.js').read_text(encoding='utf-8')

assert 'CTBRecMobile/2.15.10' in SERVER
assert '?v=21510' in INDEX
assert 'ctbrec-shell-v21510' in SW
assert 'def _touch_sort_queue_from_poll' in SERVER
assert 'queue_data["phone_open"] = False' in SERVER
assert 'queue_data["phone_open"] = True' in SERVER
assert SERVER.count('poll_active = self._touch_sort_queue_from_poll(queue_id)') >= 2

import sys
sys.path.insert(0, str(ROOT))
from ctbrec_mobile_server import MobileReviewerState  # noqa: E402


def fresh_state():
    state = object.__new__(MobileReviewerState)
    state.lock = threading.RLock()
    state.queues = {}
    state.tasks = {}
    state.priority_generation_targets = {}
    state.priority_generation_tasks = {}
    state.active_sort_queue_id = ''
    state.sort_session_active_until = 0.0
    state.action_wakeup = threading.Event()
    return state

# A queue the phone explicitly opened may reclaim/renew an expired timer lease
# from a successful queue-specific poll. This is the iOS timer-throttling case.
state = fresh_state()
q1 = {'id': 'q1', 'phone_open': True, 'prefetch_cancel_requested': True, 'prefetch_status': {}}
state.queues['q1'] = q1
before = time.time()
assert state._touch_sort_queue_from_poll('q1') is True
assert state.active_sort_queue_id == 'q1'
assert state.sort_session_active_until >= before + 40
assert q1['prefetch_cancel_requested'] is False

# Explicitly leaving the review view is authoritative. A stale/in-flight poll
# after that must NOT resurrect the old queue.
state.mark_sort_session_inactive()
assert q1['phone_open'] is False
assert q1['prefetch_cancel_requested'] is True
assert state.active_sort_queue_id == ''
assert state._touch_sort_queue_from_poll('q1') is False
assert state.active_sort_queue_id == ''

# Even if the timer lease already expired and cleared active_sort_queue_id before
# the explicit leave arrives, the durable phone_open flag lets inactive close the
# abandoned queue so a late request cannot resurrect it.
state = fresh_state()
state.queues['q1'] = {'id': 'q1', 'phone_open': True, 'prefetch_cancel_requested': False, 'prefetch_status': {}}
state.active_sort_queue_id = ''
state.sort_session_active_until = 0.0
state.mark_sort_session_inactive()
assert state.queues['q1']['phone_open'] is False
assert state.queues['q1']['prefetch_cancel_requested'] is True
assert state._touch_sort_queue_from_poll('q1') is False

# Switching queues explicitly closes the old queue and opens only the new one.
state = fresh_state()
state.queues['q1'] = {'id': 'q1', 'phone_open': False, 'prefetch_cancel_requested': False, 'prefetch_status': {}}
state.queues['q2'] = {'id': 'q2', 'phone_open': False, 'prefetch_cancel_requested': False, 'prefetch_status': {}}
state.mark_sort_session_active(queue_id='q1')
assert state.queues['q1']['phone_open'] is True
state.mark_sort_session_active(queue_id='q2')
assert state.queues['q1']['phone_open'] is False
assert state.queues['q1']['prefetch_cancel_requested'] is True
assert state.queues['q2']['phone_open'] is True
assert state.active_sort_queue_id == 'q2'
assert state._touch_sort_queue_from_poll('q1') is False

# Reproduce the screenshot state: the phone is still polling q1, but the timer
# lease expired and both current-mosaic + look-ahead statuses were cancelled as
# "no longer open". A legitimate status poll must renew the lease and restart
# both missing work items rather than freezing the cancelled message.
state = fresh_state()
chunk = SimpleNamespace(signature='chunk-14', mosaics=[])
queue = {
    'id': 'q1', 'mode': 'original', 'model': 'demo', 'phone_open': True,
    'chunks': [chunk], 'history': [], 'initial_count': 18, 'preparing_model': False,
    'current_mosaic_status': {
        'state': 'cancelled', 'signature': 'chunk-14',
        'message': 'Stopped mosaic generation because this model is no longer open on the phone.',
    },
    'prefetch_cancel_requested': True,
    'prefetch_status': {
        'state': 'cancelled', 'ready': 0, 'target': 3,
        'message': 'Stopped because this model is no longer open.',
    },
}
state.queues['q1'] = queue
state._sync_pending_model_task = lambda *a, **k: None
state.recu_markers_for_chunk = lambda *a, **k: {'status': 'disabled', 'error': '', 'segments': {}, 'unmatched': []}
state.prefetch_status = lambda q: dict(q.get('prefetch_status', {}))
state.action_queue_summary = lambda: {}
state.library_status_payload = lambda: {}
state.nsfw_status_payload = lambda: {}
state._scheduler_settings = lambda: {}
restarted = []
refilled = []
def restart_current(queue_id, force=False):
    restarted.append((queue_id, force))
    queue['current_mosaic_status'] = {'state': 'queued', 'signature': 'chunk-14', 'message': 'Queued again.'}
def restart_prefetch(queue_id, ignore_idle=False):
    refilled.append((queue_id, ignore_idle))
    queue['prefetch_status'] = {'state': 'running', 'ready': 0, 'target': 3, 'message': 'Refilling again.'}
state.request_interactive_mosaic = restart_current
state.schedule_prefetch = restart_prefetch
payload = state.queue_status_payload('q1')
assert state.active_sort_queue_id == 'q1'
assert restarted == [('q1', False)], restarted
assert refilled == [('q1', False)], refilled
assert payload['mosaic_status']['state'] == 'queued'
assert payload['prefetch']['state'] == 'running'

# Once an explicit leave closed the queue, the same status code may report old
# state but must not restart generation.
state = fresh_state()
queue = {
    'id': 'q1', 'mode': 'original', 'model': 'demo', 'phone_open': False,
    'chunks': [chunk], 'history': [], 'initial_count': 18, 'preparing_model': False,
    'current_mosaic_status': {
        'state': 'cancelled', 'signature': 'chunk-14',
        'message': 'Stopped mosaic generation because this model is no longer open on the phone.',
    },
    'prefetch_cancel_requested': True,
    'prefetch_status': {'state': 'cancelled', 'message': 'Stopped because this model is no longer open.'},
}
state.queues['q1'] = queue
state._sync_pending_model_task = lambda *a, **k: None
state.recu_markers_for_chunk = lambda *a, **k: {'status': 'disabled', 'error': '', 'segments': {}, 'unmatched': []}
state.prefetch_status = lambda q: dict(q.get('prefetch_status', {}))
state.action_queue_summary = lambda: {}
state.library_status_payload = lambda: {}
state.nsfw_status_payload = lambda: {}
state._scheduler_settings = lambda: {}
state.request_interactive_mosaic = lambda *a, **k: (_ for _ in ()).throw(AssertionError('closed queue must not restart'))
state.schedule_prefetch = lambda *a, **k: (_ for _ in ()).throw(AssertionError('closed queue must not refill'))
state.queue_status_payload('q1')
assert state.active_sort_queue_id == ''

print('PASS: v2.15.4 visible-queue lease renewal and false-cancellation recovery')
