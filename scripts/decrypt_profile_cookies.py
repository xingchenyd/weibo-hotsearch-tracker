# -*- coding: utf-8 -*-
"""
直接从浏览器 profile 解密微博 Cookie（不需要浏览器在运行）
============================================================
原理：
  Chrome/Edge 的 Cookie 用 AES-256-GCM 加密，密钥本身被 Windows DPAPI 保护。
  加密值前缀是 b'v10'：
      [0:3]   = b'v10'
      [3:15]  = 12 字节 nonce
      [15:]   = 密文 + 16 字节 GCM tag
  密钥来自 profile 目录下 Local State 的 os_crypt.encrypted_key，
  先用 DPAPI 解出，再拿它做 AES-GCM 解密。

为什么走这条路：
  浏览器在本机沙箱里活不过一次工具调用，而 Cookie 只是磁盘上的数据。
  直接读磁盘 + 解密，比维持一个浏览器窗口可靠得多。

关键点（踩过的坑）：
  微博在 .weibo.com 和 .weibo.cn 两个域上各有一份 SUB/SUBP，**值不一样**。
  给 m.weibo.cn 的请求必须用 .weibo.cn 那一份；混用会被判为未登录。
  所以这里按浏览器规则做域匹配，而不是简单按名字去重。

用法：
  python scripts/decrypt_profile_cookies.py            # 解密+验证+写 config/cookie.txt
  python scripts/decrypt_profile_cookies.py --list     # 只列出解密结果
"""
import argparse
import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE = os.path.join(ROOT, '.browser-profile')
OUT = os.path.join(ROOT, 'config', 'cookie.txt')

UA_M = ('Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) '
        'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1')
UA_W = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36')


class DATA_BLOB(ctypes.Structure):
    _fields_ = [('cbData', wt.DWORD), ('pbData', ctypes.POINTER(ctypes.c_char))]


def dpapi_unprotect(data):
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise OSError('CryptUnprotectData 失败')
    out = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return out


def load_key(profile):
    ls_path = os.path.join(profile, 'Local State')
    if not os.path.exists(ls_path):
        raise FileNotFoundError(f'缺少 Local State: {ls_path}')
    state = json.load(open(ls_path, encoding='utf-8'))
    b64 = state.get('os_crypt', {}).get('encrypted_key')
    if not b64:
        raise KeyError('Local State 里没有 os_crypt.encrypted_key')
    raw = base64.b64decode(b64)
    if raw[:5] != b'DPAPI':
        raise ValueError(f'意外的密钥前缀: {raw[:5]!r}')
    return dpapi_unprotect(raw[5:])


def decrypt_value(value, key):
    """解密并剥离域绑定前缀。

    实测（Chrome 153）：v10 解密后的明文 **开头有 32 字节的域绑定前缀**，
    之后才是真正的 Cookie 值。例如 .weibo.cn 的 SUB 解出来是
        [32字节二进制前缀] + '_2A25Hqgx****************************************************************'
    最后那部分才是真正的 SUB（以 `_2A25` 开头）。
    （上面是脱敏示例，真实值请用 config/cookie.txt 里的。）
    同一域名下所有 Cookie 共享同一个前缀，因此这不是解密错误。
    不剥掉前缀的话，得到的值含非 ASCII 字节，既编不进 HTTP 头，
    服务端也会判为未登录。

    另外不能用 errors='replace'：替换成 U+FFFD 后无法还原。
    """
    if not value:
        return ''
    if value[:3] == b'v10':
        nonce, ct = value[3:15], value[15:]
        raw = AESGCM(key).decrypt(nonce, ct, None)
    elif value[:3] == b'v20':
        # App-Bound 加密：密钥由浏览器进程自身权限保护，普通方式解不开
        return None
    else:
        try:
            raw = dpapi_unprotect(value)
        except Exception:                          # noqa: BLE001
            return None

    def _clean(b):
        try:
            s = b.decode('utf-8')
        except UnicodeDecodeError:
            return None
        # 合理值应当可打印，不含控制字符
        if all(ch.isprintable() or ch in ' \t' for ch in s):
            return s
        return None

    # 先按"带 32 字节前缀"处理，再退回整体解码
    if len(raw) > 32:
        stripped = _clean(raw[32:])
        if stripped is not None:
            return stripped
    whole = _clean(raw)
    if whole is not None:
        return whole
    return raw.decode('latin-1')


def read_cookies(profile):
    src = os.path.join(profile, 'Default', 'Network', 'Cookies')
    if not os.path.exists(src):
        src = os.path.join(profile, 'Default', 'Cookies')
    if not os.path.exists(src):
        raise FileNotFoundError('找不到 Cookies 数据库')
    tmp = os.path.join(tempfile.gettempdir(), 'wb_cookies_copy.db')
    shutil.copy2(src, tmp)
    con = sqlite3.connect(tmp)
    rows = con.execute(
        'SELECT host_key, name, encrypted_value, path, is_httponly, '
        'is_secure, expires_utc FROM cookies').fetchall()
    con.close()
    os.remove(tmp)

    key = load_key(profile)
    out, failed = [], 0
    for host, name, enc, path, http_only, secure, exp in rows:
        val = decrypt_value(bytes(enc or b''), key)
        if val is None:
            failed += 1
            continue
        out.append({'domain': host, 'name': name, 'value': val,
                    'path': path or '/', 'httpOnly': bool(http_only),
                    'secure': bool(secure)})
    return out, len(rows), failed


def domain_matches(cookie_domain, host):
    """按浏览器规则判断该 Cookie 是否会发给 host。"""
    d = (cookie_domain or '').lstrip('.')
    return host == d or host.endswith('.' + d)


def build_header(cookies, host):
    """为该 host 拼 Cookie 头，遵循域匹配 + 路径 + 同名取最长路径。"""
    matched = [c for c in cookies if domain_matches(c['domain'], host)]
    matched.sort(key=lambda c: len(c.get('path') or '/'), reverse=True)
    keep, seen = [], set()
    for c in matched:
        if c['name'] in seen:
            continue
        try:
            c['value'].encode('latin-1')
        except UnicodeEncodeError:
            print(f"    [跳过] {c['name']}@{c['domain']} "
                  f"含无法编入 HTTP 头的字符（{len(c['value'])} 字符）")
            continue
        seen.add(c['name'])
        keep.append(f"{c['name']}={c['value']}")
    return '; '.join(keep)


def verify_mobile(header):
    """用 curl_cffi 模拟 Chrome 指纹验证。

    必须模拟指纹：用原生 urllib/requests 时，即使 Cookie 完全正确，
    微博也会返回 passport 跳转（假的"未登录"）。实测 curl_cffi
    impersonate='chrome' 才能通过。
    """
    try:
        from curl_cffi import requests as creq
    except ImportError:
        print('    [跳过] 未安装 curl_cffi，无法验证（pip install curl_cffi）')
        return None

    url = ('https://m.weibo.cn/api/container/getIndex?containerid=100103type%3D1'
           '%26q%3D%E5%A5%BD%E4%BA%BA%E5%A5%BD%E4%BA%8B&page_type=searchall&page=1')
    try:
        r = creq.get(url, headers={
            'Cookie': header, 'Referer': 'https://m.weibo.cn/',
            'Accept': 'application/json, text/plain, */*',
            'X-Requested-With': 'XMLHttpRequest', 'MWeibo-Pwa': '1',
        }, impersonate='chrome', timeout=25)
    except Exception as e:                         # noqa: BLE001
        print(f'    请求失败: {type(e).__name__}: {str(e)[:80]}')
        return False
    body = r.text
    if 'passport' in body or '"ok":-100' in body:
        print(f'    → 未登录（passport 跳转），HTTP {r.status_code}')
        return False
    try:
        d = json.loads(body)
    except json.JSONDecodeError:
        print('    非 JSON:', body[:120].replace('\n', ' '))
        return False
    if d.get('ok') == 1:
        info = (d.get('data') or {}).get('cardlistInfo') or {}
        n = sum(1 for card in ((d.get('data') or {}).get('cards') or [])
                for c in [card] + list(card.get('card_group') or [])
                if c.get('mblog'))
        print(f'    → ✓✓ 可用！ok=1  total={info.get("total")}  本页 {n} 条微博')
        return True
    print('    ok =', d.get('ok'), str(d)[:140])
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--profile', default=PROFILE)
    args = ap.parse_args()

    print(f'profile: {args.profile}')
    cookies, total, failed = read_cookies(args.profile)
    print(f'Cookie 读取: 共 {total} 条，解密成功 {len(cookies)} 条'
          + (f'，失败 {failed} 条（可能 v20 App-Bound 加密）' if failed else ''))

    weibo = [c for c in cookies if 'weibo' in c['domain'] or 'sina' in c['domain']]
    print(f'其中 weibo/sina 相关: {len(weibo)} 条')
    bydom = {}
    for c in weibo:
        bydom.setdefault(c['domain'], []).append(c['name'])
    for dom, names in sorted(bydom.items()):
        print(f'  {dom:24s} {len(names):>2} 条  {sorted(names)}')

    if args.list:
        print('\n--- 解密后的关键字段值（前 16 字符）---')
        for c in sorted(weibo, key=lambda c: (c['domain'], c['name'])):
            if c['name'] in ('SUB', 'SUBP', 'ALF', 'SSOLoginState', 'SCF'):
                print(f"  {c['domain']:22s} {c['name']:16s} {c['value'][:16]}…")
        return 0

    print()
    print('=== 验证：给 m.weibo.cn 发请求（用 .weibo.cn 域那份 Cookie）===')
    h_mobile = build_header(cookies, 'm.weibo.cn')
    print(f'  移动端 Cookie 头长度: {len(h_mobile)}')
    for c in sorted([c for c in cookies if domain_matches(c['domain'], 'm.weibo.cn')],
                    key=lambda c: c['name']):
        print(f"    {c['domain']:22s} {c['name']}")
    ok = verify_mobile(h_mobile)

    if ok:
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        with open(OUT, 'w', encoding='utf-8') as fh:
            fh.write(h_mobile)
        print(f'\n✓ 已写入 {OUT}（{len(h_mobile)} 字符）')
        print('  这是登录凭据：别提交 git、别外传、用完可删。')
        return 0
    print('\n✗ 移动端接口未通过。')
    return 2


if __name__ == '__main__':
    sys.exit(main())
