"""Verify this application's identity before configuring an external entry point."""
import argparse
import json
import time
import urllib.request

p=argparse.ArgumentParser()
p.add_argument('--url',default='http://127.0.0.1:18080/healthz')
args=p.parse_args()
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
for attempt in range(10):
    try:
        with opener.open(args.url,timeout=2) as response:
            value=json.loads(response.read(2048))
            if response.status==200 and value.get('application')=='vps-watch':
                print('VPS Watch 网页身份检查通过。')
                raise SystemExit(0)
    except Exception:
        pass
    time.sleep(0.5)
raise SystemExit('网页身份检查失败：请检查 vps-watch-web 日志及 18080 端口占用。')
