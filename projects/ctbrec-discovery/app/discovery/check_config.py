from __future__ import annotations
import argparse, json, socket
from pathlib import Path
from urllib.parse import urlparse
from .config import load_config


def resolve(root: Path, raw: str) -> Path | None:
    if not raw: return None
    p=Path(raw); return p if p.is_absolute() else root/p


def tcp_status(url: str, timeout: float=1.0):
    try:
        u=urlparse(url); host=u.hostname or '127.0.0.1'; port=u.port or (443 if u.scheme=='https' else 80)
        with socket.create_connection((host,port),timeout=timeout): pass
        return f'OK — TCP {host}:{port} reachable'
    except Exception as exc:
        return f'OFFLINE/UNREACHABLE — {type(exc).__name__}: {exc}'


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--config',default='discovery_config.json'); ns=ap.parse_args()
    root=Path(__file__).resolve().parent.parent; path=root/ns.config
    try: cfg=load_config(path)
    except Exception as exc:
        print(f'CONFIG ERROR: {type(exc).__name__}: {exc}'); raise SystemExit(2)
    print('CTBRec Discovery configuration check\n'+'='*42)
    print(f'Config: {path}')
    cat=resolve(root,str(cfg.get('mobile_catalog_cache','')))
    print(f'Mobile catalog: {cat if cat else "NOT CONFIGURED"}')
    print(f'  {"OK" if cat and cat.is_file() else "MISSING"}')
    recu=((cfg.get('collectors') or {}).get('recu_local_archives') or {})
    print(f'Recu local archives: {"enabled" if recu.get("enabled") else "disabled"}')
    for raw in recu.get('paths') or []:
        p=resolve(root,str(raw)); print(f'  {"OK" if p and p.is_file() else "MISSING"}: {p}')
    print(f'Mobile Reviewer: {tcp_status(str(cfg.get("reviewer_base_url","http://127.0.0.1:8787")))}')
    print(f'Live Control: {tcp_status(str(cfg.get("live_control_base_url","http://127.0.0.1:8792")))}')
    direct=((cfg.get('collectors') or {}).get('chaturbate_affiliate') or {})
    print(f'Direct affiliate fallback: {"ENABLED" if direct.get("enabled") else "disabled"} (recommended disabled while testing v0.4.1 local integrations)')
    filt=cfg.get('recommendation_filters') or {}
    print('Recommendation filters: '+json.dumps(filt,sort_keys=True))
    print('\nThis checker performs no Chaturbate or Recu web scraping and does not run collectors.')

if __name__=='__main__': main()
