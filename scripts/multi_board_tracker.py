# -*- coding: utf-8 -*-
"""
多榜单热搜追踪器 v1（2026-09-21 重构）
================================================================================
需求（v3）：
  · 六个分类榜（文娱 / 生活 / 社会 / 同城 / 体育 / 科技），**每榜 Top50 全采**
  · 30 分钟一轮采样
  · 评论**按游标逐页累积**：每轮对在榜话题抓 1 页新评论，`comment_id` 去重
    （第 1 轮 1-20、第 2 轮 21-40 …，游标推进 + 主键去重双保险）
  · 下榜则记录时间并停止监控；**复上榜新开区段**，中间缺席期天然留白
  · 正文仅对新话题抓一次（搜索 1 + 正文 1），避免浪费额度

用法：
  python scripts/multi_board_tracker.py --probe              # 探测六榜 band_id 映射
  python scripts/multi_board_tracker.py --tick               # 跑一轮
  python scripts/multi_board_tracker.py --status             # 查看状态
  python scripts/multi_board_tracker.py --loop-minutes 450 --interval 30 --wait-network
                                                             # 长循环（等到 7:30）
"""
import argparse
import hashlib
import json
import os
import random
import re
import sqlite3
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from event_crawler import (Blocked, EmptyResult, clean_text,      # noqa: E402
                           discover_search, fetch_post, _parse_weibo_dt)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, 'data', 'multi_boards.db')
COOKIE_FILE = os.path.join(ROOT, 'config', 'cookie.txt')
CST = timezone(timedelta(hours=8))

UA_W = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')
UA_M = ('Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) '
        'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1')

# 六个分类榜的接口端点（2026-09-22 用真实浏览器从 https://weibo.com/hot/search
# 点击 tab 抓到的真实请求）。要点：
#   · 接口形如 https://weibo.com/ajax/statuses/{endpoint}
#   · **体育是单数 sport**，写成 sports 会 404（这是最初没找到的坑）
#   · 微博页面上的 tab 实际是：热搜 / 文娱 / 社会 / 科技 / 生活 / 体育 / ACG
#     —— **没有「同城」榜**，故用 ACG 补齐第六个
#   · 科技榜与 ACG 榜只有 30 条，其余 50 条
BOARD_ENDPOINTS = [
    ('entertainment', '文娱'),
    ('life',          '生活'),
    ('social',        '社会'),
    ('sport',         '体育'),
    ('technology',    '科技'),
    ('acg',           'ACG'),
]

# 限速（秒）：board 极便宜；search 是唯一踩过 403 的接口，单独保守
DELAY = {'board': (1.0, 1.6), 'search': (6.0, 10.0),
         'post': (1.5, 3.0), 'comment': (1.5, 3.0)}
SEARCH_STREAK_LIMIT = 8          # 连续搜索超过这个数就暂停（血的教训：54 次被封）

DDL = """
CREATE TABLE IF NOT EXISTS boards (
    board_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint   TEXT UNIQUE,        -- 接口端点：entertainment / life / social / sport /…
    name       TEXT,               -- 我们起的规范名（文娱 / 生活 / …）
    board_size INTEGER,            -- 该榜实测条数（50 或 30）
    enabled    INTEGER DEFAULT 1,
    probed_at  TEXT
);

CREATE TABLE IF NOT EXISTS topics (
    topic_id      TEXT PRIMARY KEY,   -- md5(board_id:word)[:16]
    board_id      INTEGER NOT NULL,
    word          TEXT NOT NULL,
    word_scheme   TEXT,
    category      TEXT,
    first_seen_ts TEXT,
    last_seen_ts  TEXT,
    is_active     INTEGER DEFAULT 1,
    miss_streak   INTEGER DEFAULT 0,
    onboard_count INTEGER DEFAULT 1,
    peak_heat     INTEGER,
    latest_heat   INTEGER,
    url           TEXT,
    UNIQUE(board_id, word)
);

-- 上榜区段：一次上榜一行。复上榜 → seg_no=2,3…；缺席期不写样本 = 天然留白
CREATE TABLE IF NOT EXISTS topic_segments (
    topic_id    TEXT NOT NULL,
    seg_no      INTEGER NOT NULL,
    onboard_ts  TEXT NOT NULL,
    offboard_ts TEXT,               -- 下榜**下界**：最后一次确认在榜
    offboard_upper_ts TEXT,         -- 下榜**上界**：首次确认已不在榜
                                    --   真实下榜 ∈ (offboard_ts, offboard_upper_ts]
    PRIMARY KEY (topic_id, seg_no)
);

-- 热度样本（30 分钟一条）
CREATE TABLE IF NOT EXISTS heat_samples (
    topic_id   TEXT NOT NULL,
    seg_no     INTEGER NOT NULL,
    sample_ts  TEXT NOT NULL,
    num        INTEGER,
    rank       INTEGER,
    PRIMARY KEY (topic_id, sample_ts)
);

CREATE TABLE IF NOT EXISTS topic_posts (
    topic_id        TEXT PRIMARY KEY,
    board_id        INTEGER,
    word            TEXT,
    mid             TEXT,
    text            TEXT,
    screen_name     TEXT,
    user_id         TEXT,
    created_at      TEXT,
    reposts_count   INTEGER,
    comments_count  INTEGER,
    attitudes_count INTEGER,
    region_name     TEXT,
    source          TEXT,
    pics            TEXT,
    prefer_fallback INTEGER DEFAULT 0,
    crawled_at      TEXT
);

-- 评论：comment_id 主键 = 天然去重
CREATE TABLE IF NOT EXISTS comments (
    comment_id   TEXT PRIMARY KEY,
    topic_id     TEXT NOT NULL,
    mid          TEXT,
    root_id      TEXT,
    parent_id    TEXT,
    is_reply     INTEGER DEFAULT 0,
    floor_number INTEGER,
    screen_name  TEXT,
    user_id      TEXT,
    text         TEXT,
    like_count   INTEGER,
    reply_count  INTEGER,
    created_at   TEXT,
    seg_no       INTEGER,
    first_page   INTEGER,          -- 首次抓到时在第几页
    crawled_at   TEXT
);

-- 评论抓取进度（逐页累积去重的核心）
CREATE TABLE IF NOT EXISTS comment_progress (
    topic_id      TEXT PRIMARY KEY,
    seg_no        INTEGER,
    next_cursor   TEXT,
    cursor_type   INTEGER DEFAULT 0,
    pages_done    INTEGER DEFAULT 0,
    fetched_cnt   INTEGER DEFAULT 0,
    exhausted     INTEGER DEFAULT 0,
    last_fetch_ts TEXT
);

CREATE TABLE IF NOT EXISTS crawl_log (
    ts        TEXT,
    board_id  INTEGER,
    n_board   INTEGER,
    n_new     INTEGER,
    n_off     INTEGER,
    n_heat    INTEGER,
    n_post    INTEGER,
    n_cmt     INTEGER,
    req_used  INTEGER,
    note      TEXT
);

CREATE TABLE IF NOT EXISTS req_audit (
    ts     TEXT,
    url    TEXT,
    kind   TEXT,
    status TEXT,
    note   TEXT
);

CREATE INDEX IF NOT EXISTS idx_hs_topic ON heat_samples(topic_id);
CREATE INDEX IF NOT EXISTS idx_cm_topic ON comments(topic_id);
CREATE INDEX IF NOT EXISTS idx_tp_board ON topics(board_id, is_active);
"""


# ------------------------------------------------------------------ 工具

def now_cst():
    return datetime.now(CST)


def fmt(dt):
    return dt.strftime('%Y-%m-%d %H:%M:%S')


def tid_of(board_id, word):
    return hashlib.md5(f'{board_id}:{word}'.encode('utf-8')).hexdigest()[:16]


def load_cookie():
    if not os.path.exists(COOKIE_FILE):
        return ''
    with open(COOKIE_FILE, 'r', encoding='utf-8', errors='ignore') as f:
        for ln in f:
            ln = ln.strip()
            if ln and not ln.startswith('#'):
                return ln
    return ''


class SearchLimit(Exception):
    """本轮搜索次数已达上限（防止连续搜索触发 403）。"""


class Fetcher:
    """带限速、连续搜索熔断、风控识别与审计的取数器。

    与 event_crawler.Fetcher 接口兼容（有 get_json / verbose / guard / cookie），
    这样 discover_search / fetch_post / fetch_comments 可以直接复用。
    """

    def __init__(self, cookie='', verbose=True, max_req_per_round=400):
        self.cookie = cookie
        self.verbose = verbose
        self.guard = None                    # 兼容 event_crawler 的 guard 检查
        self.max_req_per_round = max_req_per_round
        self.search_streak = 0               # 连续搜索计数
        self.req_round = 0                   # 本轮请求数
        self.throttle_hits = 0
        self._audit = None
        self._audit_pending = []

    def bind_audit(self, conn):
        self._audit = conn

    @staticmethod
    def kind_of(url):
        if 'container/getIndex' in url:
            return 'search'
        if 'comments/' in url:
            return 'comment'
        if 'statuses/show' in url:
            return 'post'
        if 'hot_band' in url or 'hotSearch' in url:
            return 'board'
        return 'general'

    def _headers(self, referer):
        h = {'User-Agent': UA_W if referer and 'weibo.com' in referer else UA_M,
             'Referer': referer or 'https://m.weibo.cn/',
             'Accept': 'application/json, text/plain, */*',
             'Accept-Language': 'zh-CN,zh;q=0.9',
             'X-Requested-With': 'XMLHttpRequest'}
        if self.cookie:
            h['Cookie'] = self.cookie
        return h

    def _audit_write(self, url, kind, status, note=''):
        ts = fmt(now_cst())
        if self._audit is not None:
            try:
                self._audit.execute(
                    'INSERT INTO req_audit(ts,url,kind,status,note) VALUES(?,?,?,?,?)',
                    (ts, url[:300], kind, status, note[:200]))
                self._audit.commit()
            except Exception:                                   # noqa: BLE001
                pass

    def get_json(self, url, retries=None, referer=None):
        kind = self.kind_of(url)
        # —— 本轮请求总量封顶：保 30 分钟节拍不漂移 ——
        if self.req_round >= self.max_req_per_round:
            raise Blocked(f'本轮请求已达上限 {self.max_req_per_round}，本轮收尾')
        # —— 连续搜索熔断（唯一踩过 403 的接口）——
        if kind == 'search':
            if self.search_streak >= SEARCH_STREAK_LIMIT:
                raise SearchLimit(f'连续搜索 {self.search_streak} 次，暂停本轮搜索')
            self.search_streak += 1
        else:
            self.search_streak = 0

        lo, hi = DELAY.get(kind, (1.5, 3.0))
        time.sleep(random.uniform(lo, hi))

        from curl_cffi import requests as creq
        self.req_round += 1
        try:
            r = creq.get(url, headers=self._headers(referer),
                         impersonate='chrome', timeout=25, allow_redirects=True)
        except Exception as exc:                                # noqa: BLE001
            self._audit_write(url, kind, 'ERR', f'{type(exc).__name__}: {exc}')
            raise Blocked(f'{type(exc).__name__}: {str(exc)[:90]}') from exc

        code = r.status_code
        body = r.text or ''
        if code in (403, 418, 429, 432):
            self.throttle_hits += 1
            self._audit_write(url, kind, f'HTTP{code}', 'THROTTLE')
            raise Blocked(f'HTTP {code} 疑似风控')
        if code != 200:
            self._audit_write(url, kind, f'HTTP{code}', '')
            raise Blocked(f'HTTP {code}')
        # 风控关键词（注意不能宽泛匹配 verify —— 正文自带 verified 字段会误判）
        for marker in ('passport.weibo.com', '"ok":-100', '访问过于频繁',
                       '请稍后再试', '登录后查看'):
            if marker in body:
                self.throttle_hits += 1
                self._audit_write(url, kind, 'BLOCKED', marker)
                raise Blocked(f'命中风控标记 {marker!r}')

        try:
            d = json.loads(body)
        except Exception:                                       # noqa: BLE001
            self._audit_write(url, kind, 'NOTJSON', body[:80])
            raise Blocked('返回不是 JSON')

        if isinstance(d, dict) and d.get('ok') == 0:
            msg = str(d.get('msg') or '')
            self._audit_write(url, kind, 'ok0', msg)
            if any(m in msg for m in ('暂无数据', '没有内容', '没有更多')):
                raise EmptyResult(msg)
        self._audit_write(url, kind, 'request', '')
        return d


# ------------------------------------------------------------- 榜单探测

def fetch_band(endpoint, limit=50, cookie=''):
    """取某个分类榜。

    ⚠️ **必须带登录 Cookie**：与总榜 hot_band 不同，分类榜接口在未登录时
    返回的 band_list 是**空的**（不报错、HTTP 200，很容易误判成"接口不存在"）。
    实测：同一 URL 带 Cookie → 50 条；不带 → 0 条。

    接口：https://weibo.com/ajax/statuses/{endpoint}
    踩过的坑：早期以为分类榜是 hot_band?band_id=N，实测 band_id 被服务端
    **完全忽略**（0~19 全部返回同一份总榜）。真正的分类榜是独立的 statuses
    路径，来自网页 https://weibo.com/hot/search 的 tab 切换。
    """
    from curl_cffi import requests as creq
    hdr = {'User-Agent': UA_W, 'Referer': 'https://weibo.com/hot/search',
           'Accept': 'application/json, text/plain, */*'}
    if cookie:
        hdr['Cookie'] = cookie
    d = creq.get(f'https://weibo.com/ajax/statuses/{endpoint}',
                 headers=hdr, impersonate='chrome', timeout=25).json()
    lst = ((d.get('data') or {}).get('band_list')) or []
    out = []
    for it in lst[:limit]:
        w = it.get('word')
        if not w or it.get('is_ad'):
            continue
        out.append({
            'word': w,
            'word_scheme': it.get('word_scheme') or f'#{w}#',
            'category': it.get('category') or it.get('m_category') or '',
            'heat': it.get('num') or it.get('hot_num'),
            'rank': (it.get('realpos') or it.get('rank') or 0) or 0,
            'onboard_time': it.get('onboard_time'),
            'url': it.get('url') or '',
        })
    return out


def probe_bands(verbose=True, cookie=''):
    """验证六个分类榜端点是否可用。返回 [(endpoint, 显示名, 条数)]。

    ⚠️ 分类榜普遍**不返回 onboard_time**（实测只有文娱榜有），
    所以「上榜时间」只能近似为「我们首次观测到它的时刻」——
    采样间隔就是它的精度上限。这一点在导出/报告里必须写明。
    """
    out = []
    for ep, name in BOARD_ENDPOINTS:
        try:
            items = fetch_band(ep, limit=200, cookie=cookie)
            if items:
                out.append((ep, name, len(items)))
                if verbose:
                    ob = sum(1 for it in items if it.get('onboard_time'))
                    print(f'  ✓ {name:<4} ({ep:<14}) {len(items):>3} 条  '
                          f'带 onboard_time 的 {ob} 条')
            elif verbose:
                print(f'  ✗ {name:<4} ({ep:<14}) 返回空 —— 多半是 Cookie 失效/未登录')
        except Exception as exc:                                # noqa: BLE001
            if verbose:
                print(f'  ✗ {name:<4} ({ep:<14}) {type(exc).__name__}: {str(exc)[:45]}')
        time.sleep(1.0)
    return out


def ensure_boards(c, found):
    """写入榜单字典。found = [(endpoint, name, count)]。"""
    ts = fmt(now_cst())
    for ep, name, n in found:
        row = c.execute('SELECT board_id FROM boards WHERE endpoint=?',
                        (ep,)).fetchone()
        if row:
            c.execute('UPDATE boards SET name=?,board_size=?,probed_at=? '
                      'WHERE endpoint=?', (name, n, ts, ep))
        else:
            c.execute('INSERT INTO boards(endpoint,name,board_size,probed_at,'
                      'enabled) VALUES(?,?,?,?,1)', (ep, name, n, ts))
    c.commit()
    return c.execute('SELECT board_id,endpoint,name,board_size FROM boards '
                     'WHERE enabled=1 ORDER BY board_id').fetchall()


# ------------------------------------------------------------------ 建库

def conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.executescript(DDL)
    # 兼容已建的旧库：补上后加的字段（CREATE TABLE IF NOT EXISTS 不会改旧表）
    for t, col, decl in (('topic_segments', 'offboard_upper_ts', 'TEXT'),):
        have = {r[1] for r in c.execute(f'PRAGMA table_info({t})')}
        if col not in have:
            c.execute(f'ALTER TABLE {t} ADD COLUMN {col} {decl}')
    c.commit()
    return c


# --------------------------------------------------------------- 采一轮

def tick(args, conn_hint=None):
    c = conn_hint or conn()
    boards = c.execute('SELECT board_id,endpoint,name FROM boards WHERE enabled=1 '
                       'ORDER BY board_id').fetchall()
    if not boards:
        print('!! boards 表为空：先用 --probe 探测并写入榜单映射')
        return None

    f = Fetcher(cookie=load_cookie(), verbose=args.verbose,
                max_req_per_round=args.max_req_per_round)
    f.bind_audit(c)
    ts = fmt(now_cst())
    stat = dict(n_board=0, n_new=0, n_off=0, n_heat=0, n_post=0, n_cmt=0)
    notes = []

    # ============ 1. 采榜 + 状态机 ============
    for board_id, endpoint, name in boards:
        try:
            items = fetch_band(endpoint, limit=args.per_board,
                               cookie=f.cookie)
        except Exception as exc:                                # noqa: BLE001
            notes.append(f'{name}取榜失败:{type(exc).__name__}')
            print(f'  [{name}] 取榜失败: {type(exc).__name__} {str(exc)[:60]}')
            continue
        f.req_round += 1
        stat['n_board'] += len(items)
        words_now = set()

        for it in items:
            w = it['word']
            words_now.add(w)
            tid = tid_of(board_id, w)
            row = c.execute('SELECT is_active,miss_streak,onboard_count,peak_heat '
                            'FROM topics WHERE topic_id=?', (tid,)).fetchone()
            if row is None:
                # —— 新话题：建 topic + 开第 1 段 ——
                ob = it['onboard_time']
                ob_ts = (fmt(datetime.fromtimestamp(float(ob), CST))
                         if ob else ts)
                c.execute('INSERT INTO topics(topic_id,board_id,word,word_scheme,'
                          'category,first_seen_ts,last_seen_ts,is_active,'
                          'miss_streak,onboard_count,peak_heat,latest_heat,url) '
                          'VALUES(?,?,?,?,?,?,?,1,0,1,?,?,?)',
                          (tid, board_id, w, it['word_scheme'], it['category'],
                           ts, ts, it['heat'], it['heat'], it['url']))
                c.execute('INSERT OR REPLACE INTO topic_segments'
                          '(topic_id,seg_no,onboard_ts,offboard_ts) VALUES(?,1,?,NULL)',
                          (tid, ob_ts))
                c.execute('INSERT OR REPLACE INTO heat_samples'
                          '(topic_id,seg_no,sample_ts,num,rank) VALUES(?,1,?,?,?)',
                          (tid, ts, it['heat'], it['rank']))
                stat['n_new'] += 1
                stat['n_heat'] += 1
            else:
                is_active, miss, cnt, peak = row
                seg = c.execute('SELECT MAX(seg_no) FROM topic_segments '
                                'WHERE topic_id=?', (tid,)).fetchone()[0] or 1
                if not is_active:
                    # —— 复上榜：新开一段（中间缺席期不写样本 = 天然留白）——
                    seg += 1
                    ob = it['onboard_time']
                    ob_ts = (fmt(datetime.fromtimestamp(float(ob), CST))
                             if ob else ts)
                    c.execute('INSERT OR REPLACE INTO topic_segments'
                              '(topic_id,seg_no,onboard_ts,offboard_ts) '
                              'VALUES(?,?,?,NULL)', (tid, seg, ob_ts))
                    c.execute('UPDATE topics SET is_active=1,miss_streak=0,'
                              'onboard_count=onboard_count+1 WHERE topic_id=?', (tid,))
                # 写样本 + 更新热度
                c.execute('INSERT OR REPLACE INTO heat_samples'
                          '(topic_id,seg_no,sample_ts,num,rank) VALUES(?,?,?,?,?)',
                          (tid, seg, ts, it['heat'], it['rank']))
                stat['n_heat'] += 1
                c.execute('UPDATE topics SET last_seen_ts=?,miss_streak=0,'
                          'latest_heat=?,peak_heat=MAX(COALESCE(peak_heat,0),?) '
                          'WHERE topic_id=?', (ts, it['heat'], it['heat'] or 0, tid))
        c.commit()

        # —— 本轮没出现的在榜话题：防抖后判下榜 ——
        actives = c.execute('SELECT topic_id FROM topics WHERE board_id=? '
                            'AND is_active=1', (board_id,)).fetchall()
        for (tid,) in actives:
            w = c.execute('SELECT word FROM topics WHERE topic_id=?',
                          (tid,)).fetchone()[0]
            if w in words_now:
                continue
            miss = c.execute('SELECT miss_streak FROM topics WHERE topic_id=?',
                             (tid,)).fetchone()[0] or 0
            new_miss = miss + 1
            seg = c.execute('SELECT MAX(seg_no) FROM topic_segments '
                            'WHERE topic_id=?', (tid,)).fetchone()[0] or 1
            if new_miss == 1:
                # 首次确认「已不在榜」= 下榜时刻的**上界**。
                # 真实下榜落在 (last_seen_ts, 本时刻]；只记下界会让
                # 「只被采到一轮」的话题显示成「上榜十几秒就下榜」。
                c.execute('UPDATE topic_segments SET offboard_upper_ts=? '
                          'WHERE topic_id=? AND seg_no=?', (ts, tid, seg))
                c.execute('UPDATE topics SET miss_streak=? WHERE topic_id=?',
                          (new_miss, tid))
            elif new_miss < args.offboard_misses:
                c.execute('UPDATE topics SET miss_streak=? WHERE topic_id=?',
                          (new_miss, tid))
            else:
                last = c.execute('SELECT last_seen_ts FROM topics WHERE topic_id=?',
                                 (tid,)).fetchone()[0]
                c.execute('UPDATE topic_segments SET offboard_ts=? '
                          'WHERE topic_id=? AND seg_no=?', (last, tid, seg))
                c.execute('UPDATE topics SET is_active=0,miss_streak=? '
                          'WHERE topic_id=?', (new_miss, tid))
                stat['n_off'] += 1
        c.commit()

    # ============ 2. 正文队列（仅新话题；搜索接口须节流）============
    if not args.no_post:
        q = c.execute(
            "SELECT t.topic_id,t.word,t.board_id,x.onboard_ts FROM topics t "
            "LEFT JOIN topic_posts p ON p.topic_id=t.topic_id "
            "JOIN topic_segments x ON x.topic_id=t.topic_id AND x.seg_no=1 "
            "WHERE p.topic_id IS NULL ORDER BY t.first_seen_ts DESC LIMIT ?",
            (args.max_post_per_round,)).fetchall()
        for tid, word, bid, ob_ts in q:
            try:
                cand = discover_search(f, word, args.post_candidates, pages=2,
                                       min_comments=args.min_comments,
                                       prefer_before=ob_ts)
            except SearchLimit as exc:
                notes.append(f'搜索熔断:{exc}')
                print(f'  ⏸ 搜索熔断（已抓 {stat["n_post"]} 条正文），剩余顺延下轮')
                break
            except Blocked as exc:
                notes.append(f'搜索受阻:{str(exc)[:40]}')
                break
            if not cand:
                c.execute('INSERT INTO topic_posts(topic_id,board_id,word,'
                          'prefer_fallback,crawled_at) VALUES(?,?,?,1,?)',
                          (tid, bid, word, fmt(now_cst())))
                c.commit()
                continue
            mid = cand[0][0]
            try:
                p = fetch_post(f, mid)
            except (Blocked, SearchLimit) as exc:
                notes.append(f'正文受阻:{str(exc)[:40]}')
                break
            if not p:
                continue
            c.execute('INSERT OR REPLACE INTO topic_posts(topic_id,board_id,word,'
                      'mid,text,screen_name,user_id,created_at,reposts_count,'
                      'comments_count,attitudes_count,region_name,source,pics,'
                      'prefer_fallback,crawled_at) '
                      'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)',
                      (tid, bid, word, p['mid'], p['text'], p['screen_name'],
                       p['user_id'], p['created_at'], p['reposts_count'],
                       p['comments_count'], p['attitudes_count'],
                       p['region_name'], p['source'], p['pics'], fmt(now_cst())))
            c.commit()
            stat['n_post'] += 1
            if args.verbose:
                print(f'    · 正文 [{word[:14]}] {(p["text"] or "")[:26]}')

    # ============ 3. 评论队列（在榜话题逐页累积）============
    if not args.no_comment:
        q = c.execute(
            "SELECT t.topic_id,t.word,p.mid,t.board_id,"
            "COALESCE(g.pages_done,0),COALESCE(g.exhausted,0),"
            "COALESCE(g.next_cursor,''),COALESCE(g.cursor_type,0),"
            "COALESCE((SELECT MAX(seg_no) FROM topic_segments s WHERE s.topic_id=t.topic_id),1) "
            "FROM topics t JOIN topic_posts p ON p.topic_id=t.topic_id "
            "LEFT JOIN comment_progress g ON g.topic_id=t.topic_id "
            "WHERE t.is_active=1 AND p.mid IS NOT NULL AND p.mid!='' "
            "ORDER BY t.latest_heat DESC").fetchall()
        for tid, word, mid, bid, pages, exhausted, cursor, ctype, seg in q:
            if exhausted or pages >= args.max_comment_pages:
                continue
            if f.req_round >= args.max_req_per_round:
                notes.append('请求封顶，评论队列中断，下轮继续')
                break
            try:
                got = fetch_comments_page(f, mid, cursor, ctype)
            except (Blocked, EmptyResult) as exc:
                if isinstance(exc, EmptyResult):
                    c.execute('INSERT OR REPLACE INTO comment_progress'
                              '(topic_id,seg_no,next_cursor,cursor_type,pages_done,'
                              'fetched_cnt,exhausted,last_fetch_ts) '
                              'VALUES(?,?,?,?,?,COALESCE((SELECT fetched_cnt FROM '
                              'comment_progress WHERE topic_id=?),0),1,?)',
                              (tid, seg, cursor, ctype, pages + 1, tid, fmt(now_cst())))
                    c.commit()
                else:
                    notes.append(f'评论受阻:{str(exc)[:40]}')
                    break
                continue
            items, new_cursor, new_ctype = got
            added = 0
            for cm in items:
                # cm 里 topic_id / seg_no / first_page 是占位，这里补真实值
                row = (cm[0], tid, cm[2], cm[3], cm[4], cm[5], cm[6], cm[7],
                       cm[8], cm[9], cm[10], cm[11], cm[12], seg, pages + 1,
                       cm[15])
                cur = c.execute(
                    'INSERT OR IGNORE INTO comments(comment_id,topic_id,mid,'
                    'root_id,parent_id,is_reply,floor_number,screen_name,user_id,'
                    'text,like_count,reply_count,created_at,seg_no,first_page,'
                    'crawled_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', row)
                added += cur.rowcount
            c.execute('INSERT OR REPLACE INTO comment_progress'
                      '(topic_id,seg_no,next_cursor,cursor_type,pages_done,'
                      'fetched_cnt,exhausted,last_fetch_ts) '
                      'VALUES(?,?,?,?,?,COALESCE((SELECT fetched_cnt FROM '
                      'comment_progress WHERE topic_id=?),0)+?,?,?)',
                      (tid, seg, str(new_cursor or ''), new_ctype, pages + 1,
                       tid, added, 1 if not new_cursor else 0, fmt(now_cst())))
            c.commit()
            stat['n_cmt'] += added
            if args.verbose and added:
                print(f'    · 评论 [{word[:12]}] 第{pages+1}页 +{added}')

    # ============ 4. 日志 ============
    c.execute('INSERT INTO crawl_log(ts,board_id,n_board,n_new,n_off,n_heat,'
              'n_post,n_cmt,req_used,note) VALUES(?,?,?,?,?,?,?,?,?,?)',
              (ts, None, stat['n_board'], stat['n_new'], stat['n_off'],
               stat['n_heat'], stat['n_post'], stat['n_cmt'], f.req_round,
               '; '.join(notes)[:300]))
    c.commit()
    print(f'\n== 本轮 {ts} ==')
    print(f'   榜位 {stat["n_board"]} · 新话题 {stat["n_new"]} · 下榜 {stat["n_off"]} '
          f'· 热度样本 {stat["n_heat"]} · 正文 {stat["n_post"]} · 评论 +{stat["n_cmt"]}')
    print(f'   请求 {f.req_round}/{args.max_req_per_round} · 风控 {f.throttle_hits}')
    if notes:
        print('   备注:', '; '.join(notes[:5]))
    return stat


def fetch_comments_page(f, mid, cursor='', ctype=0):
    """抓一页评论（游标推进）。返回 (rows, new_cursor, new_cursor_type)。

    带登录 Cookie 时 hotflow 的 max_id 才有效；未登录时 max_id 恒 0、
    一页约 9 条且无法翻页（这就是本设计必须依赖 cookie 的原因）。
    """
    url = (f'https://m.weibo.cn/comments/hotflow?id={mid}&mid={mid}'
           f'&max_id_type={ctype or 0}')
    if cursor:
        url += f'&max_id={cursor}'
    d = f.get_json(url, referer=f'https://m.weibo.cn/detail/{mid}')
    data = d.get('data') or {}
    items = data.get('data') or []
    if not items:
        raise EmptyResult('本页无评论')
    ts = fmt(now_cst())
    rows = []

    def mk(c, is_reply=0, root='', parent=''):
        cid = str(c.get('id') or '')
        if not cid:
            return None
        u = c.get('user') or {}
        return (cid, None, mid, root, parent, is_reply,
                c.get('floor_number'), u.get('screen_name') or '',
                str(u.get('id') or ''), clean_text(c.get('text')),
                c.get('like_count'), c.get('reply_count'),
                c.get('created_at') or '', None, None, ts)

    for c in items:
        cid = str(c.get('id') or '')
        r = mk(c, root=cid)
        if r:
            rows.append(r)
        for sub in (c.get('comments') or []):
            r2 = mk(sub, is_reply=1, root=cid, parent=cid)
            if r2:
                rows.append(r2)
    new_max = data.get('max_id')
    new_type = data.get('max_id_type')
    # max_id 与本次相同 → 接口卡住，视为到底（防死循环）
    if new_max is None or str(new_max) in ('0', '') or str(new_max) == str(cursor):
        new_max = None
    return rows, new_max, (new_type if new_type is not None else ctype)


# ------------------------------------------------------------------ 状态

def status():
    c = conn()
    print('== 榜单 ==')
    for r in c.execute('SELECT board_id,endpoint,name,board_size FROM boards'):
        print(f'   #{r[0]} {r[2]:<6} endpoint={r[1]:<14} {r[3]} 条')
    print('\n== 规模 ==')
    def q(s, a=()):
        return c.execute(s, a).fetchone()[0]
    print('  话题总数:', q('SELECT COUNT(*) FROM topics'))
    print('    追踪中:', q('SELECT COUNT(*) FROM topics WHERE is_active=1'))
    print('    已下榜:', q('SELECT COUNT(*) FROM topics WHERE is_active=0'))
    print('  复上榜话题:', q('SELECT COUNT(*) FROM topics WHERE onboard_count>1'))
    print('  上榜区段:', q('SELECT COUNT(*) FROM topic_segments'))
    print('  热度样本:', q('SELECT COUNT(*) FROM heat_samples'))
    print('  正文:', q('SELECT COUNT(*) FROM topic_posts'))
    print('  评论:', q('SELECT COUNT(*) FROM comments'))
    print('  评论进度行:', q('SELECT COUNT(*) FROM comment_progress'))
    print('  本轮请求审计:', q('SELECT COUNT(*) FROM req_audit'))
    print('\n== 各榜分布 ==')
    for r in c.execute("""SELECT b.name, COUNT(t.topic_id),
                                 SUM(CASE WHEN t.is_active=1 THEN 1 ELSE 0 END)
                          FROM boards b LEFT JOIN topics t ON t.board_id=b.board_id
                          GROUP BY b.board_id ORDER BY b.board_id"""):
        print(f'   {r[0]}: 话题 {r[1]} (在榜 {r[2]})')
    print('\n== 最近 5 轮 ==')
    for r in c.execute('SELECT ts,n_board,n_new,n_off,n_heat,n_post,n_cmt,req_used '
                       'FROM crawl_log ORDER BY ts DESC LIMIT 5'):
        print(f'   {r[0]}  榜{r[1]} 新{r[2]} 下{r[3]} 热{r[4]} 文{r[5]} 评{r[6]} 请求{r[7]}')
    c.close()


# ------------------------------------------------------------------ 主流程

def wait_network(max_wait_min=90, verbose=True):
    """等待微博可达（等用户关代理 / 加直连规则）。通了立即返回 True。"""
    from curl_cffi import requests as creq
    hdr = {'User-Agent': UA_W, 'Referer': 'https://weibo.com/hot/search',
           'Accept': 'application/json, text/plain, */*'}
    t0 = time.time()
    n = 0
    while (time.time() - t0) / 60 < max_wait_min:
        n += 1
        try:
            r = creq.get('https://weibo.com/ajax/statuses/entertainment',
                         headers=hdr, impersonate='chrome', timeout=15)
            if r.status_code == 200 and 'band_list' in (r.text or ''):
                if verbose:
                    print(f'[{fmt(now_cst())}] ✅ 微博已可达（第 {n} 次探测）')
                return True
            if verbose:
                print(f'[{fmt(now_cst())}] 探测#{n}: HTTP {r.status_code}，继续等')
        except Exception as exc:                                # noqa: BLE001
            if verbose:
                print(f'[{fmt(now_cst())}] 探测#{n}: {type(exc).__name__} '
                      f'{str(exc)[:45]}，继续等（微博仍被代理劫持？）')
        time.sleep(45)
    return False


def run_loop(args):
    if args.wait_network:
        print('等待微博可达（请关闭代理 / 或给 weibo.com 加 DIRECT 规则）…')
        if not wait_network(args.max_wait_min):
            print('!! 等待超时，微博仍不可达，退出。')
            return 3
    # 校验并写入六个分类榜
    if args.probe_again or not os.path.exists(DB_PATH):
        print('校验六个分类榜接口 …')
        ck = load_cookie()
        if not ck:
            print('!! 缺少 config/cookie.txt —— 分类榜接口需要登录态，退出。')
            return 3
        found = probe_bands(cookie=ck)
        if not found:
            print('!! 六个分类榜都取不到（检查网络/代理/Cookie），退出。')
            return 2
        c = conn()
        ensure_boards(c, found)
        c.close()
        print(f'榜单已写入（{len(found)} 个）。')

    t0 = time.time()
    # --until HH:MM 优先：把结束时间钉死在某个时刻（等待网络的时间不计入）
    if args.until:
        try:
            hh, mm = args.until.split(':')
            end = now_cst().replace(hour=int(hh), minute=int(mm), second=0,
                                    microsecond=0)
            if end <= now_cst():
                end += timedelta(days=1)
            args.loop_minutes = int((end - now_cst()).total_seconds() // 60)
            print(f'结束时刻钉在 {fmt(end)}，共 {args.loop_minutes} 分钟')
        except Exception as exc:                                # noqa: BLE001
            print('--until 解析失败，回退 --loop-minutes:', exc)
    limit_sec = args.loop_minutes * 60
    round_no = 0
    final_round = False          # 标记：正在跑"结束时刻之后的最后一轮"
    while True:
        spent = time.time() - t0
        if spent >= limit_sec:
            if final_round:
                print('\n最后一轮已完成，正常退出。')
                break
            # ── 到达结束时刻：不是停下，而是**再跑一轮** ──────────────
            # 用户口径：「9 点钟不是停止，是跑最后一轮」。所以到点后再
            # 完整跑一轮，跑完才退出。
            final_round = True
            print(f'\n>>> 已到结束时刻（{args.until or args.loop_minutes}），'
                  f'执行**最后一轮**…')
        round_no += 1
        if not final_round:
            print(f'\n########## 第 {round_no} 轮 / 已运行 {spent/60:.0f} '
                  f'分钟 / 剩余 {(limit_sec-spent)/60:.0f} 分钟 ##########')
        else:
            print(f'\n########## 第 {round_no} 轮（最后一轮）##########')
        round_start = time.time()
        try:
            tick(args)
        except Exception as exc:                                # noqa: BLE001
            import traceback
            print('!! 本轮异常:', type(exc).__name__, exc)
            traceback.print_exc()
        if final_round:
            continue            # 回到顶部 → 此时 final_round 已 True → 退出
        cost = time.time() - round_start
        # 保持 interval 分钟节拍：用"间隔 − 本轮耗时"作为休眠时长
        nap = args.interval * 60 - cost
        remain = limit_sec - (time.time() - t0)
        nap = min(nap, remain)
        if nap <= 0:
            continue
        print(f'  本轮耗时 {cost/60:.1f} 分钟，休眠 {nap/60:.1f} 分钟至下一轮…')
        time.sleep(nap)
    print(f'\n循环结束（共 {round_no} 轮，含最后一轮）。')
    return 0


def main():
    ap = argparse.ArgumentParser(description='多榜单热搜追踪器')
    ap.add_argument('--tick', action='store_true', help='跑一轮')
    ap.add_argument('--status', action='store_true', help='查看状态')
    ap.add_argument('--probe', action='store_true', help='探测六榜 band_id 并写入')
    ap.add_argument('--probe-again', action='store_true', help='每次启动重新探测榜单')
    ap.add_argument('--per-board', type=int, default=50, help='每榜取前 N 条（默认50）')
    ap.add_argument('--no-post', action='store_true', help='本轮不抓正文')
    ap.add_argument('--no-comment', action='store_true', help='本轮不抓评论')
    ap.add_argument('--max-post-per-round', type=int, default=30,
                    help='每轮最多抓多少条正文（默认30）')
    ap.add_argument('--post-candidates', type=int, default=1,
                    help='每个话题取最热的前 N 条微博当正文（默认1）')
    ap.add_argument('--min-comments', type=int, default=0,
                    help='搜索候选的最低评论数门槛（默认0）')
    ap.add_argument('--max-comment-pages', type=int, default=5,
                    help='每话题最多抓多少页评论（默认5）')
    ap.add_argument('--max-req-per-round', type=int, default=400,
                    help='每轮请求总量封顶（默认400）')
    ap.add_argument('--offboard-misses', type=int, default=2,
                    help='连续几轮不在榜判下榜（防抖，默认2）')
    ap.add_argument('--loop-minutes', type=int, default=0, help='循环总分钟数')
    ap.add_argument('--until', type=str, default='',
                    help='跑到指定时刻 HH:MM（优先于 --loop-minutes）')
    ap.add_argument('--interval', type=int, default=30, help='轮间隔分钟（默认30）')
    ap.add_argument('--wait-network', action='store_true', help='先等微博可达再开始')
    ap.add_argument('--max-wait-min', type=int, default=90, help='等待网络上限(分)')
    ap.add_argument('--quiet', action='store_true')
    args = ap.parse_args()
    args.verbose = not args.quiet

    if args.status:
        status()
        return 0
    if args.probe:
        print('校验六个分类榜接口 …')
        found = probe_bands(cookie=load_cookie())
        if found:
            c = conn()
            ensure_boards(c, found)
            c.close()
            print(f'\n已写入 {len(found)} 个榜单。')
        else:
            print('\n!! 六个分类榜都取不到。')
        return 0
    if args.tick:
        tick(args)
        return 0
    if args.loop_minutes > 0 or args.until:
        return run_loop(args)
    ap.print_help()
    return 0


if __name__ == '__main__':
    sys.exit(main())
