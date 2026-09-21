from pathlib import Path
import re

APP = Path(__file__).resolve().parents[1] / "static" / "app.js"
text = APP.read_text(encoding="utf-8")

assert "function scrollReviewToTop()" in text, "missing scroll helper"
assert "const previousSignature = String(state.current?.chunk?.signature || '')" in text
assert "const chunkChanged = Boolean(nextSignature && nextSignature !== previousSignature)" in text

match = re.search(r"async function loadCurrent\(resetSelections = true\) \{(.*?)\n\}\n\nasync function refreshCurrentMetadata", text, re.S)
assert match, "loadCurrent block not found"
body = match.group(1)
assert "renderCurrent();\n  if (chunkChanged) scrollReviewToTop();" in body, "new chunk does not reset scroll"

refresh = re.search(r"async function refreshCurrentMetadata\(\) \{(.*?)\n\}\n", text, re.S)
assert refresh, "refreshCurrentMetadata block not found"
assert "scrollReviewToTop()" not in refresh.group(1), "same-chunk background refresh must not bounce scroll"

print("PASS v2.14.1 mosaic advance scroll-reset regression")
