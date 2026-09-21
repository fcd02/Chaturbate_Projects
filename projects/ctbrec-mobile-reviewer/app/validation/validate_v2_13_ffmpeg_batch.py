import os, sys, tempfile, threading, subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import ctbrec_mosaic_sort_lite as orig
import ctbrec_review_folder_sort_lite as review

with tempfile.TemporaryDirectory() as td0:
    td=Path(td0)
    src=td/'src.mp4'
    subprocess.run(['/usr/bin/ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc=size=320x240:rate=10','-t','5','-pix_fmt','yuv420p','-y',str(src)],check=True)
    count=td/'count.txt'
    wrapper=td/'ffmpeg-wrapper.sh'
    wrapper.write_text(f'''#!/bin/sh\necho 1 >> "{count}"\nexec /usr/bin/ffmpeg "$@"\n''')
    wrapper.chmod(0o755)
    req=[(src,i,td/f'o{i}.jpg') for i in range(4)]
    got=orig.extract_frames_batch(wrapper,req,240,threading.Event(),[None],45)
    calls1=len(count.read_text().splitlines()) if count.exists() else 0
    assert len(got)==4, (len(got),got)
    assert calls1==1, calls1
    count.unlink()
    req2=[(src,float(i),td/f'r{i}.jpg') for i in range(4)]
    got2=review.extract_review_frames_batch(wrapper,req2,240,45)
    calls2=len(count.read_text().splitlines()) if count.exists() else 0
    assert len(got2)==4, (len(got2),got2)
    assert calls2==1, calls2
    print('PASS Original 4 frames => 1 ffmpeg process')
    print('PASS Review 4 frames => 1 ffmpeg process')
