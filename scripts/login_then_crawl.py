# -*- coding: utf-8 -*-
"""
一条龙：开浏览器 → 等登录 → 导出 Cookie → 体检接口 → 自动开始慢速采集
================================================================================
给使用者的操作只有一步：**在弹出的浏览器窗口里完成登录（建议扫码）**。
之后全部自动完成。

为什么必须写成一个脚本、放在一个持续运行的任务里：
  本机沙箱会在命令结束时回收浏览器进程（DETACHED_PROCESS 也不行）。
  所以「开窗口 → 等登录 → 导出 Cookie」必须在同一个进程里完成，
  否则窗口会在你还没登录完就消失。

为什么用全新 profile：
  如果复用旧 profile，浏览器打开时就是"已登录旧账号"的状态，
  新账号无法登录。所以这里用 .browser-profile2（干净）。

安全设计：
  - 登录期间只用 CDP 读 Cookie（纯读取，不会顶掉你正在操作的页面）
  - 导出后先体检搜索接口（1 次请求），通了才开始采集
  - 采集一律用交替模式 + 慢速 + 连续搜索上限，见 SAFE_CRAWL_ARGS
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import websocket  # noqa: E402

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE_PREFIX = r'C:\Users\lenovo\.workbuddy\binaries\node\versions\22.22.2-3'
AB = os.path.join(NODE_PREFIX, 'agent-browser.cmd')
SESSION = 'weibo'
COOKIE_OUT = os.path.join(ROOT, 'config', 'cookie.txt')
PROFILE = os.path.join(ROOT, '.browser-profile2')      # 全新 profile

UA_M = ('Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) '
        'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1')
LOGIN_MARKERS = ('ALF', 'SUHB', 'SSOLoginState')
SEARCH_URL = ('https://m.weibo.cn/api/container/getIndex?containerid=100103type%3D1'
              '%26q%3D%E6%95%99%E8%82%B2&page_type=searchall')

#: 采集参数 —— 全部按安全铁律设定，不要在这里调快
SAFE_CRAWL_ARGS = [
    '--mode', 'hot-events',
    '--keyword-file', 'config/domain_keywords.txt',
    '--interleave',                      # 必须：交替模式
    '--min-delay', '15', '--max-delay', '30',
    '--max-consecutive-search', '8',     # 连续搜索上限
    '--max-extra-keywords', '25',
    '--max-keywords', '15',
    '--max-posts', '1',
    '--min-comments', '5',
    '--search-pages', '1',
    '--skip-crawled',
    '--max-hashtags', '4',
    '--max-comment-pages', '2',
    '--max-requests', '200',
    '--max-requests-per-day', '2000',
    '--cookie-file', 'config/cookie.txt',
]


def env_with_node():
    env = dict(os.environ)
    env['PATH'] = NODE_PREFIX + os.pathsep + env.get('PATH', '')
    env['AGENT_BROWSER_HEADED'] = 'true'
    return env


def ab(*args, timeout=120):
    p = subprocess.run([AB, '--session', SESSION, *args], capture_output=True,
                       text=True, timeout=timeout, errors='replace',
                       env=env_with_node(), cwd=ROOT, shell=False)
    return ((p.stdout or '') + (p.stderr or '')).strip()


def kill_stale():
    subprocess.run(['taskkill', '/F', '/IM', 'chrome.exe'],
                   capture_output=True, timeout=60)
    d = r'C:\Users\lenovo\.agent-browser'
    for n in ('default.pid', 'default.port', 'default.engine',
              'default.version', 'default.stream'):
        p = os.path.join(d, n)
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


def get_cdp_ws():
    m = re.search(r'(ws://127\.0\.0\.1:\d+/devtools/browser/\S+)', ab('get', 'cdp-url'))
    return m.group(1) if m else None


def cdp_cookies(ws_url):
    """通过 CDP 读全部 Cookie。纯读取，不导航，不会影响你正在操作的页面。"""
    ws = websocket.create_connection(ws_url, timeout=20,
                                     origin='http://127.0.0.1',
                                     suppress_origin=True)
    try:
        ws.send(json.dumps({'id': 1, 'method': 'Storage.getCookies'}))
        end = time.time() + 20
        while time.time() < end:
            msg = json.loads(ws.recv())
            if msg.get('id') == 1:
                return [
                    {'name': c.get('name'), 'value': c.get('value'),
                     'domain': c.get('domain'), 'path': c.get('path')}
                    for c in ((msg.get('result') or {}).get('cookies') or [])]
        return []
    finally:
        try:
            ws.close()
        except Exception:                          # noqa: BLE001
            pass


def build_header(cookies):
    """按浏览器规则拼 Cookie 头，移动端接口优先用 .weibo.cn 域。"""
    def rank(dom):
        if dom in ('.weibo.cn', 'm.weibo.cn'):
            return 0
        if dom in ('.weibo.com', 'weibo.com'):
            return 1
        return 2 if 'weibo' in (dom or '') else 3
    keep, seen = [], set()
    for c in sorted(cookies, key=lambda c: rank(c.get('domain') or '')):
        dom = c.get('domain') or ''
        if not any(k in dom for k in ('weibo', 'sina')):
            continue
        name = c.get('name')
        if not name or name in seen:
            continue
        try:
            (c.get('value') or '').encode('ascii')
        except UnicodeEncodeError:
            continue
        seen.add(name)
        keep.append(f"{name}={c.get('value', '')}")
    return '; '.join(keep)


def http_json(url, headers, timeout=25):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode('utf-8', 'replace')
        except Exception:                          # noqa: BLE001
            return e.code, ''
    except Exception as exc:                       # noqa: BLE001
        return None, f'{type(exc).__name__}: {exc}'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--timeout', type=int, default=1800,
                    help='等待登录的最长秒数，默认 1800（30 分钟）')
    ap.add_argument('--poll', type=int, default=6)
    ap.add_argument('--no-crawl', action='store_true',
                    help='只到导出 Cookie 为止，不自动开始采集')
    args = ap.parse_args()

    print('=' * 74)
    print('微博采集一条龙：开浏览器 → 等登录 → 导出Cookie → 体检 → 自动采集')
    print('=' * 74, flush=True)
    print(f'新账号专用 profile: {PROFILE}')
    print('（用全新 profile 是为了让新账号能正常登录；旧 profile 里是旧账号）')
    print(flush=True)

    print('[1/5] 清理残留浏览器与旧会话状态', flush=True)
    kill_stale()
    time.sleep(2)

    print('[2/5] 打开浏览器窗口（有头模式，你应该能看到窗口）', flush=True)
    subprocess.Popen([AB, '--session', SESSION, 'open',
                      'https://weibo.com/login.php'],
                     env=env_with_node(), cwd=ROOT, shell=False,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
    time.sleep(14)

    ws_url = get_cdp_ws()
    if not ws_url:
        print('[失败] 拿不到 CDP 地址，浏览器可能没起来。', flush=True)
        return 1
    print(f'      CDP 就绪: {ws_url[:64]}…', flush=True)

    base = cdp_cookies(ws_url)
    names0 = {c.get('name') for c in base}
    print(f'      当前 Cookie {len(base)} 条，登录字段 '
          f'{[m for m in LOGIN_MARKERS if m in names0] or "（无，说明是未登录状态，正常）"}',
          flush=True)

    print('\n' + '★' * 74, flush=True)
    print('★  请在弹出的浏览器窗口里完成登录（建议扫码，最安全）', flush=True)
    print('★  登录完成后不用告诉我，脚本会自动检测并继续', flush=True)
    print('★' * 74 + '\n', flush=True)

    t0 = time.time()
    last_log = 0
    header = None
    while time.time() - t0 < args.timeout:
        time.sleep(args.poll)
        elapsed = int(time.time() - t0)
        try:
            cookies = cdp_cookies(ws_url)
        except Exception:                          # noqa: BLE001
            ws_url = get_cdp_ws() or ws_url
            continue
        names = {c.get('name') for c in cookies}
        hit = [m for m in LOGIN_MARKERS if m in names]
        if hit:
            print(f'[{elapsed}s] ✓ 检测到登录字段 {hit}（共 {len(cookies)} 条 Cookie）',
                  flush=True)
            h = build_header(cookies)
            if h:
                header = h
                break
        if elapsed - last_log >= 60:
            last_log = elapsed
            print(f'[{elapsed}s] 等待登录中… 当前 Cookie {len(cookies)} 条', flush=True)

    if not header:
        print(f'\n✗ 超时（{args.timeout}s）未检测到登录。', flush=True)
        return 2

    print(f'[3/5] 导出 Cookie（{len(header)} 字符）并验证账号', flush=True)
    os.makedirs(os.path.dirname(COOKIE_OUT), exist_ok=True)
    with open(COOKIE_OUT, 'w', encoding='utf-8') as fh:
        fh.write(header)
    print(f'      已写入 {COOKIE_OUT}', flush=True)

    hdr = {'User-Agent': UA_M, 'Referer': 'https://m.weibo.cn/',
           'Accept': 'application/json, text/plain, */*',
           'X-Requested-With': 'XMLHttpRequest', 'MWeibo-Pwa': '1',
           'Cookie': header}
    st, body = http_json('https://m.weibo.cn/api/config', hdr)
    uid = ''
    if st == 200:
        try:
            data = json.loads(body).get('data') or {}
            uid = str(data.get('uid') or '')
            print(f'      账号 uid = {uid}   login = {data.get("login")}', flush=True)
            old_uid = os.environ.get('WEIBO_OLD_UID', '').strip()
            if old_uid and uid == old_uid:
                print('      ⚠ 检测到仍是【上一个账号】的 uid！新账号可能没登录成功，'
                      '或者浏览器复用了旧登录态。', flush=True)
                print('        建议：关掉那个浏览器窗口，重新跑本脚本。', flush=True)
                return 3
        except Exception:                          # noqa: BLE001
            pass
    else:
        print(f'      ⚠ 登录态检查失败 HTTP {st}', flush=True)

    print('[4/5] 体检搜索接口（1 次请求）', flush=True)
    time.sleep(10)
    st2, body2 = http_json(SEARCH_URL, hdr)
    ok_search = False
    if st2 == 200:
        try:
            d = json.loads(body2)
            info = (d.get('data') or {}).get('cardlistInfo') or {}
            if d.get('ok') == 1:
                ok_search = True
                print(f'      ✓ 搜索接口可用！total={info.get("total")}', flush=True)
        except Exception:                          # noqa: BLE001
            pass
    if not ok_search:
        print(f'      ✗ 搜索接口 HTTP {st2}  {body2[:120]}', flush=True)
        print('\n  → 新账号的搜索接口也不通。可能原因：', flush=True)
        print('     1) 网络层仍在限流（等一下再试）', flush=True)
        print('     2) 新账号本身也在限流中', flush=True)
        print('     Cookie 已导出，稍后用 scripts/probe_weibo.py 再体检即可。', flush=True)
        return 4

    if args.no_crawl:
        print('\n[5/5] 已按要求跳过采集。', flush=True)
        return 0

    print('[5/5] 开始慢速交替采集（后台跑，约 1 小时）', flush=True)
    print('      参数：交替模式 / 15~30秒间隔 / 连续搜索≤8次 / 200次请求上限',
          flush=True)
    print('=' * 74, flush=True)
    py = os.path.join(ROOT, '.venv', 'Scripts', 'python.exe')
    if not os.path.exists(py):
        py = sys.executable
    cmd = [py, '-u', os.path.join('scripts', 'event_crawler.py')] + SAFE_CRAWL_ARGS
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env_with_node(),
                            stdin=subprocess.DEVNULL)
    rc = proc.wait()
    print('=' * 74, flush=True)
    print(f'采集结束（退出码 {rc}）', flush=True)
    print('接下来可以导出数据集：', flush=True)
    print('  .venv/Scripts/python.exe scripts/export_dataset.py --no-download --zip',
          flush=True)
    return rc


if __name__ == '__main__':
    sys.exit(main())
