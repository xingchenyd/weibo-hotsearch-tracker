# -*- coding: utf-8 -*-
"""
微博「事件」采集器：微博正文 + 该微博下的评论（含二级回复）
================================================================
核心思路：一条微博 = 一个事件。采集它的正文，以及评论区的全部评论。

【实测能力边界 2026-09-19】
  无需 Cookie（已验证可用）：
    - 单条微博正文   GET https://m.weibo.cn/statuses/show?id=<mid>
    - 微博评论       GET https://m.weibo.cn/comments/hotflow?id=<mid>&mid=<mid>
                     （返回 total_number / max_id，可翻页；含二级回复字段 comments）
    - 热搜榜词条     GET https://weibo.com/ajax/side/hotSearch
  需要登录（已验证被拦）：
    - 关键词搜索 / 话题搜索 → {"ok":-100, "url":"passport.weibo.com/sso/signin..."}
    - 用户时间线 / 话题容器 → HTTP 432（5 次指数退避无效，非瞬时风控）
    - 各 HTML 页面 → 仅 9.5KB 登录壳，无内嵌 JSON

因此本采集器把「发现微博」和「抓取正文评论」解耦：

  发现层（discover）：
    --mode hot        无需 Cookie。以热搜榜置顶要闻为事件源，每轮 1 条，可长期累积
    --mode mid        无需 Cookie。直接指定微博 mid
    --mode hot-events 需要 Cookie。跟随热搜榜事件：每个热搜词当作关键词去搜索，
                      取回该事件下的多条微博。可用 --categories 限定官方分类
    --mode search     需要 Cookie。按关键词搜索，覆盖明星/时政/好人好事/游戏/科技/金融…
    --mode uid        需要 Cookie。按用户时间线采集（适合官媒账号）

  抓取层（fetch）：正文 + 评论翻页，各模式下完全一致。

Cookie 配置：环境变量 WEIBO_COOKIE，或 --cookie-file 指向一行 cookie 的文本文件。

代理配置（重要）：
  container 系列接口对同一 IP 会持续返回 HTTP 432，换参数/UA/会话 Cookie 均无效，
  属 IP 层面标记。--proxy / --proxy-file 提供代理后，遇 432 会自动轮换下一个 IP。
    --proxy http://user:pass@host:port
    --proxy-file proxies.txt      （一行一个，遇 432 依次轮换）

落盘：
  data/events.db    表 events(正文) / comments(评论) / crawl_log
  data/events.csv   正文宽表
  data/comments.csv 评论宽表

用法：
  python scripts/event_crawler.py --mode mid --mid 5344469907933160
  python scripts/event_crawler.py --mode hot --max-comment-pages 3
  python scripts/event_crawler.py --mode hot-events --max-keywords 15 --max-posts 10
  python scripts/event_crawler.py --mode search --keywords "好人好事,亚运会" --max-posts 20
  python scripts/event_crawler.py --mode uid --uids 2803301701 --max-posts 30
  python scripts/event_crawler.py --mode hot-events --proxy-file config/proxies.txt
"""
import argparse
import csv
import html
import json
import os
import random
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

CST = timezone(timedelta(hours=8))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from safety import SafetyAbort, SafetyGuard  # noqa: E402

UA_POOL = [
    'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) '
    'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1',
    'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) '
    'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1',
    'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36',
]

TAG_RE = re.compile(r'<[^>]+>')
SPACE_RE = re.compile(r'[ \t\u00a0]+')


def clean_text(raw):
    """微博正文/评论含 <a>、<span class="url-icon"><img alt="[心]"> 等标签。

    注意顺序：必须先把 <img alt="[心]"> 还原成 [心]，再剥掉其余标签。
    若先删掉整个 url-icon span，纯表情评论会变成空串而被误丢弃。
    """
    if not raw:
        return ''
    text = re.sub(r'<img[^>]*\balt="([^"]*)"[^>]*/?>', r'\1', raw)
    text = TAG_RE.sub('', text)
    text = html.unescape(text)
    text = SPACE_RE.sub(' ', text)
    return text.strip()


class Blocked(Exception):
    """被风控或需登录。"""


class EmptyResult(Blocked):
    """接口正常返回，但结果是空的。

    例如 {"ok":0,"msg":"这里还没有内容","data":{"cards":[]}}。
    这**不是**风控，绝不能当成封锁信号中止整轮采集——
    踩过的坑：曾把"该关键词没搜到内容"误判为风控，直接中断了整轮事件发现，
    10 个关键词只跑了 2 个。所以单独建一个类型，让它继承 Blocked
    以兼容既有的 except Blocked 分支，同时可被精确区分。
    """


#: ok=0 时，消息里含这些词说明只是"没有结果"而非风控
EMPTY_MARKERS = ('没有内容', '暂无数据', '没有更多', 'no data', 'empty')


def mask_proxy(proxy):
    """打印代理时隐藏账号密码。"""
    if not proxy:
        return '(直连)'
    return re.sub(r'//[^@/]+@', '//***@', proxy)


class Fetcher:
    """带安全护栏与代理池的取数器。

    guard 存在时：每次请求前经护栏记账与限速；命中风控信号立即熔断。
    实测：container 系列接口对同一 IP 会持续返回 432，换参数、换 UA、
    带会话 Cookie 都无效，因此这里在 432 时优先切换代理再重试。
    """

    def __init__(self, cookie='', proxies=None, min_delay=3.0, max_delay=5.5,
                 verbose=True, guard=None):
        self.cookie = cookie
        self.proxies = list(proxies or [])
        self.pi = 0
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.verbose = verbose
        self.guard = guard
        self.throttle_hits = 0

    @property
    def proxy(self):
        return self.proxies[self.pi] if self.proxies else None

    def rotate(self):
        if not self.proxies:
            return False
        self.pi = (self.pi + 1) % len(self.proxies)
        if self.verbose:
            print(f'      ↻ 切换到代理 {mask_proxy(self.proxy)}')
        return True

    def _headers(self, referer=None):
        h = {
            'User-Agent': random.choice(UA_POOL),
            'Referer': referer or 'https://m.weibo.cn/',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'X-Requested-With': 'XMLHttpRequest',
            'MWeibo-Pwa': '1',
        }
        if self.cookie:
            h['Cookie'] = self.cookie
        return h

    def _request(self, url, referer=None):
        """发请求。

        必须用 curl_cffi 模拟 Chrome 的 TLS 指纹：实测用原生 urllib/requests 时，
        即使 Cookie 完全正确，微博也会返回 passport 跳转（假的"未登录"）。
        """
        try:
            from curl_cffi import requests as creq
        except ImportError as exc:
            raise Blocked(
                '缺少 curl_cffi，无法通过微博的 TLS 指纹校验。'
                '请安装：.venv/Scripts/python.exe -m pip install curl_cffi') from exc
        kwargs = {'impersonate': 'chrome', 'timeout': 25, 'allow_redirects': True}
        if self.proxy:
            kwargs['proxies'] = {'http': self.proxy, 'https': self.proxy}
        return creq.get(url, headers=self._headers(referer), **kwargs)

    def get_json(self, url, retries=None, referer=None):
        """取 JSON。

        安全模式下 retries 默认 1 —— 遇风控**不重试**，因为反复重试正是
        加重账号标记的行为。只有显式关闭熔断时才允许退避重试。
        """
        safe = bool(self.guard and self.guard.abort_on_throttle)
        if retries is None:
            retries = 1 if safe else 3
        last = None

        for attempt in range(retries):
            if self.guard:
                self.guard.acquire(url)              # 额度检查 + 限速
            try:
                resp = self._request(url, referer)
            except SafetyAbort:
                raise
            except Exception as exc:                 # noqa: BLE001
                ename = type(exc).__name__
                msg = str(exc)
                # 连接类错误：先判断是本地网络故障还是微博侧封锁。
                # `CONNECT tunnel failed` / 502 多为本地代理抖动，不是封禁。
                conn_like = ('SSLError' in ename or 'SSL_ERROR' in msg
                             or 'Connection closed abruptly' in msg
                             or 'ConnectionError' in ename
                             or 'Connection aborted' in msg
                             or 'CONNECT tunnel failed' in msg)
                if self.guard:
                    self.guard.record(url, f'ERR:{ename}')
                if conn_like:
                    if neutral_site_reachable():
                        # 中立站点通、只有微博不通 → 才判为微博侧封锁
                        if self.guard:
                            self.guard.hard_block(url, f'{ename}: {msg[:70]}')
                        raise Blocked(
                            f'连接失败（仅微博不通）{ename}: {msg[:80]}') from exc
                    # 中立站点也不通 → 本地网络/代理故障，不熔断
                    if self.verbose:
                        print(f'      ⚠ 本地网络/代理异常（中立站点也不通），'
                              f'跳过本条：{msg[:60]}')
                    raise Blocked(
                        f'本地网络/代理故障，非微博封禁：{msg[:80]}') from exc
                last = exc
                if attempt < retries - 1:
                    time.sleep(10 * (attempt + 1))
                    continue
                raise Blocked(f'请求失败 {ename}: {msg[:90]}') from exc

            status, body = resp.status_code, resp.text

            if self.guard:
                # 响应体里出现 passport 跳转等即视为风控
                self.guard.check_throttle(url, status, body[:2000])
                self.guard.record(url, f'HTTP{status}')

            if status in (403, 429, 432):
                self.throttle_hits += 1
                if self.verbose:
                    print(f'      ⚠ HTTP {status} 风控'
                          f'（当前出口 {mask_proxy(self.proxy)}）')
                if self.rotate():
                    continue
                if safe:
                    raise Blocked(f'HTTP {status} 且无代理可换')
                time.sleep(20 * (attempt + 1))
                last = status
                continue

            try:
                data = json.loads(body)
            except json.JSONDecodeError as exc:
                raise Blocked(f'返回非 JSON（前 120 字符）: {body[:120]!r}') from exc

            if isinstance(data, dict):
                if data.get('ok') == -100:
                    if self.guard:
                        self.guard.check_throttle(url, None, body[:800])
                    raise Blocked('该接口需要登录（返回 passport 跳转）')
                if data.get('ok') == 0:
                    msg = str(data.get('msg') or '')
                    if any(m in msg for m in EMPTY_MARKERS):
                        # 只是没结果，不是风控
                        if self.guard:
                            self.guard.record(url, 'empty', msg[:60])
                        raise EmptyResult(f'暂无结果: {msg}')
                    if self.guard:
                        self.guard.record(url, 'ok0', msg[:60])
                    raise Blocked(f"接口返回 ok=0: {str(data)[:120]}")
            return data
        raise Blocked(f'连续 {retries} 次失败: {last}')


# ---------------------------------------------------------------- 抓取层
def fetch_post(fetcher, mid):
    """取单条微博正文。"""
    d = fetcher.get_json(f'https://m.weibo.cn/statuses/show?id={mid}',
                         referer=f'https://m.weibo.cn/detail/{mid}')
    m = d.get('data') or {}
    if not m:
        return None
    user = m.get('user') or {}
    text = clean_text(m.get('text'))
    # 话题标签数量：#...# 的个数。政务"晚安"帖常在末尾挂一串广告标签，
    # 会因标签命中搜索但内容与事件无关，用标签数可以识别这类噪音。
    hashtags = re.findall(r'#([^#]{1,30})#', text)
    return {
        'mid': str(m.get('id') or mid),
        'bid': m.get('bid') or '',
        'created_at': m.get('created_at') or '',
        'text': text,
        'is_long_text': 1 if m.get('isLongText') else 0,
        'user_id': str(user.get('id') or ''),
        'screen_name': user.get('screen_name') or '',
        'followers_count': user.get('followers_count'),
        'verified': 1 if user.get('verified') else 0,
        'verified_reason': user.get('verified_reason') or '',
        'reposts_count': m.get('reposts_count'),
        'comments_count': m.get('comments_count'),
        'attitudes_count': m.get('attitudes_count'),
        'source': clean_text(m.get('source')),
        'region_name': m.get('region_name') or '',
        'pic_count': len(m.get('pics') or []),
        # 图片 URL 用 | 分隔存放，供导出数据集时下载"事件配图"
        'pics': '|'.join(p.get('url') or p.get('large', {}).get('url') or ''
                         for p in (m.get('pics') or []) if p),
        'video_url': ((m.get('page_info') or {}).get('media_info') or {}).get(
            'stream_url_hd') or '',
        'retweeted_mid': str((m.get('retweeted_status') or {}).get('id') or ''),
        'hashtag_count': len(hashtags),
        'crawled_at': datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S'),
    }


def fetch_comments(fetcher, mid, max_pages=3):
    """取某条微博的评论。

    实测要点（2026-09-19，未登录状态）：
      - /comments/hotflow   返回“热门评论”一页约 9 条，max_id 恒为 0，无法翻页；
                            total_number 会报真实总数（如 23），但拿不到剩余部分
      - /api/comments/show  返回的是**同一批评论**（仅排序与字段不同，楼层号缺失），
                            第 1 页约 9 条，第 2 页即 {"ok":0,"msg":"暂无数据"}
      => 两接口合并去重后仍为约 9 条，未登录时单条微博的评论上限就是这里。
      多采集微博条数（而非指望单条翻页）才是提高评论总量的办法。

    携带登录 Cookie 后 hotflow 的 max_id 会给出有效翻页值，才可能抓全量。
    注意：这一点尚未在本机验证过——拿到 Cookie 后需实测确认。
    """
    out, seen = [], set()
    total = None

    def add(c, is_reply=0, parent=''):
        row = _comment_row(c, mid, is_reply=is_reply, parent_id=parent)
        if row and row['comment_id'] and row['comment_id'] not in seen:
            seen.add(row['comment_id'])
            out.append(row)

    # A) hotflow —— 热门评论，带楼层号与二级回复
    max_id, page = 0, 0
    while page < max_pages:
        page += 1
        url = (f'https://m.weibo.cn/comments/hotflow?id={mid}&mid={mid}'
               f'&max_id_type=0')
        if max_id:
            url += f'&max_id={max_id}'
        try:
            d = fetcher.get_json(url, referer=f'https://m.weibo.cn/detail/{mid}')
        except Blocked as exc:
            if fetcher.verbose:
                print(f'      ⚠ hotflow 第 {page} 页受阻：{exc}')
            break
        data = d.get('data') or {}
        items = data.get('data') or []
        if total is None:
            total = data.get('total_number')
        if not items:
            break
        for c in items:
            add(c)
            for sub in (c.get('comments') or []):
                add(sub, is_reply=1, parent=str(c.get('id')))
        new_max = data.get('max_id')
        if not new_max or new_max == max_id:
            break
        max_id = new_max

    # B) /api/comments/show —— 另一批评论（无楼层号）
    for page in range(1, max_pages + 1):
        url = f'https://m.weibo.cn/api/comments/show?id={mid}&page={page}'
        try:
            d = fetcher.get_json(url, referer=f'https://m.weibo.cn/detail/{mid}')
        except Blocked as exc:
            if fetcher.verbose:
                print(f'      ⚠ comments/show 第 {page} 页受阻：{exc}')
            break
        data = d.get('data')
        items = (data or {}).get('data') if isinstance(data, dict) else (data or [])
        if not items:
            break
        for c in items:
            add(c)
            for sub in (c.get('comments') or []):
                add(sub, is_reply=1, parent=str(c.get('id')))
    return out, total


def _comment_row(c, weibo_mid, is_reply=0, parent_id=''):
    if not isinstance(c, dict):
        return None
    user = c.get('user') or {}
    text = clean_text(c.get('text'))
    pic_num = c.get('pic_num') or 0
    # 纯图片评论没有文本，但仍是有效评论，不能丢
    if not text and not pic_num:
        return None
    return {
        'comment_id': str(c.get('id') or ''),
        'weibo_mid': weibo_mid,
        'is_reply': is_reply,
        'parent_comment_id': parent_id,
        'rootid': str(c.get('rootid') or ''),
        'floor_number': c.get('floor_number'),
        'created_at': c.get('created_at') or '',
        'text': text,
        'pic_num': pic_num,
        'user_id': str(user.get('id') or ''),
        'screen_name': user.get('screen_name') or '',
        'like_count': c.get('like_count'),
        'source': clean_text(c.get('source')),
        'crawled_at': datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S'),
    }


# ---------------------------------------------------------------- 发现层
def discover_hot(fetcher):
    """无需 Cookie：热搜榜置顶要闻（含 mid）作为事件源。"""
    ua_w = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')
    req = urllib.request.Request('https://weibo.com/ajax/side/hotSearch',
                                 headers={'User-Agent': ua_w,
                                          'Referer': 'https://weibo.com/'})
    with urllib.request.urlopen(req, timeout=20) as resp:
        d = json.loads(resp.read().decode('utf-8'))
    found = []
    data = d.get('data') or {}
    govs = data.get('hotgovs') or []
    if data.get('hotgov'):
        govs = [data['hotgov']] + list(govs)
    for g in govs:
        # 只认 hotgov 的 mid 字段。总榜条目里的 id 不是微博 mid，
        # 误取会得到无效值（实测会返回 param error）。
        mid = g.get('mid')
        if mid:
            found.append((str(mid), clean_text(g.get('word') or g.get('name') or '')))
    return found


def _parse_weibo_dt(s):
    """解析微博时间 'Sat Sep 19 23:05:04 +0800 2026' -> aware datetime。"""
    if not s:
        return None
    try:
        return datetime.strptime(s, '%a %b %d %H:%M:%S %z %Y')
    except (ValueError, TypeError):
        return None


#: 中立站点探测结果缓存（避免每次错误都去探测）
_NEUTRAL_CACHE = {'ts': 0.0, 'ok': None}


def neutral_site_reachable():
    """探测一个中立站点，用来区分「本地网络故障」和「微博封禁」。

    为什么需要它（踩过的坑）：
      本地代理抖动会报 `curl: (7) CONNECT tunnel failed, response 502`，
      这是**本地网络问题**，但当时被一律判为"网络层封禁"，
      护栏把整轮采集停掉了 2 小时——而微博其实一切正常。
      正确做法：先看中立站点通不通。
        · 中立站点也不通 → 本地网络/代理故障，不该当成封禁
        · 中立站点正常、只有微博不通 → 才可能是微博侧封锁
    """
    now = time.time()
    if now - _NEUTRAL_CACHE['ts'] < 30 and _NEUTRAL_CACHE['ok'] is not None:
        return _NEUTRAL_CACHE['ok']
    ok = False
    try:
        from curl_cffi import requests as creq
        r = creq.get('https://www.example.com', impersonate='chrome', timeout=12)
        ok = r.status_code == 200
    except Exception:                              # noqa: BLE001
        ok = False
    _NEUTRAL_CACHE.update(ts=now, ok=ok)
    return ok


def discover_search(fetcher, keyword, max_posts, pages=2, min_comments=0,
                    min_attitudes=0, stats=None, max_age_hours=0,
                    prefer_before=None):
    """按关键词搜索微博，返回**最热门的前 max_posts 条**。

    两个要点：
    1. 先筛后抓——搜索结果的 mblog 自带 comments_count / attitudes_count，
       所以能先按热度筛掉"没人讨论"的，再去请求正文与评论，省请求额度。
    2. **按热度排序后取 Top N**（而不是取搜索返回的前 N 条）。
       踩过的坑：原来按返回顺序取，导致同一事件被重复抓多条（实测 9 个事件
       主题抓成了 29 条微博，单个事件最多 6 条），内容大量重复。
       配合 --max-posts 1 就能做到"一个事件只留最热门的一条"。

    3. **prefer_before**（第三个要点，2026-09-20 新增）：
       只在"该时间点之前发布"的微博里挑最热的。
       为什么需要：调用方是拿"该话题下最热微博"来充当事件正文的，
       但原帖下沉时会把**上热搜之后才发的**帖子选出来，
       于是「事件发生时间」晚于「上热搜时间」——与"事件最开始发生的时间"矛盾。
       实测 8 条交付数据里出现 1 条这种倒挂。
       传入 onboard_ts 即可。若上榜前没有任何合格候选，则回退到全局最热，
       并把 stats['before_pref_failed'] 计数 +1，让调用方能把这种情况标出来，
       而不是悄悄给一个时间倒挂的值。
    """
    encoded = urllib.parse.quote(keyword)
    cand, seen_ids = [], set()
    seen = passed = 0
    for page in range(1, pages + 1):
        url = ('https://m.weibo.cn/api/container/getIndex?containerid='
               f'100103type%3D1%26q%3D{encoded}&page_type=searchall&page={page}')
        if fetcher.verbose:
            print(f'    搜索「{keyword}」第 {page} 页')
        try:
            d = fetcher.get_json(url, referer='https://m.weibo.cn/')
        except EmptyResult:
            # 该关键词没有更多结果 —— 正常情况，收尾后换下一个关键词
            if fetcher.verbose:
                print(f'      （第 {page} 页无结果，该关键词到此为止）')
            break
        except Blocked:
            if stats is not None:
                stats['seen'] = stats.get('seen', 0) + seen
                stats['passed'] = stats.get('passed', 0) + passed
            raise
        cards = ((d.get('data') or {}).get('cards') or [])
        got = 0
        for card in cards:
            for c in [card] + list(card.get('card_group') or []):
                m = c.get('mblog')
                if not m or not m.get('id'):
                    continue
                got += 1
                seen += 1
                mid = str(m['id'])
                if mid in seen_ids:
                    continue
                cc = m.get('comments_count') or 0
                ac = m.get('attitudes_count') or 0
                if cc < min_comments or ac < min_attitudes:
                    continue
                # 时效过滤：只要"最新"的事件。搜索接口按相关性排序，
                # 老帖也会被召回（尤其是行业类关键词），必须按发布时间筛掉。
                if max_age_hours:
                    ts = _parse_weibo_dt(m.get('created_at'))
                    if ts is None:
                        continue
                    age_h = (datetime.now(CST) - ts).total_seconds() / 3600.0
                    if age_h > max_age_hours:
                        continue
                seen_ids.add(mid)
                passed += 1
                # 热度 = 评论数为主，点赞数为辅（评论更能代表"事件被讨论"）
                heat = cc * 1000 + min(ac, 999)
                # 同时记录发布时间，供 prefer_before 判断（解析失败记 None）
                cand.append((heat, cc, ac, mid,
                             _parse_weibo_dt(m.get('created_at'))))
        if got == 0:
            break

    if stats is not None:
        stats['seen'] = stats.get('seen', 0) + seen
        stats['passed'] = stats.get('passed', 0) + passed
    if not cand:
        return []
    cand.sort(key=lambda x: -x[0])

    # 优先在"上榜之前发布"的候选里挑；取不到才回退，并如实计数
    pool, fallback = cand, False
    if prefer_before is not None:
        if isinstance(prefer_before, str):
            try:
                prefer_before = datetime.strptime(
                    prefer_before, '%Y-%m-%d %H:%M:%S').replace(tzinfo=CST)
            except ValueError:
                prefer_before = None
        if prefer_before is not None:
            early = [x for x in cand
                     if x[4] is not None and x[4] <= prefer_before]
            if early:
                pool = early
            else:
                fallback = True

    top = pool[:max_posts]
    if stats is not None and fallback:
        stats['before_pref_failed'] = stats.get('before_pref_failed', 0) + 1
    if fetcher.verbose:
        picked = '、'.join(f'{cc}评论/{ac}赞' for _, cc, ac, _, _ in top)
        tag = ('回退：上榜前无候选' if fallback
               else ('上榜前发布' if prefer_before is not None else '按热度'))
        print(f'      Top{len(top)}（{tag}）：{picked}')
    return [(mid, keyword) for _, _, _, mid, _ in top]


def discover_uid(fetcher, uid, max_posts, pages=5):
    """需要 Cookie：用户时间线。"""
    found, cid = [], f'107603{uid}'
    for page in range(1, pages + 1):
        if len(found) >= max_posts:
            break
        url = ('https://m.weibo.cn/api/container/getIndex?type=uid&value='
               f'{uid}&containerid={cid}&page={page}')
        if fetcher.verbose:
            print(f'    用户 {uid} 第 {page} 页')
        d = fetcher.get_json(url, referer=f'https://m.weibo.cn/u/{uid}')
        cards = ((d.get('data') or {}).get('cards') or [])
        got = 0
        for card in cards:
            m = card.get('mblog')
            if m and m.get('id'):
                found.append((str(m['id']), f'uid:{uid}'))
                got += 1
        if got == 0:
            break
    return found[:max_posts]


def fetch_hot_words(limit=20, categories=None, expand_subjects=True):
    """取热搜榜词条及其官方分类，作为「事件关键词」。无需 Cookie。

    category 来自 hot_band 接口；hotSearch 总榜没有该字段。

    expand_subjects=True 时，额外把 band 条目里的 `subject_querys`
    （形如 'event|2026亚运会|爱知名古屋亚运会'）拆成候选关键词。
    这么做是因为**热搜榜只有约 50 个词**，单轮事件数会被这个数量卡死；
    相关话题能把关键词池扩大，让一轮覆盖更多事件。
    """
    ua_w = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')

    def _get(url):
        """取 JSON。

        这里也必须走 curl_cffi：用原生 urllib 会因 TLS 指纹被拒，
        实测报 `ssl.SSLEOFError: UNEXPECTED_EOF_WHILE_READING`
        （与本项目采集 m.weibo.cn 时遇到的是同一类问题）。
        """
        try:
            from curl_cffi import requests as creq
            r = creq.get(url, headers={
                'User-Agent': ua_w, 'Referer': 'https://weibo.com/',
                'Accept': 'application/json, text/plain, */*'},
                impersonate='chrome', timeout=25)
            return r.json()
        except ImportError:
            req = urllib.request.Request(url, headers={
                'User-Agent': ua_w, 'Referer': 'https://weibo.com/',
                'Accept': 'application/json, text/plain, */*'})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode('utf-8'))

    cat_map, band_items = {}, []
    try:
        band = _get('https://weibo.com/ajax/statuses/hot_band?band_id=1')
        band_items = ((band.get('data') or {}).get('band_list')) or []
        for it in band_items:
            if it.get('word') and it.get('category'):
                cat_map.setdefault(it['word'], it['category'])
    except Exception as exc:                        # noqa: BLE001
        print(f'    （分类信息获取失败，继续：{type(exc).__name__}）')

    out, seen = [], set()

    def _add(word, cate):
        w = (word or '').strip()
        if not w or w in seen:
            return
        if categories and cate not in categories:
            return
        seen.add(w)
        out.append((w, cate))

    try:
        hs = _get('https://weibo.com/ajax/side/hotSearch')
    except Exception as exc:                        # noqa: BLE001
        print(f'    （热搜榜获取失败：{type(exc).__name__}: {str(exc)[:60]}）')
        hs = None
    if hs:
        for it in (((hs.get('data') or {}).get('realtime')) or []):
            _add(it.get('word'), cat_map.get(it.get('word'), ''))

    if expand_subjects:
        for it in band_items:
            sq = it.get('subject_querys') or ''
            if not sq:
                continue
            cate = it.get('category') or ''
            parts = [p.strip() for p in sq.split('|') if p.strip()]
            for p in parts[1:]:                     # 第 0 段是类型标记，跳过
                _add(p, cate)

    return out[:limit]


def load_domain_keywords(path):
    """读取领域补充关键词表。

    格式：每行 `领域: 关键词1, 关键词2, ...`，`#` 开头为注释。

    为什么需要它：热搜榜天然偏向娱乐/体育/社会新闻，金融、教育、医疗、
    法律等行业几乎上不了榜，光靠热搜词会导致领域覆盖严重偏斜。
    用领域词表主动补充，才能把领域铺开。
    """
    out = []
    if not path or not os.path.exists(path):
        return out
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if ':' in line:
                dom, _, rest = line.partition(':')
            elif '：' in line:
                dom, _, rest = line.partition('：')
            else:
                dom, rest = '', line
            dom = dom.strip()
            for kw in rest.split(','):
                kw = kw.strip()
                if kw:
                    out.append((kw, dom))
    return out


def interleave_by_category(words):
    """按官方分类轮转重排，保证领域多样。

    热搜榜里剧集/艺人往往占一半以上，若按原顺序取前 N 个，
    采到的全是娱乐事件。轮转后顺序变成
    「各分类第 1 个 → 各分类第 2 个 → …」，
    这样即使只取少量关键词也能覆盖多个领域。

    踩过的坑：原顺序取前 10 个时，9 个事件里 6 个是剧集/艺人。
    """
    buckets, order = {}, []
    for w, c in words:
        key = c or '未分类'
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append((w, c))
    out, i = [], 0
    while True:
        added = False
        for key in order:
            if i < len(buckets[key]):
                out.append(buckets[key][i])
                added = True
        if not added:
            break
        i += 1
    return out


def discover_hot_events(fetcher, max_posts, max_keywords, categories=None,
                        min_comments=0, min_attitudes=0, pages=2,
                        extra_words=None, max_extra=0, max_age_hours=0):
    """跟随热搜榜事件：每个关键词搜索并取**最热门的前 max_posts 条**。

    热搜榜本身就是一份「正在发生的事件」列表，且带官方分类，
    因此不需要用户自己指定关键词。关键词会先按分类轮转重排，
    以保证采到的领域足够分散。

    extra_words 是领域补充关键词 [(kw, domain)] —— 热搜榜覆盖不到的
    金融/教育/医疗/法律等行业靠它补齐。max_extra 限制本次用多少个。

    需要 Cookie（搜索接口未登录会返回 ok:-100 / 432）。
    """
    words = fetch_hot_words(200, categories)      # 先全量取，再重排截断
    words = interleave_by_category(words)[:max_keywords]
    if extra_words:
        picked = extra_words if not max_extra else extra_words[:max_extra]
        # 领域词放在前面：它们填空缺领域，优先级高于已经很多的热搜词
        words = picked + words
        print(f'    + 领域补充关键词 {len(picked)} 个'
              f'（共 {len(extra_words)} 个可用）')
    if not words:
        return []
    cats_used = {}
    for _, c in words:
        cats_used[c or '未分类'] = cats_used.get(c or '未分类', 0) + 1
    if fetcher.verbose:
        print(f'    事件关键词 {len(words)} 个，覆盖 {len(cats_used)} 个领域：'
              + '、'.join(f'{k}×{v}' for k, v in
                          sorted(cats_used.items(), key=lambda x: -x[1])))
        print('    取词：' + '、'.join(f'{w}({c})' if c else w for w, c in words)
              + ('…' if len(words) > 10 else ''))
    found = []
    stats = {}
    for word, cate in words:
        try:
            got = discover_search(fetcher, word, max_posts, pages=pages,
                                  min_comments=min_comments,
                                  min_attitudes=min_attitudes, stats=stats,
                                  max_age_hours=max_age_hours)
        except Blocked as exc:
            print(f'    ✗ 搜索「{word}」受阻：{exc}')
            print('    → 确认为风控/登录问题（非"无结果"），已中止事件发现，'
                  '避免加重账号标记。')
            break
        if fetcher.verbose:
            print(f'    「{word}」入选 {len(got)} 条')
        found += [(mid, f'{word}|{cate}') for mid, _ in got]
    print(f'    [筛选统计] 看到 {stats.get("seen", 0)} 条微博，'
          f'符合「评论≥{min_comments}」的 {stats.get("passed", 0)} 条，'
          f'按热度取 {len(found)} 条')
    return found


# ---------------------------------------------------------------- 存储层
EVENT_COLS = ['mid', 'bid', 'created_at', 'text', 'is_long_text', 'user_id',
              'screen_name', 'followers_count', 'verified', 'verified_reason',
              'reposts_count', 'comments_count', 'attitudes_count', 'source',
              'region_name', 'pic_count', 'pics', 'video_url', 'retweeted_mid',
              'event_keyword', 'hashtag_count', 'crawled_at']
COMMENT_COLS = ['comment_id', 'weibo_mid', 'is_reply', 'parent_comment_id',
                'rootid', 'floor_number', 'created_at', 'text', 'pic_num',
                'user_id', 'screen_name', 'like_count', 'source', 'crawled_at']

DDL = """
CREATE TABLE IF NOT EXISTS events (
    mid TEXT PRIMARY KEY, bid TEXT, created_at TEXT, text TEXT,
    is_long_text INTEGER, user_id TEXT, screen_name TEXT,
    followers_count INTEGER, verified INTEGER, verified_reason TEXT,
    reposts_count INTEGER, comments_count INTEGER, attitudes_count INTEGER,
    source TEXT, region_name TEXT, pic_count INTEGER, pics TEXT, video_url TEXT,
    retweeted_mid TEXT, event_keyword TEXT, hashtag_count INTEGER,
    crawled_at TEXT
);
CREATE TABLE IF NOT EXISTS comments (
    comment_id TEXT PRIMARY KEY, weibo_mid TEXT, is_reply INTEGER,
    parent_comment_id TEXT, rootid TEXT, floor_number INTEGER,
    created_at TEXT, text TEXT, pic_num INTEGER, user_id TEXT,
    screen_name TEXT, like_count INTEGER, source TEXT, crawled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_c_mid ON comments(weibo_mid);
CREATE INDEX IF NOT EXISTS idx_e_kw ON events(event_keyword);
CREATE TABLE IF NOT EXISTS crawl_log (
    ts TEXT, mode TEXT, target TEXT, posts INTEGER, comments INTEGER, note TEXT
);
"""


def _migrate(conn):
    """补齐老库缺失的列。

    CREATE TABLE IF NOT EXISTS 不会修改已存在的表，脚本迭代时新增字段
    （例如 pic_num）会导致 INSERT 报 "no column named ..."，这里自动补列。
    """
    expected = {
        'events': EVENT_COLS,
        'comments': COMMENT_COLS,
        'crawl_log': ['ts', 'mode', 'target', 'posts', 'comments', 'note'],
    }
    for table, cols in expected.items():
        try:
            have = {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
        except sqlite3.Error:
            continue
        if not have:
            continue
        for col in cols:
            if col not in have:
                conn.execute(f'ALTER TABLE {table} ADD COLUMN {col}')
                print(f'  [迁移] {table} 补充字段 {col}')


def store(root, events, comments, mode, target, note='', log=True):
    """落库。events/comments 的主键去重，所以可以安全地反复调用。

    log=False 时不写 crawl_log —— 增量落库时用它，避免每个事件都留一条日志。
    """
    data_dir = os.path.join(root, 'data')
    os.makedirs(data_dir, exist_ok=True)
    conn = sqlite3.connect(os.path.join(data_dir, 'events.db'))
    conn.executescript(DDL)
    _migrate(conn)
    if events:
        conn.executemany(
            f'INSERT OR REPLACE INTO events ({",".join(EVENT_COLS)}) '
            f'VALUES ({",".join(["?"] * len(EVENT_COLS))})',
            [tuple(e.get(c) for c in EVENT_COLS) for e in events])
    if comments:
        conn.executemany(
            f'INSERT OR REPLACE INTO comments ({",".join(COMMENT_COLS)}) '
            f'VALUES ({",".join(["?"] * len(COMMENT_COLS))})',
            [tuple(c.get(k) for k in COMMENT_COLS) for c in comments])
    if log:
        conn.execute('INSERT INTO crawl_log VALUES (?,?,?,?,?,?)',
                     (datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S'), mode,
                      str(target)[:200], len(events), len(comments), note[:200]))
    conn.commit()
    tot_e = conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]
    tot_c = conn.execute('SELECT COUNT(*) FROM comments').fetchone()[0]
    conn.close()

    for fname, cols, rows in (('events.csv', EVENT_COLS, events),
                              ('comments.csv', COMMENT_COLS, comments)):
        if not rows:
            continue
        path = os.path.join(data_dir, fname)
        need_header = not os.path.exists(path)
        with open(path, 'a', encoding='utf-8-sig', newline='') as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            if need_header:
                w.writeheader()
            w.writerows([{k: r.get(k) for k in cols} for r in rows])
    return tot_e, tot_c


# ---------------------------------------------------------------- 发现调度
def build_event_words(args):
    """构造本次要处理的事件关键词列表 [(kw, 领域)]。

    默认「领域补充词在前」——因为它们填的是热搜覆盖不到的空缺领域。
    加 --hot-first 则反过来，热搜词优先（适用于"只要热搜事件"的场景）。
    """
    cats = [c.strip() for c in (args.categories or '').split(',') if c.strip()]
    hot = interleave_by_category(fetch_hot_words(200, cats))[:args.max_keywords]
    extras = load_domain_keywords(getattr(args, 'keyword_file', None))
    if extras:
        extras = interleave_by_category(extras)
        n = args.max_extra_keywords or len(extras)
        extras = extras[:n]
        print(f'    领域补充词表载入，本次使用 {len(extras)} 个')
    if getattr(args, 'hot_first', False):
        print(f'    热搜词优先：热搜 {len(hot)} 个在前，领域词 {len(extras)} 个在后')
        return hot + extras
    return extras + hot


def _fetch_one(args, fetcher, mid, kw, label=''):
    """抓一条微博的正文+评论，并**立即落库**。

    返回 (events_added, comments_added, skipped_noise)。
    遇到 SafetyAbort 直接向上抛，由调用方决定停止；
    遇 Blocked（该条不可用）只跳过这一条，不中止整轮。
    """
    print(f'\n{label} mid={mid}  <{kw}>')
    post = fetch_post(fetcher, mid)
    if not post:
        print('    ✗ 该 mid 无数据（可能已删除或仅粉丝可见）')
        return 0, 0, 0
    if args.max_hashtags and (post.get('hashtag_count') or 0) > args.max_hashtags:
        print(f'    ⤫ 跳过：正文含 {post["hashtag_count"]} 个话题标签，'
              f'超过上限 {args.max_hashtags}，疑似与事件无关')
        return 0, 0, 1
    post['event_keyword'] = kw
    print(f'    ✓ 正文 {len(post["text"])} 字 | {post["screen_name"]} '
          f'| 接口报评论 {post["comments_count"]}')
    comments = []
    try:
        comments, total = fetch_comments(fetcher, mid, args.max_comment_pages)
        print(f'    ✓ 评论 {len(comments)} 条（接口报告总数 {total}）')
    except Blocked as exc:
        print(f'    ✗ 评论受阻：{exc}')
    store(args.root, [post], comments, args.mode, kw, log=False)
    return 1, len(comments), 0


def crawl_interleaved(args, fetcher):
    """逐关键词交替「搜索 -> 抓正文+评论」。

    为什么需要它（踩过的坑，代价是一次 403）：
      原来的流程是「先把所有关键词搜一遍，再统一抓取」。
      当关键词有 130 个时，发现阶段就变成**连续 130 次搜索请求**，
      实测在第 54 次时被返回 HTTP 403。
      连续几十次"只搜不看"是真人不会有的模式，很容易被识别。
      交替执行后，搜索请求被正文/评论请求隔开，行为模式接近真人。
    """
    words = build_event_words(args)
    if not words:
        print('没有可用的事件关键词')
        return
    cats = {}
    for _, c in words:
        cats[c or '未分类'] = cats.get(c or '未分类', 0) + 1
    print(f'    交替模式：{len(words)} 个关键词，覆盖 {len(cats)} 个领域')
    print('    ' + '、'.join(f'{k}×{v}' for k, v in
                            sorted(cats.items(), key=lambda x: -x[1])[:18]))

    # 已抓过的 mid，避免重复
    done_mids = set()
    if args.skip_crawled:
        try:
            conn = sqlite3.connect(os.path.join(args.root, 'data', 'events.db'))
            done_mids = {r[0] for r in conn.execute('SELECT mid FROM events')}
            conn.close()
        except sqlite3.Error:
            pass

    n_ev = n_cm = n_noise = n_skip = 0
    aborted = None
    stats = {}

    if args.initial_cooldown > 0:
        print(f'\n    冷却 {args.initial_cooldown}s 后再开工'
              f'（刚触发过风控需要静默期）')
        remain = args.initial_cooldown
        while remain > 0:
            step = min(30, remain)
            time.sleep(step)
            remain -= step
            print(f'      静默中… 剩余 {remain}s', flush=True)

    for i, (word, cate) in enumerate(words, 1):
        print(f'\n--- [{i}/{len(words)}] 关键词「{word}」({cate}) ---')
        try:
            got = discover_search(fetcher, word, args.max_posts,
                                  pages=args.search_pages,
                                  min_comments=args.min_comments,
                                  min_attitudes=args.min_attitudes,
                                  stats=stats,
                                  max_age_hours=args.max_age_hours)
        except SafetyAbort as exc:
            aborted = exc
            break
        except Blocked as exc:
            print(f'    ✗ 搜索受阻：{exc}')
            continue
        if not got:
            print('    无符合条件的结果，跳过')
            continue
        mid = got[0][0]
        if mid in done_mids:
            n_skip += 1
            print(f'    该事件已抓过（mid={mid}），跳过')
            continue
        try:
            e, cm, noise = _fetch_one(args, fetcher, mid, f'{word}|{cate}')
        except SafetyAbort as exc:
            aborted = exc
            break
        except Blocked as exc:
            print(f'    ✗ 正文受阻：{exc}')
            continue
        n_ev += e
        n_cm += cm
        n_noise += noise
        if e:
            done_mids.add(mid)

    print(f'\n=== 完成（交替模式）===')
    print(f'本次：正文 {n_ev} 条，评论 {n_cm} 条')
    if n_noise:
        print(f'     因话题标签过多跳过 {n_noise} 条')
    if n_skip:
        print(f'     因已抓过跳过 {n_skip} 个事件')
    print(f'     搜索筛选统计：看到 {stats.get("seen", 0)} 条，'
          f'符合条件 {stats.get("passed", 0)} 条')
    conn = sqlite3.connect(os.path.join(args.root, 'data', 'events.db'))
    print(f'累计：events {conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]} 行，'
          f'comments {conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]} 行')
    conn.close()
    if aborted:
        print(f'\n🛑 触发安全熔断并已停止：{aborted}')


def _discover(args, fetcher, cookie):
    """按模式发现待抓微博，返回 [(mid, event_key)]。"""
    if args.mode == 'mid':
        raws = [args.mid] if args.mid else []
        if args.mids:
            raws += [m.strip() for m in args.mids.split(',') if m.strip()]
        return [(m, 'manual') for m in raws]

    if args.mode == 'hot':
        return discover_hot(fetcher)

    if not cookie:
        hint = {
            'search': '搜索接口返回 ok:-100 强制登录',
            'uid': '用户时间线接口返回 HTTP 432',
            'hot-events': '它要把每个热搜词拿去搜索，而搜索接口未登录必然失败',
        }.get(args.mode, '该模式需要登录')
        raise Blocked(f'{args.mode} 模式需要 Cookie（{hint}）')

    if args.mode == 'search':
        out = []
        for kw in [k.strip() for k in (args.keywords or '').split(',') if k.strip()]:
            out += discover_search(fetcher, kw, args.max_posts,
                                   pages=args.search_pages,
                                   min_comments=args.min_comments,
                                   min_attitudes=args.min_attitudes,
                                   max_age_hours=args.max_age_hours)
        return out

    if args.mode == 'uid':
        out = []
        for uid in [u.strip() for u in (args.uids or '').split(',') if u.strip()]:
            out += discover_uid(fetcher, uid, args.max_posts)
        return out

    if args.mode == 'hot-events':
        cats = [c.strip() for c in (args.categories or '').split(',') if c.strip()]
        extras = load_domain_keywords(args.keyword_file)
        if extras:
            # 与热搜词一样，按领域轮转，避免某个领域一次吃光额度
            extras = interleave_by_category(extras)
            n = args.max_extra_keywords or len(extras)
            print(f'    领域补充词表载入 {len(extras)} 个，本次使用 {min(n, len(extras))} 个')
        return discover_hot_events(fetcher, args.max_posts, args.max_keywords,
                                   cats or None,
                                   min_comments=args.min_comments,
                                   min_attitudes=args.min_attitudes,
                                   pages=args.search_pages,
                                   extra_words=extras,
                                   max_extra=args.max_extra_keywords,
                                   max_age_hours=args.max_age_hours)
    return []


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', default='.')
    ap.add_argument('--mode',
                    choices=['hot', 'mid', 'search', 'uid', 'hot-events'],
                    default='hot')
    ap.add_argument('--mid', help='--mode mid 时的微博 mid')
    ap.add_argument('--mids', help='多个 mid，逗号分隔')
    ap.add_argument('--keywords', help='--mode search 时的关键词，逗号分隔')
    ap.add_argument('--uids', help='--mode uid 时的用户 uid，逗号分隔')
    ap.add_argument('--max-posts', type=int, default=20)
    ap.add_argument('--max-keywords', type=int, default=20,
                    help='--mode hot-events 时取用多少个热搜词作为事件关键词')
    ap.add_argument('--categories', help='只跟随这些官方分类，逗号分隔，'
                                        '如 民生新闻,国际时政,教育')
    ap.add_argument('--min-comments', type=int, default=0,
                    help='事件质量过滤：微博评论数下限（搜索结果里就能读到，'
                         '先筛后抓，省请求额度）')
    ap.add_argument('--min-attitudes', type=int, default=0,
                    help='事件质量过滤：点赞数下限')
    ap.add_argument('--skip-crawled', action='store_true',
                    help='跳过 events 表里已抓过的微博。热搜词变化慢，'
                         '重复跑会命中同一批，加上它可避免白花请求')
    ap.add_argument('--search-pages', type=int, default=2,
                    help='每个关键词翻几页搜索结果（越大候选越多）')
    ap.add_argument('--keyword-file',
                    help='领域补充关键词表（每行 `领域: 关键词1, 关键词2`）。'
                         '热搜榜偏娱乐/体育，金融/教育/医疗/法律等行业靠它补齐')
    ap.add_argument('--max-extra-keywords', type=int, default=0,
                    help='本次最多用多少个领域补充词（0=不限）')
    ap.add_argument('--max-hashtags', type=int, default=0,
                    help='正文话题标签数上限（0=不限）。政务"晚安"帖常在末尾挂一串'
                         '广告标签，会因标签命中搜索但内容与事件无关；设 4 可滤掉')
    ap.add_argument('--max-age-hours', type=float, default=0,
                    help='时效过滤：只收发布于 N 小时内的微博（0=不限）。'
                         '行业类关键词（如"A股大跌"）会召回多年前的老帖，'
                         '想要"最新事件"就设 48')
    ap.add_argument('--hot-first', action='store_true',
                    help='热搜词优先于领域补充词。适用于"只要热搜事件"的场景')
    ap.add_argument('--max-comment-pages', type=int, default=3)
    ap.add_argument('--delay', type=float, default=None,
                    help='非安全模式下的请求间隔；安全模式请用 --min-delay/--max-delay')
    ap.add_argument('--min-delay', type=float, default=8.0,
                    help='安全模式请求间隔下限秒（默认 8，不建议调小）')
    ap.add_argument('--max-delay', type=float, default=15.0,
                    help='安全模式请求间隔上限秒（默认 15）')
    ap.add_argument('--max-requests', type=int, default=60,
                    help='单次运行请求数上限（默认 60，不建议调高）')
    ap.add_argument('--max-requests-per-day', type=int, default=300,
                    help='自然日请求数上限（默认 300，不建议调高）')
    ap.add_argument('--max-consecutive-search', type=int, default=25,
                    help='连续搜索请求上限（默认 25）。超过即主动停止——'
                         '连续大量"只搜不看"是真人不会有的模式，'
                         '实测第 54 次连搜会被返回 HTTP 403')
    ap.add_argument('--interleave', action='store_true',
                    help='交替模式：逐关键词「搜索 -> 抓正文+评论」，'
                         '让搜索请求被抓取请求隔开。关键词多时强烈建议开启')
    ap.add_argument('--initial-cooldown', type=int, default=0,
                    help='开始前的冷却秒数。刚触发过风控时留一段静默期再开工')
    ap.add_argument('--unsafe-no-abort', action='store_true',
                    help='关闭「见风控即熔断」（不推荐，会明显加大账号风险）')
    ap.add_argument('--dry-run', action='store_true',
                    help='只预估请求量与耗时，不发任何请求')
    ap.add_argument('--cookie', default=os.getenv('WEIBO_COOKIE', ''))
    ap.add_argument('--cookie-file', help='一行 cookie 的文本文件路径')
    ap.add_argument('--proxy', help='单个代理，如 http://user:pass@host:port')
    ap.add_argument('--proxy-file',
                    help='代理列表文件，一行一个；遇 432 自动轮换')
    args = ap.parse_args()

    cookie = args.cookie
    if args.cookie_file and os.path.exists(args.cookie_file):
        cookie = open(args.cookie_file, encoding='utf-8').read().strip()
    if not cookie:
        print('[提示] 未提供 Cookie。正文与评论仍可抓取；'
              'search / uid / hot-events 模式会因需登录而失败。')

    proxies = []
    if args.proxy:
        proxies.append(args.proxy)
    if args.proxy_file and os.path.exists(args.proxy_file):
        with open(args.proxy_file, encoding='utf-8') as fh:
            proxies += [ln.strip() for ln in fh
                        if ln.strip() and not ln.startswith('#')]
    if proxies:
        print(f'[代理] 共 {len(proxies)} 个，当前 {mask_proxy(proxies[0])}')

    # ---- 账号安全护栏 ----
    guard = SafetyGuard(
        os.path.join(args.root, 'data', 'safety_audit.db'),
        max_per_run=args.max_requests,
        max_per_day=args.max_requests_per_day,
        min_delay=args.min_delay if args.delay is None else args.delay,
        max_delay=args.max_delay if args.delay is None else args.delay * 1.8,
        abort_on_throttle=not args.unsafe_no_abort,
        max_consecutive_search=args.max_consecutive_search,
        verbose=True)
    if cookie:
        print()
        print(guard.danger_report())
        print()

    fetcher = Fetcher(cookie=cookie, proxies=proxies, verbose=True, guard=guard)

    if args.dry_run:
        per_kw = 1
        n = 0
        if args.mode in ('hot-events', 'search'):
            kws = (args.max_keywords if args.mode == 'hot-events'
                   else max(1, len([k for k in (args.keywords or '').split(',') if k.strip()])))
            n = kws * per_kw + kws * args.max_posts * (1 + args.max_comment_pages)
        elif args.mode == 'uid':
            n = max(1, len([u for u in (args.uids or '').split(',') if u.strip()])) \
                * args.max_posts * (1 + args.max_comment_pages)
        else:
            n = 1 * (1 + args.max_comment_pages)
        print(f'[DRY-RUN] 模式 {args.mode}，未发送任何请求')
        guard.plan(n)
        return

    # ---- 交替模式：逐关键词搜索+抓取，避免连续大量搜索请求 ----
    if args.interleave:
        if not cookie:
            print('[中断] 交替模式仍需 Cookie（搜索接口未登录必然失败）。')
            return
        try:
            crawl_interleaved(args, fetcher)
        except SafetyAbort as exc:
            print(f'\n🛑 安全护栏熔断，已停止：{exc}')
        print('\n--- 安全护栏汇总 ---')
        print(guard.summary())
        return

    # ---- 发现待抓微博 ----
    try:
        targets = _discover(args, fetcher, cookie)
    except SafetyAbort as exc:
        print(f'\n🛑 安全护栏在「发现微博」阶段熔断：{exc}')
        print(guard.summary())
        return
    except Blocked as exc:
        print(f'\n发现阶段受阻：{exc}')
        print(guard.summary())
        return
    if args.mode == 'mid':
        pass  # 已在 _discover 内处理

    # 去重
    seen, uniq = set(), []
    for mid, kw in targets:
        if mid and mid not in seen:
            seen.add(mid)
            uniq.append((mid, kw))

    # 跳过已抓过的微博：热搜词变化慢，重复跑会命中同一批，
    # 主键去重拿不到新数据、请求却白花。这里直接过滤掉。
    if args.skip_crawled:
        try:
            conn = sqlite3.connect(os.path.join(args.root, 'data', 'events.db'))
            done = {r[0] for r in conn.execute('SELECT mid FROM events')}
            conn.close()
        except sqlite3.Error:
            done = set()
        before = len(uniq)
        uniq = [(m, k) for m, k in uniq if m not in done]
        print(f'跳过已抓过的微博 {before - len(uniq)} 条'
              f'（库内已有 {len(done)} 条）')

    print(f'\n待抓微博 {len(uniq)} 条（模式 {args.mode}）')

    # ---- 抓取 ----
    all_events, all_comments, done = [], [], 0
    aborted = None
    skipped_noise = 0
    for mid, kw in uniq:
        print(f'\n[{done+1}/{len(uniq)}] mid={mid}  <{kw}>')
        try:
            post = fetch_post(fetcher, mid)
        except SafetyAbort as exc:
            aborted = exc
            break
        except Blocked as exc:
            print(f'    ✗ 正文受阻：{exc}')
            continue
        if not post:
            print('    ✗ 该 mid 无数据（可能已删除或仅粉丝可见）')
            continue
        # 过滤"广告尾部标签"型噪音：正文挂了一串无关话题标签，
        # 只因标签命中搜索才被召回，内容和事件无关。
        if args.max_hashtags and (post.get('hashtag_count') or 0) > args.max_hashtags:
            print(f'    ⤫ 跳过：正文含 {post["hashtag_count"]} 个话题标签，'
                  f'超过上限 {args.max_hashtags}，疑似与事件无关')
            skipped_noise += 1
            continue
        post['event_keyword'] = kw
        all_events.append(post)
        print(f'    ✓ 正文 {len(post["text"])} 字 | '
              f'{post["screen_name"]} | 评论数 {post["comments_count"]}')
        try:
            comments, total = fetch_comments(fetcher, mid,
                                             args.max_comment_pages)
        except SafetyAbort as exc:
            aborted = exc
            break
        except Blocked as exc:
            print(f'    ✗ 评论受阻：{exc}')
            comments, total = [], None
        all_comments.extend(comments)
        print(f'    ✓ 评论 {len(comments)} 条（接口报告总数 {total}）')
        done += 1
        # 每抓完一条就落库：中途中断（熔断/超时/手动停）也不会丢掉已抓数据
        store(args.root, [post], comments, args.mode,
              args.keywords or args.uids or args.mid or 'hot', log=False)

    if aborted:
        print(f'\n🛑 触发安全熔断并已停止：{aborted}')

    tot_e, tot_c = store(args.root, all_events, all_comments, args.mode,
                         args.keywords or args.uids or args.mid or 'hot',
                         note=f'throttled={fetcher.throttle_hits}'
                              + ('; ABORTED' if aborted else ''))
    print(f'\n=== 完成 ===')
    print(f'本次：正文 {len(all_events)} 条，评论 {len(all_comments)} 条')
    if skipped_noise:
        print(f'     因话题标签过多跳过 {skipped_noise} 条（疑似与事件无关）')
    print(f'累计：events {tot_e} 行，comments {tot_c} 行')
    print('--- 安全护栏汇总 ---')
    print(guard.summary())
    if guard.throttle_signals:
        print()
        print('  ⚠ 本次出现过风控信号。请至少停 12 小时，'
              '期间不要用该账号做任何自动化操作。')


if __name__ == '__main__':
    main()
