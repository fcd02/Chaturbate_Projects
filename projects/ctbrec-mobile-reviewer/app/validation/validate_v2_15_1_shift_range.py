from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / 'static' / 'app.js').read_text(encoding='utf-8')
INDEX = (ROOT / 'static' / 'index.html').read_text(encoding='utf-8')
SW = (ROOT / 'static' / 'service-worker.js').read_text(encoding='utf-8')
SERVER = (ROOT / 'ctbrec_mobile_server.py').read_text(encoding='utf-8')


def require(token, where=APP):
    assert token in where, f'missing: {token}'

# New state is UI-local only.
require('lastFileSelectionIndex: null')
require('lastFrameSelectionIndex: null')
require('function inclusiveRangeBetween(ordered, anchor, target)')

# Real click events must reach the selection functions so shiftKey is available.
require('rememberPointerShift(hit, event)')
require('hit.onclick = (event) => frameMode ? selectFrame(tile.frame_index, event) : selectFile(tile.file_index, event);')
require('selectFile(file.index, event);')

# Plain clicks remain toggle-based; Shift clicks mirror the anchor state so ranges can select or clear.
require("const useRange = Boolean(shiftRangeRequested(event) && state.lastFileSelectionIndex !== null);")
require("const useRange = Boolean(shiftRangeRequested(event) && state.lastFrameSelectionIndex !== null);")
require('if (state.keep.has(index)) state.keep.delete(index); else state.keep.add(index);')
require("state.decisions[key] = currentDecision === state.decisionMode ? 'Leave for review' : state.decisionMode;")

# Range selection applies one anchor-derived state across the inclusive range.
require('for (const fileIndex of changedFiles) setFileSelected(fileIndex, shouldSelect, state.decisionMode);')
require('for (const index of changedFrames) setFrameSelected(index, shouldSelect, state.decisionMode);')
require('if (selected) state.keep.add(index); else state.keep.delete(index);')
require("state.decisions[String(index)] = selected ? decision : 'Leave for review';")

# Anchor becomes the newest click and resets on chunk/model selection reset.
require('state.lastFileSelectionIndex = index;')
require('state.lastFrameSelectionIndex = frameIndex;')
require('state.lastFileSelectionIndex = null;')
require('state.lastFrameSelectionIndex = null;')

# Version/cache bump.
assert 'CTBRecMobile/2.15.10' in SERVER
assert '?v=21510' in INDEX
assert 'ctbrec-shell-v21510' in SW

print('PASS: v2.15.1 Shift-click range selection invariants')
