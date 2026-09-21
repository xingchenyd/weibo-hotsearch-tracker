# -*- coding: utf-8 -*-
"""
微博接口体检 —— 只发 2 次请求，用来判断限制是否解除
=====================================================
背景：`container/getIndex`（搜索/话题容器）这个接口族一旦被限，
正文和评论接口往往还是正常的。所以恢复判断要**分接口看**，
不能只看"能不能连上"。

用途：触发风控后间歇性地检查恢复情况。每次只发 2 次请求，
不要频繁跑，也不要因为失败就重试——那正是让限制加重的原因。

用法：
  .venv/Scripts/python.exe scripts/probe_weibo.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from curl_cffi import requests as creq
except ImportError:
    print('需要 curl_cffi')
    sys.exit(1)

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) '
      'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1')
COOKIE_FILE = os.path.join(ROOT, 'config', 'cookie.txt')

#: 取一个已抓过的 mid 做正文测试；没有就用已知的老 mid
SAMPLE_MID = '5344998059147763'


def headers(cookie=''):
    h = {'User-Agent': UA, 'Referer': 'https://m.weibo.cn/',
         'Accept': 'application/json, text/plain, */*',
         'X-Requested-With': 'XMLHttpRequest', 'MWeibo-Pwa': '1'}
    if cookie:
        h['Cookie'] = cookie
    return h


def main():
    cookie = ''
    if os.path.exists(COOKIE_FILE):
        cookie = open(COOKIE_FILE, encoding='utf-8').read().strip()

    print('=' * 62)
    print('微博接口体检（本脚本只发 2 次请求）')
    print('=' * 62)

    results = {}

    # 1) 搜索/容器接口 —— 被限时最先挂的就是它
    url = ('https://m.weibo.cn/api/container/getIndex?containerid=100103type%3D1'
           '%26q%3D%E6%95%99%E8%82%B2&page_type=searchall')
    try:
        r = creq.get(url, headers=headers(cookie), impersonate='chrome', timeout=25)
        if r.status_code == 200:
            d = r.json()
            info = (d.get('data') or {}).get('cardlistInfo') or {}
            print(f'  [1] 搜索接口 container : ✓ 可用  '
                  f'total={info.get("total")}  ok={d.get("ok")}')
            results['search'] = True
        else:
            try:
                msg = r.json().get('msg')
            except Exception:                      # noqa: BLE001
                msg = r.text[:60]
            print(f'  [1] 搜索接口 container : ✗ HTTP {r.status_code}  {msg}')
            results['search'] = False
    except Exception as exc:                       # noqa: BLE001
        print(f'  [1] 搜索接口 container : ✗ {type(exc).__name__} {str(exc)[:70]}')
        results['search'] = False

    # 2) 正文接口 —— 通常不受影响
    try:
        r = creq.get(f'https://m.weibo.cn/statuses/show?id={SAMPLE_MID}',
                     headers=headers(cookie), impersonate='chrome', timeout=25)
        ok = r.status_code == 200 and '"ok":1' in r.text
        print(f'  [2] 正文接口 statuses  : {"✓ 可用" if ok else "✗ HTTP " + str(r.status_code)}')
        results['detail'] = ok
    except Exception as exc:                       # noqa: BLE001
        print(f'  [2] 正文接口 statuses  : ✗ {type(exc).__name__} {str(exc)[:70]}')
        results['detail'] = False

    print()
    if results.get('search'):
        print('  → 搜索接口已恢复，可以继续采集（务必用 --interleave 慢速跑）')
        return 0
    if results.get('detail'):
        print('  → 搜索接口仍被限，但正文/评论可用。')
        print('     → 还不能发现新事件（发现依赖搜索）。请继续等待，')
        print('       间隔 1~2 小时再体检一次，不要频繁重试。')
    else:
        print('  → 都不可用，请继续静默等待。')
    return 2


if __name__ == '__main__':
    sys.exit(main())
