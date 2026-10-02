# -*- coding: utf-8 -*-
"""
用真实浏览器打开微博热搜页，找出「文娱/生活/社会/同城/体育/科技」六个分类榜的入口。

背景：hot_band / hotSearch 的 band_id 等参数被服务端完全忽略（19 个值返回同一份总榜），
网页版公开 JSON 接口取不到分类榜。只能在真实浏览器里看页面结构与网络请求。

做法：
  1. 打开 https://weibo.com/hot/search
  2. 快照页面文本 → 看是否存在分类 tab
  3. 截图 → 人工可读
  4. 用 JS 遍历页面里出现过的接口 URL（performance entries）
  5. 关闭浏览器

输出：logs/probe_ui.log（逐条 flush，便于随时查看）
"""
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, 'logs', 'probe_ui.log')

# agent-browser 的定位：优先环境变量，其次 PATH，最后按常见安装位置兜底。
# （不要写死本机绝对路径 —— 换台机器就失效，且会泄露用户名。）
AB = (os.environ.get('AGENT_BROWSER')
      or shutil.which('agent-browser')
      or shutil.which('agent-browser.cmd')
      or '')
NODE_DIR = os.environ.get('AGENT_BROWSER_NODE') or (
    os.path.dirname(AB) if AB else '')
if not AB:
    print('未找到 agent-browser，请设置环境变量 AGENT_BROWSER 指向其可执行文件。',
          flush=True)
    sys.exit(2)

os.makedirs(os.path.dirname(LOG), exist_ok=True)
_fh = open(LOG, 'w', encoding='utf-8')


def log(*a):
    s = ' '.join(str(x) for x in a)
    print(s)
    _fh.write(s + '\n')
    _fh.flush()


def run(args, timeout=300, cwd=ROOT):
    env = dict(os.environ)
    env['PATH'] = NODE_DIR + os.pathsep + env.get('PATH', '')
    cmd = f'"{AB}" {args}'
    log(f'\n$ agent-browser {args}')
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           encoding='utf-8', errors='ignore',
                           timeout=timeout, env=env, cwd=cwd)
        out = (r.stdout or '').strip()
        err = (r.stderr or '').strip()
        log(f'  rc={r.returncode}')
        if out:
            log(out[-3000:])
        if err:
            log('  STDERR: ' + err[-1000:])
        return r.returncode, out
    except subprocess.TimeoutExpired:
        log(f'  (超时 {timeout}s)')
        return -1, ''
    except Exception as e:                                        # noqa: BLE001
        log(f'  ERR {type(e).__name__}: {e}')
        return -2, ''


def main():
    log('=== 微博分类榜入口探测（浏览器）===')
    log('时间:', time.strftime('%Y-%m-%d %H:%M:%S'))

    run('--version', timeout=120)

    # 1) 打开热搜页
    run('open https://weibo.com/hot/search', timeout=300)

    # 2) 等页面加载（可能超时，降级跳过）
    run('wait --load load', timeout=120)

    # 3) 快照：看页面有没有分类 tab 的文字
    rc, snap = run('snapshot', timeout=180)
    for kw in ('文娱', '生活', '社会', '同城', '体育', '科技', '热搜榜', '要闻'):
        if kw in snap:
            log(f'  ★ 页面快照里出现关键词: {kw}')

    # 4) 看页面加载过的接口 URL（JS 执行）
    js = ("JSON.stringify(performance.getEntriesByType('resource')"
          ".map(e=>e.name).filter(u=>/ajax|api|band|hot|search/i.test(u)))")
    run(f'eval "{js}"', timeout=120)

    # 5) 截图
    shot = os.path.join(ROOT, 'logs', 'weibo_hot_page.png')
    run(f'screenshot "{shot}"', timeout=180)

    run('close', timeout=120)
    log('\n=== 完成 ===')
    _fh.close()


if __name__ == '__main__':
    sys.exit(main())
