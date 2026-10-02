# 微博热搜追踪爬虫 · 六分类榜 / 30 分钟采样

> 每 **30 分钟**采样微博六个分类榜（文娱 / 生活 / 社会 / 体育 / 科技 / ACG），
> 记录每条话题**从上榜到下榜的完整热度轨迹**，并抓取正文与评论。
> 输出「事件表 + 热度明细 + 评论表 + 分析报告」结构化数据集。

一套用于微博热搜**纵向追踪**的 Python 采集工具。它不做"广撒网式"批量抓取，而是按固定节拍
反复采样同一批榜单，从而回答三个问题：**这条话题什么时候上的榜、在榜期间热度怎么变、什么时候下的榜。**

同时保留早期版本的**总榜生命周期追踪**模式（单榜单 / 每小时），见[第二节](#二两套采集模式)。

---

## 目录

- [一、项目背景](#一项目背景)
- [二、两套采集模式](#二两套采集模式)
- [三、实测效果](#三实测效果)
- [四、关键接口：六个分类榜](#四关键接口六个分类榜)
- [五、数据模型](#五数据模型)
- [六、核心特性](#六核心特性)
- [七、目录结构](#七目录结构)
- [八、快速开始](#八快速开始)
- [九、参数说明](#九参数说明)
- [十、数据口径与局限](#十数据口径与局限必读)
- [十一、常见问题](#十一常见问题)
- [十二、注意事项](#十二注意事项)

---

## 一、项目背景

面向"突发事件数据采集"类课程作业。要求：

- 采集**当天发生**、**上过热榜**的突发事件
- 记录事件内容、来源、发生时间、上下热榜时间
- 跟踪上热搜后的**热度变化轨迹**
- 抓取事件的**评论**（内容 + 点赞数）与**配图**

需求演进中确立了两条关键设计决策：

1. **改用六个分类榜**，而非总热搜榜——总榜变化快、内容杂，分类榜更稳定、更利于分析
2. **改用 30 分钟采样间隔**——上榜后 1 小时是热度高位窗口，1 小时采样会严重错失峰值

本仓库是其中的**微博**部分实现。

---

## 二、两套采集模式

| | **六榜追踪**（推荐 / 主力） | 单榜生命周期追踪（早期） |
|---|---|---|
| 脚本 | `multi_board_tracker.py` | `hot_tracker.py` |
| 入口 | 六个分类榜 | 总热搜榜 |
| 采样间隔 | 每 30 分钟 | 每小时 |
| 数据表 | `topics` / `topic_segments` / `heat_samples` | `tracked_events` / `heat_series` |
| 数据库 | `data/multi_boards.db` | `data/hot_tracker.db` |
| 支持复上榜留白 | ✅ 原生支持 | 部分 |
| 适用 | 追踪分类榜热度变化 | 对齐 24 列模板导出 |

> **新用户建议直接用 `multi_board_tracker.py`。** `hot_tracker.py` 保留是因为它能把结果
> 对齐到 24 列的事件模板（含配图下载），部分场景仍有价值。

---

## 三、实测效果

一次连续整夜运行（**2026-09-22 22:41 → 09-23 09:00**，共 **19 轮**，全程 **0 次风控**）：

| 指标 | 数值 |
|---|---|
| 话题（去重） | **583**（追踪中 294 / 已下榜 289） |
| **复上榜话题** | **81**（14%，最多 4 次） |
| 上榜区段总数 | 683 |
| **热度样本** | **6028**（24 个采样时刻） |
| 正文覆盖 | **583 / 583 = 100%** |
| **评论** | **25445**（一级 20961 / 二级回复 4484） |
| 接口请求 | 3279（风控信号 **0**） |

各榜话题数：文娱 164 · 体育 121 · 社会 114 · 生活 79 · ACG 55 · 科技 50

**从数据里得到的三条结论**（详见 `gen_boards_report.py` 生成的报告）：

1. **热度黄金窗口约 1 小时**——热度中位数在上榜后 0/30/60 分钟几乎持平（约 7.7 万），
   **90 分钟跌到 71%**，3 小时剩 38%，6 小时剩 25%
2. **42% 的话题"上榜即巅峰"**——首次被观测到时已是峰值，说明**采样频率直接决定数据质量**
3. **热度与评论数只有弱相关（r = 0.198）**——热度高的是"刷到即看"的资讯类，评论多的是有争议的话题；
   **分析舆论影响不能只用热度做代理变量**

> 仓库**不含采集数据**：`data/`、`dataset_boards/` 已在 `.gitignore` 中排除，运行脚本后自行生成。

---

## 四、关键接口：六个分类榜

这是本项目最关键的发现，也是踩坑最多的地方。

### 4.1 榜单端点

分类榜**不是**总榜接口的参数，而是**六条独立路径**：

| 榜单 | 接口端点 | 本轮条数 |
|---|---|---|
| 文娱 | `https://weibo.com/ajax/statuses/entertainment` | 50 |
| 生活 | `https://weibo.com/ajax/statuses/life` | 50 |
| 社会 | `https://weibo.com/ajax/statuses/social` | 50 |
| **体育** | `https://weibo.com/ajax/statuses/`**`sport`** | 50 |
| 科技 | `https://weibo.com/ajax/statuses/technology` | 30 |
| ACG | `https://weibo.com/ajax/statuses/acg` | 30 |

**合计约 260 条 / 轮。**

### 4.2 三个必须知道的坑

**① 体育是 `sport`（单数），不是 `sports`** —— 写 `sports` 直接 404。这是最初没能找全六个榜的原因。

**② 分类榜必须带登录 Cookie** —— 不带 Cookie 时接口返回 **HTTP 200 但 `band_list` 为空**，
**且不报错**。这极易被误判成"接口不存在"或"接口变更"。
（总榜 `hot_band` 则不需要 Cookie。）

**③ `hot_band?band_id=N` 是死路** —— 实测 `band_id` 参数被服务端**完全忽略**：
0~25 全部返回同一份总榜，响应内容签名完全一致。不要在这条路上浪费时间。

### 4.3 分类榜不返回上榜时间

实测**只有文娱榜**返回 `onboard_time`，其余五榜没有。因此这几榜的「上榜时间」只能取
**首次观测到它的采样时刻**，精度等于采样间隔（**±30 分钟**）。这是数据源限制，无法用技术手段改善。

### 4.4 其他接口

| 接口 | 作用 | 需登录 | 说明 |
|---|---|---|---|
| `m.weibo.cn/api/container/getIndex` | 话题搜索 → 找到最热微博 | **是** | 唯一有登录墙、也是唯一真正触发过 403 的接口 |
| `m.weibo.cn/statuses/show?id=` | 取正文 | 否 | 一次一条微博 |
| `m.weibo.cn/comments/hotflow?id=` | 热门评论（带 `max_id` 游标） | 否 | 按热度排序，支持翻页 |
| `m.weibo.cn/api/comments/show?id=` | 评论第二批 | 否 | 与上面字段重叠，互补 |

> 为什么不用 Selenium / Playwright 抓数据：浏览器要渲染整页，比直接调 JSON 接口慢一个数量级。
> 微博移动端接口是公开 JSON，能直接调就不该上浏览器（浏览器仅用于**一次性**排查接口，
> 见 `probe_ui.py`）。

---

## 五、数据模型

**核心设计：把「话题」与「上榜区段」拆成两张表**，从而让"下榜→复上榜→中间留白"成为数据的自然结果。

```
boards            六个榜单的字典
topics            一个话题一行（跨多次上榜累计）
  └ topic_segments 一次上榜一行：seg_no = 1, 2, 3 …
                     └ heat_samples   每 30 分钟一个热度样本（话题 × 采样时刻 × 热度 × 名次）
                       comment_progress 评论游标（next_cursor / pages_done / exhausted）

topic_posts       正文（最热微博的文本 / 账号 / 时间 / 配图 URL）
comments          评论（comment_id 主键，含 root_id / parent_id 回复树）
crawl_log         每轮统计（新上榜 / 下榜 / 热度 / 正文 / 评论 / 请求数）
req_audit         请求审计（每个请求写 2 行，统计时只看 status='request'）
```

**三条规则如何成为"自然结果"**：

| 需求 | 实现 |
|---|---|
| 有新增就加上 | 发现新词 → 建 `topics` 行 + 开 `seg_no=1` |
| 下榜记时间、不再监控 | 连续 2 轮不在榜 → 本段写 `offboard_ts`，停止采样 |
| **复上榜、中间留白、继续写入** | `is_active=1` → 新开 `seg_no=2`；**缺席期不写任何样本 = 天然留白** |

**评论"逐页累积去重"（双保险）**：

1. **游标推进** —— `comment_progress.next_cursor` 记录 `hotflow` 的 `max_id`，
   每轮从上次位置继续（第 1 轮取 1–20 条，第 2 轮 21–40 条…），天然不回头、不重复请求
2. **主键去重** —— `comments.comment_id` 为主键 + `INSERT OR IGNORE`，
   即使接口返回重叠或话题复上榜，也不会产生重复行

每话题上限 `--max-comment-pages`（默认 5 页 ≈ 100 条），翻完即置 `exhausted=1` 停止。

---

## 六、核心特性

| 特性 | 说明 |
|---|---|
| **反爬绕过** | `curl_cffi` 伪装 Chrome 的 **TLS 指纹**——不是"加个 User-Agent"那么表层，这是能请求通的前提 |
| **分接口限速** | 榜单 1.0–1.6s / 正文 1.5–3.0s / 评论 1.5–3.0s / **搜索 6–10s（最保守）** |
| **连续搜索熔断** | 连续搜索超过 **8 次**即暂停（血的教训：曾连续 54 次搜索被 403） |
| **每轮请求封顶** | 默认 **400 次/轮**，保证 30 分钟节拍不漂移（节拍乱了热度采样就错位） |
| **分段留白** | 复上榜自动新开区段，缺席期不写样本——不用错误的值填充 |
| **评论游标去重** | 游标推进 + `comment_id` 主键，双重保证不重复 |
| **自愈守护** | `watchdog.py` 纯 Python 轮询，进程死/卡住自动重启，**不依赖模型配额** |
| **定时收工** | `--until 09:00` 钉住结束时刻，且**到点会跑完最后一轮**才退出 |
| **一键导出** | 事件表（含首次/峰值/最新热度 + 变化率）+ 热度明细长表 + 评论表 + 3 张图 |
| **自动出报告** | `gen_boards_report.py` 生成 Markdown 报告；`gen_report_html.py` 可转 Word |

---

## 七、目录结构

```
weibo-hotsearch-tracker/
├── README.md
├── requirements.txt
├── .gitignore
├── agent-browser.example.json       # 浏览器登录配置示例
├── config/
│   ├── cookie.example.txt           # Cookie 模板（真实 Cookie 不要提交）
│   ├── proxies.example.txt          # 代理模板
│   └── domain_keywords.txt          # 领域补充词表（可选）
├── scripts/
│   ├── multi_board_tracker.py       # ★ 六榜 30 分钟采集主体
│   ├── export_boards.py             # ★ 导出事件表 / 热度明细 / 评论表 + 图表
│   ├── gen_boards_report.py         # ★ 生成分析报告 + 热度衰减曲线
│   ├── gen_report_html.py           # ★ 报告 Markdown → 规范 HTML（可再转 Word）
│   ├── watchdog.py                  # ★ 自愈守护（异常自动重启）
│   ├── probe_bands.py               # 分类榜接口 / 评论游标探测（含负结果记录）
│   ├── probe_ui.py                  # 用真实浏览器排查接口（一次性工具）
│   ├── hot_tracker.py               # 单榜生命周期追踪（早期模式）
│   ├── export_tracker.py            # 导出 24 列模板格式（含配图下载）
│   ├── gen_report.py                # 单榜模式的统计报告
│   ├── event_crawler.py             # 底层采集库（HTTP 封装 / 搜索 / 正文 / 评论）
│   ├── safety.py                    # 安全护栏（限速 / 熔断 / 审计）
│   ├── login_then_crawl.py          # 一条龙：开浏览器 → 登录 → 导 Cookie → 采集
│   ├── decrypt_profile_cookies.py   # 从浏览器 profile 解密 Cookie
│   └── probe_weibo.py               # 连通性体检（只发 2 次请求）
└── dataset_template/                # 目标数据模板（24 列格式参考）
```

---

## 八、快速开始

### 第 1 步 · 安装依赖

```bash
git clone https://github.com/xingchenyd/weibo-hotsearch-tracker.git
cd weibo-hotsearch-tracker

python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

> 若 `pip install` 报"找不到任何版本"，多半是本机 pip 用了不可达的镜像源，
> 显式指定官方源即可：`pip install -r requirements.txt -i https://pypi.org/simple`

### 第 2 步 · 配置 Cookie（**六榜必需**）

分类榜接口**必须带登录 Cookie**，否则返回空列表且不报错。方式二选一：

**方式一（推荐，不落盘）：**

```bash
# Windows PowerShell
$env:WEIBO_COOKIE="SUB=你的值; SUBP=你的值; SSOLoginState=你的值"

# Git Bash / macOS / Linux
export WEIBO_COOKIE="SUB=你的值; SUBP=你的值; SSOLoginState=你的值"
```

**方式二（写入文件，注意别提交）：**

```bash
cp config/cookie.example.txt config/cookie.txt
# 编辑 config/cookie.txt，填入真实 Cookie
```

**怎么取 Cookie**：浏览器登录 `weibo.com` → `F12` → Network → 刷新页面 →
点任意请求 → Headers → 复制 `Cookie:` 后面整行。

> 一键方案：`python scripts/login_then_crawl.py` 会弹出浏览器窗口，
> 你只需手动登录（建议扫码），之后自动导出 Cookie 并体检接口。

### 第 3 步 · 校验六个榜单（**建议先跑，10 秒**）

```bash
python scripts/multi_board_tracker.py --probe
```

期望输出六行 `✓ 文娱 (entertainment) 50 条` 之类。
**若全部为空**，说明 Cookie 没配好——分类榜不会报错，只会静默返回空。

### 第 4 步 · 跑一轮试水（约 15 分钟）

```bash
python scripts/multi_board_tracker.py --tick \
    --per-board 50 --max-post-per-round 30 --max-comment-pages 5
```

### 第 5 步 · 整夜连续采集

```bash
# 每 30 分钟一轮，跑到次日 09:00（到点会跑完最后一轮再退出）
python scripts/multi_board_tracker.py --until 09:00 --interval 30 \
    --per-board 50 --max-post-per-round 30 --max-comment-pages 5 \
    --max-req-per-round 400 >> logs/night_run.log 2>&1
```

**建议同时启动守护**（另开一个终端 / 后台任务），异常会自动重启：

```bash
python scripts/watchdog.py >> logs/watchdog_stdout.log 2>&1
```

守护每 **5 分钟**检查一次，判据为「日志是否在增长」+「库里最新采样时刻是否推进」；
两项停滞超过 **45 分钟**才判定卡死并重启（避免误杀正常的长轮次）。
守护默认在 **09:40** 退出——比采集晚，确保最后一轮有人看护。

### 第 6 步 · 导出数据集

```bash
python scripts/export_boards.py
```

产物在 `dataset_boards/六榜数据集/`：

| 文件 | 内容 |
|---|---|
| `事件表.xlsx` | 每话题一行：六榜话题 + 首次/峰值/最新热度 + **热度变化率** |
| `热度明细.xlsx` | 长表：话题 × 采样时刻 × 热度 × 榜内排名（**直接可画曲线**） |
| `事件评论信息.xlsx` | 评论内容 + 点赞数 + 一级/二级标识 |
| `热度曲线_Top10.png` | 热度 Top10 话题的时间序列（绝对时间轴） |
| `榜单对比.png` | 各榜话题数 / 峰值热度 / 评论量对比 |
| `热度与评论分布.png` | 峰值热度与评论数的分布关系 |

### 第 7 步 · 生成分析报告

```bash
python scripts/gen_boards_report.py    # → 分析报告.md + 热度衰减曲线.png
python scripts/gen_report_html.py      # → 分析报告.html（排版规范，图片已内嵌）
```

HTML 报告本身可直接用浏览器打开阅读。如需 **Word 版**（便于打印提交），
再执行下面这段（依赖 `html-for-docx`，已在 `requirements.txt` 中）：

```bash
python - <<'PY'
import docx, html4docx
src = 'dataset_boards/六榜数据集/分析报告.html'
dst = 'dataset_boards/六榜数据集/分析报告.docx'
html = open(src, encoding='utf-8').read()
doc = docx.Document()
html4docx.HtmlToDocx().add_html_to_document(html, doc)
doc.save(dst)
print('已生成', dst)
PY
```

> 图片之所以能在 Word 里正常显示，是因为 `gen_report_html.py` 把图**以 base64 内嵌**进 HTML，
> 而不是引用外部路径——转换器读 `data:image/...` 即可，不依赖文件位置。

---

## 九、参数说明

### `multi_board_tracker.py`

```bash
python scripts/multi_board_tracker.py --status        # 查看当前数据规模
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--tick` | — | 跑一轮（适合外部定时任务调用） |
| `--status` | — | 只打印当前库状态，不采集 |
| `--probe` | — | 校验六个榜单端点（不采集） |
| `--probe-again` | — | 每次启动重新校验榜单 |
| `--per-board N` | 50 | 每榜取前 N 条 |
| `--max-post-per-round N` | 30 | 每轮最多为几条话题抓正文 |
| `--post-candidates N` | 1 | 每个话题取最热的前 N 条微博当正文 |
| `--min-comments N` | 0 | 搜索候选的最低评论数门槛 |
| `--max-comment-pages N` | 5 | 每话题最多抓几页评论（1 页 ≈ 20 条） |
| `--max-req-per-round N` | 400 | 每轮请求总量封顶（保节拍） |
| `--offboard-misses N` | 2 | 连续 N 轮不在榜才判下榜（防榜单抖动） |
| `--interval N` | 30 | 轮间隔（分钟） |
| `--until HH:MM` | — | 跑到指定时刻（**优先于** `--loop-minutes`） |
| `--loop-minutes N` | 0 | 循环总时长（0 = 只跑一轮） |
| `--wait-network` | — | 先等微博可达再开始（代理没关时很有用） |
| `--max-wait-min N` | 90 | 等待网络的上限（分钟） |
| `--no-post` / `--no-comment` | — | 本轮不抓正文 / 不抓评论 |

> ⚠️ **九点不是"停止"，是"跑最后一轮"。** `--until 09:00` 的语义是到点后再跑完整一轮才退出，
> 因此实际结束时间约为 09:16。守护脚本的退出时刻相应设为 09:40。

### `export_boards.py` / `gen_boards_report.py`

无必需参数。二者都需要 **matplotlib**；若主环境未装，用任意装有 matplotlib 的解释器运行即可。

### `watchdog.py`

无参数。常量在脚本顶部：`CHECK_INTERVAL=300`（秒）、`STALE_LIMIT_MIN=45`、`END_HHMM=(9, 40)`。

---

## 十、数据口径与局限（必读）

### 10.1 热度值的口径

"热度"取自榜单条目的 `num` 字段，即**微博热搜榜显示的热度值**（数十万~百万量级）。

微博**不对外提供**"话题阅读量"接口——已实测话题页、搜索页、热门话题容器等超过 10 个端点均无。
故以榜面热度值作为"浏览量口径"的替代，与榜面显示一致。

> 补充一句：微博详情接口里有个 `number_display_strategy`，其值是显示文本「100万+」。
> 那是**展示规则**（超过 100 万就显示成 100万+），**不是可读取的真实数字**，不能当阅读量用。

### 10.2 上榜时间的精度

- **文娱榜**：使用接口返回的 `onboard_time`
- **其余五榜**：接口不返回，取**我们首次观测到它的采样时刻**，精度 = 采样间隔（**±30 分钟**）

另外实测发现，`onboard_time` 实际是**榜单的批次刷新时刻**，不是逐话题独立的秒级时刻：

| 观察 | 数据 |
|---|---|
| **分钟可信** | 144/163（**88%**）落在「上一轮采样 < 上榜时间 ≤ 首次观测到它」的合理区间内 |
| **秒不可靠** | "秒"随时间单调递增（13→14→…→24）；真实时钟的秒不可能单调递增 |
| **同批共享** | 47/163（**28.8%**）与其他话题的上榜时间完全相同 |

这是**数据源特性，不是抓取错误**。想缩小"真实上榜"与"我们首次发现"的滞后，
唯一手段是**提高采样频率**。

### 10.3 下榜时间的精度

采样是离散的，无法知道话题在哪一秒掉榜，因此保留**区间**：

```
真实下榜时刻 ∈ ( offboard_ts , offboard_upper_ts ]
                 最后一次确认在榜        首次确认"已不在榜"
```

导出取**上界**。原因：若取下界，只被采样到一轮的话题（163 条里曾占 59 条）会显示成
"上榜 18 秒就下榜"——那是观测假象，会严重低估话题寿命。取上界误差 ≤ 一个采样间隔。

### 10.4 采样密度的固有盲区

30 分钟间隔意味着：**在两次采样之间"上榜又下榜"的极短命话题会被漏掉**。
热搜存在大量几分钟到十几分钟寿命的话题，这是**采样方式的固有盲区**。
要缩小它只能提高采样频率，代价是请求量与风控风险上升。

### 10.5 微博不提供历史热搜（**最重要的一条**）

微博**不提供**任意历史时刻的榜单数据：`hot_band?date=` 参数被服务端忽略、
`hotSearchHistory` 接口无权限、榜单无分页。

**推论：没采到的时刻无法事后回补。采样密度是唯一的捕捉能力。**
本项目曾因调度中断产生过几小时的缺口，实测确认永久无法补回——因此才需要
`watchdog.py` 这样的守护机制，以及 `--wait-network` 这样的前置等待。

### 10.6 抓取合规

整夜 19 轮共 3279 次请求，**风控信号 0 次**。措施：分接口限速、连续搜索熔断（>8 次暂停）、
每轮请求封顶（400）、进程级串行（**不使用并发**——并发只推高瞬时速率，直接撞风控红线）。

---

## 十一、常见问题

**Q：`--probe` 显示六个榜单全是空的，但接口返回 HTTP 200？**
A：几乎一定是 **Cookie 没配好**。分类榜在无登录态下返回 200 且 `band_list` 为空、**不报错**。
请按第八节第 2 步配置 Cookie。

**Q：体育榜 404？**
A：端点是 **`sport`（单数）**，不是 `sports`。

**Q：报 `SSL_ERROR_SYSCALL` / `Connection closed abruptly`，所有微博域名都连不上？**
A：大概率是 **VPN / 代理劫持了微博的 TLS 握手**，不是封号。
诊断方法：探测一个中立网站（如 `example.com`）——若它正常、只有微博失败，即为代理问题。
解决：关闭代理，或在代理软件里把 `weibo.com` / `weibo.cn` / `sina.com.cn`
设为**直连（DIRECT）**。注意 TUN 模式下应用层绕不过，必须改分流规则。

**Q：搜索接口返回 HTTP 432 / 403？**
A：432 通常是"需要登录"；403 往往是**连续搜索次数过多**触发风控。
本项目已内置"交替模式 + 连续搜索熔断"，**请勿关闭，也不要提高并发或缩短间隔**。

**Q：热度值为空是抓取失败吗？**
A：不一定。请区分三态：`offboard`（话题已下榜）和 `missed`（漏采）都是**有意留空**。
只有确认话题当时在榜、且偏差超限时才可能是漏采。

**Q：请求额度怎么算？**
A：审计库中每条请求写 2 行（`request` + `HTTP200`），
统计请求数**只看 `status='request'`**，否则会翻倍。

**Q：跑了一晚上，为什么没有 24h / 48h 的热度？**
A：因为热搜**话题寿命很短**——六榜实测在榜时长中位数约 **139 分钟**（早期总榜实测中位数仅 28 分钟）。
多数话题活不到那么久，到点时已下榜（留空）。这是数据源特性，不是采集失败。

**Q：装 `html-for-docx` 后报 `cannot import name 'HtmlToDocx'`？**
A：包名与导入名不同——**包名是 `html-for-docx`，导入名才是 `html4docx`**。
误装 PyPI 上同名的 `html4docx` 包就会报这个错。

---

## 十二、注意事项

1. **本工具仅供学习与技术研究使用**，请遵守目标网站的 `robots.txt` 与服务条款。
2. **切勿提交真实 Cookie / 代理账号密码**到任何公开仓库；本仓库 `.gitignore` 已排除相关文件。
3. 建议使用**不常用的小号**进行采集，避免影响主账号。
4. 采集请**控制频率**、设置熔断保护。触发风控后应立即停止，而非重试。
5. **不要使用并发/多线程**。瓶颈是账号级风控，并发只会加速被封。
6. 未经授权，**不得将采集到的用户评论等数据用于商业用途或公开发布**。

---

## License

仅供学习交流使用。数据版权归原网站及内容作者所有。
