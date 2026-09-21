import hashlib, os, subprocess, sys, tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import ctbrec_nsfw_cleanup as nsfw

class Detector:
    def detect_batch(self, paths):
        for p in paths:
            assert Path(p).is_file() and Path(p).stat().st_size > 0
        return [[] for _ in paths]

with tempfile.TemporaryDirectory() as td0:
    td=Path(td0)
    src=td/'src.mp4'
    subprocess.run(['/usr/bin/ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc=size=320x240:rate=8','-t','36','-pix_fmt','yuv420p','-y',str(src)],check=True)
    count=td/'count.txt'
    target_hint='00002.jpg'
    wrapper=td/'ffmpeg-wrapper.sh'
    wrapper.write_text(f'''#!/bin/sh\necho 1 >> "{count}"\n/usr/bin/ffmpeg "$@"\nrc=$?\nn=$(wc -l < "{count}")\nif [ "$n" -eq 1 ]; then\n  for a in "$@"; do\n    case "$a" in *{target_hint}) rm -f "$a";; esac\n  done\nfi\nexit $rc\n''')
    wrapper.chmod(0o755)
    stat=src.stat()
    temp_root=td/'frames'; temp_root.mkdir()
    settings={
        'sample_every_seconds':15,
        'confidence_threshold':0.45,
        'borderline_confidence':0.2,
        'explicit_classes':nsfw.DEFAULT_EXPLICIT_CLASSES,
        'detector_model':'320n',
        'batch_size':24,
        'frame_extract_batch_size':6,
        'frame_extract_timeout_seconds':45,
    }
    result=nsfw._scan_one_file(Detector(), wrapper, src, stat, 36.0, settings, temp_root)
    calls=len(count.read_text().splitlines())
    expected=len(nsfw.sample_times(36.0,15))
    assert result['classification']=='safe', result
    assert result['scanned_samples']==expected, (result,expected)
    assert calls==2, f'expected one batch + one single-frame fallback, got {calls}'
    print(f'PASS NSFW detection: {expected} samples used 1 batch ffmpeg + 1 targeted fallback')

    # Clean test of the cleanup mosaic composer: all four requested frames from
    # one source should use a single ffmpeg launch when no output is missing.
    count.unlink()
    clean_wrapper=td/'ffmpeg-clean.sh'
    clean_wrapper.write_text(f'''#!/bin/sh\necho 1 >> "{count}"\nexec /usr/bin/ffmpeg "$@"\n''')
    clean_wrapper.chmod(0o755)
    video=nsfw.CleanupVideo(path=src,start=nsfw.datetime.now(),size=stat.st_size,mtime=stat.st_mtime,duration=36.0,sample_times=[2.0,10.0,18.0,26.0])
    class Status:
        def write(self,*a,**k): pass
    settings.update({'columns':4,'tile_width':200,'max_tiles_per_image':120,'jpeg_quality':80})
    outs, manifest=nsfw._compose_mosaic('testmodel',[video],td/'model',clean_wrapper,settings,Status())
    calls=len(count.read_text().splitlines())
    assert calls==1, calls
    assert len(outs)==1 and outs[0].is_file() and manifest.is_file()
    print('PASS NSFW cleanup mosaic: 4 tiles => 1 ffmpeg process')
