"""Browser/PWA regression suite updated for v2.13.3.

The key regression reproduced here is the real-phone symptom from v2.13.0:
bootstrap returns network-level `Failed to fetch`, yet catalog is reachable.
The model list must still load and the PIN view must remain hidden.
"""
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1] / 'static'
html=(ROOT/'index.html').read_text(encoding='utf-8')
for ver in ('2133','2131','2130'):
    html=html.replace(f'<script src="/static/app.js?v={ver}"></script>','').replace(f'<script src="/static/rapid.js?v={ver}"></script>','')
    html=html.replace(f'<link rel="stylesheet" href="/static/styles.css?v={ver}">','')
html=html.replace('<link rel="manifest" href="/static/manifest.webmanifest">','')
app=(ROOT/'app.js').read_text(encoding='utf-8'); rapid=(ROOT/'rapid.js').read_text(encoding='utf-8')
mock=r'''
(() => {
  let bootstrapCount = 0;
  let originalCatalogCount = 0;
  window.catalogSignalWasPassed = false;
  const makeModels = (prefix,n) => Array.from({length:n},(_,i)=>({name:`${prefix}_${String(i).padStart(4,'0')}`,bytes:10000000-i,size:'9.5 MB',folder_count:1,drives:['E'],ready_chunks:1}));
  const response = (obj,status=200) => new Response(JSON.stringify(obj), {status,headers:{'Content-Type':'application/json'}});
  const wait = (ms, signal) => new Promise((resolve,reject)=>{
    const t=setTimeout(resolve,ms);
    if (signal) signal.addEventListener('abort',()=>{clearTimeout(t); reject(new DOMException('Aborted','AbortError'));},{once:true});
  });
  window.fetch = async (input, options={}) => {
    const path=String(input); const url=new URL(path,'http://mock.local'); const p=url.pathname; const q=url.searchParams; const signal=options.signal;
    if (p==='/api/auth') return response({authenticated:true,device_token:'test-token',paired_token_supported:true});
    if (p==='/api/auth/recover') return response({ok:true,authenticated:true});
    if (p==='/api/bootstrap') {
      bootstrapCount++;
      // Reproduce the user-visible v2.13.0 failure exactly: transport-level fetch
      // failure, not an HTTP error. Catalog must not depend on this succeeding.
      if (bootstrapCount <= 4) throw new TypeError('Failed to fetch');
      return response({drives:['E'],catalog_status:{status:'ready',progress:'mock ready',updated_at:'now'},library_background:{},non_nsfw_background:{},background_scheduler:{},action_queue:{}});
    }
    if (p==='/api/catalog') {
      if (options.signal) window.catalogSignalWasPassed = true;
      const mode=q.get('mode')||'original', filt=q.get('filter')||'';
      if (mode==='review') { await wait(450,signal); return response({models:makeModels('REVIEW',80),status:{progress:'review',updated_at:'now'}}); }
      if (filt==='easy') { await wait(30,signal); return response({models:makeModels('EASY',4100),status:{progress:'easy',updated_at:'now'}}); }
      originalCatalogCount++;
      if (originalCatalogCount === 1) throw new TypeError('Failed to fetch');
      await wait(40,signal); return response({models:makeModels('ORIGINAL',200),status:{progress:'original',updated_at:'now'}});
    }
    if (p==='/api/background-status') return response({catalog_status:{updated_at:'now'},library_background:{},non_nsfw_background:{},background_scheduler:{},action_queue:{}});
    if (p==='/api/work-status') return response({catalog:{},library:{},nsfw:{},action_jobs:[],tasks:[]});
    if (p==='/api/settings') return response({speed_mode:{offline_pack_chunks:12,offline_pack_max_mb:750}});
    if (p==='/api/sorting/heartbeat') return response({ok:true});
    return response({ok:true});
  };
  try { Object.defineProperty(navigator,'serviceWorker',{value:undefined,configurable:true}); } catch (_) {}
})();
'''
results=[]
with sync_playwright() as pw:
    browser=pw.chromium.launch(headless=True, executable_path='/usr/bin/chromium', args=['--no-sandbox'])
    page=browser.new_page(viewport={'width':390,'height':844})
    page.set_content(html, wait_until='domcontentloaded')
    page.add_script_tag(content=mock)
    page.add_script_tag(content=app)
    page.add_script_tag(content=rapid)

    # Even though bootstrap repeatedly throws TypeError("Failed to fetch") and
    # the first catalog request also fails, internal GET retry should populate models.
    page.wait_for_function("document.querySelector('#model-list .model-name')?.textContent.startsWith('ORIGINAL_')", timeout=9000)
    login_hidden=page.locator('#login-view').evaluate("el=>el.classList.contains('hidden')")
    models_visible=not page.locator('#models-view').evaluate("el=>el.classList.contains('hidden')")
    results.append(('bootstrap transport failure does not blank/block model list', login_hidden and models_visible))
    results.append(('catalog transport retry recovers without PIN', page.locator('#model-list .model-card').count() > 0))
    results.append(('catalog fetch uses no AbortController signal', page.evaluate('window.catalogSignalWasPassed === false')))

    # Old race: Review response is slow; user immediately switches to EZ Sort.
    page.click('#mode-picker button[data-mode="review"]')
    page.wait_for_timeout(40)
    page.click('#mode-picker button[data-mode="easy"]')
    page.wait_for_function("document.querySelector('#model-list .model-name')?.textContent.startsWith('EASY_')",timeout=5000)
    page.wait_for_timeout(700)
    first=page.locator('#model-list .model-name').first.text_content()
    count_text=page.locator('#model-count').text_content()
    dom_cards=page.locator('#model-list .model-card').count()
    results.append(('stale slow tab response cannot overwrite current tab', first.startswith('EASY_')))
    results.append(('4100-model tab reports complete logical count', '4,100' in count_text))
    results.append(('4100-model tab progressively renders bounded DOM', dom_cards <= 240))

    # No global bearer header: the fetch wrapper itself should not add one.
    results.append(('dedicated paired-session recovery function present', page.evaluate("typeof recoverPairedSession === 'function'")))

    # Sorting tap path must update only touched controls, not rebuild mosaic/file DOM.
    same_nodes=page.evaluate("""() => {
      state.current={done:false,mode:'review',model:'SORT_TEST',remaining:1,initial_count:1,can_back:false,recu:{status:'disabled',segments:{}},prefetch:{},mosaic_status:{state:'ready'},action_queue:{},library_background:{},non_nsfw_background:{},background_scheduler:{},chunk:{signature:'sig',size:'1 MB',file_count:1,files:[{index:0,number:1,name:'x.mp4',size:'1 MB',bytes:100,duration_seconds:30,kinks:[]}],frame_cut_available:true,parts:[{url:'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAQABAAACAUwAOw==',tiles:[{file_index:0,number:1,frame_index:0,chunk_seconds:0,interval_end_seconds:10,left:0,top:0,width:33,height:100},{file_index:0,number:1,frame_index:1,chunk_seconds:10,interval_end_seconds:20,left:33,top:0,width:33,height:100},{file_index:0,number:1,frame_index:2,chunk_seconds:20,interval_end_seconds:30,left:66,top:0,width:34,height:100}]}]}};
      state.frameCutMode=true; state.frameDecisions={}; state.decisionMode='Cumshots'; state.frameTilesSignature=''; state.frameTilesCache=[]; state.frameTilesByFile=new Map(); state.frameTilesByIndex=new Map();
      renderMosaics(); renderFiles(); renderSummary();
      const image=document.querySelector('#mosaic-list img'); const row=document.querySelector('#file-list .file-row'); const other=document.querySelector('#mosaic-list [data-frame-index="1"]');
      selectFrame(0);
      return image===document.querySelector('#mosaic-list img') && row===document.querySelector('#file-list .file-row') && other===document.querySelector('#mosaic-list [data-frame-index="1"]');
    }""")
    results.append(('frame selection updates in place without rebuilding mosaic/file DOM', bool(same_nodes)))
    browser.close()

for name,ok in results: print(('PASS' if ok else 'FAIL'),name)
if not all(ok for _,ok in results): raise SystemExit(1)
