# QQ Bot Saver

通过 NapCatQQ 监听 QQ 消息，自动保存文本、图片、视频和文件到本地磁盘。

## 文件结构

```
D:\opencword\bot\
├── bot.py              # 主逻辑：WS 连接、API 调用、事件队列流水线、main
├── config.py           # 配置加载（config.json + 环境变量覆盖 token）
├── db.py               # SQLite 持久层：事件队列、消息/媒体索引、FTS5 全文检索
├── storage.py          # 落盘层：文本/图片/视频/文件，MD5 去重，扩展名推断
├── forward.py          # 合并转发：递归展开（最多 3 层），媒体单次下载
├── magnets.py          # 磁力链接登记表：新链接按 btih 比对 xlsx 主表，缺则补行
├── search.py           # 命令行全文检索工具
├── replay.py           # 队列状态查看 / error、dead 项重置重放
├── backup.py           # 每日备份 bot.db/xlsx/近 3 天消息 md（计划任务 03:30）
├── test_bot.py         # 单元测试（标准库 unittest，零依赖）：python test_bot.py
├── config.json         # 配置（WebSocket 地址、Token、数据目录）— 被 .gitignore 忽略
├── config.example.json # 配置模板
├── start.bat           # 一键启动脚本（隐藏 NapCat + 无控制台拉起 bot）
├── start_hidden.vbs    # 无黑框入口，双击它而不是 start.bat
├── stop.bat            # 停止 bot + NapCat + QQ
├── napcat/             # NapCatQQ Shell 副本
└── README.md
```
D:\QQBot_Data\
├── messages\YYYY-MM-DD.md          # 所有消息文本（按日分文件，导出视图）
├── videos\                         # 全部视频（不分私聊/群，统一目录，按 MD5 命名）
├── bot.db                          # SQLite 索引库（事件队列 + 消息/媒体元数据）
├── 磁力链接汇总.xlsx                # 磁力登记表（bot 增量追加「磁力链接(去重)」主表）
├── extract_magnets.py              # 离线全量重扫 + 磁力解析规则（bot 动态加载）
├── private\                        # 私聊媒体，按类型分流
│   ├── images\  files\             # 按 MD5 命名
└── group\                          # 群聊媒体，按类型分流
    ├── images\  files\             # 按 MD5 命名
```

> 文本统一写入 `messages/YYYY-MM-DD.md`，群消息以 `[群号/昵称]` 前缀区分发言人；图片按来源分流到 `private/`、`group/`，视频统一存顶层 `videos/`（不分来源，历史文件在启动时自动并入）。
> **历史目录说明**：`D:\QQBot_Data` 下的顶层 `groups/`（若在）是早期版本留下的历史存档，**请勿删除**；顶层 `videos/` 是当前版本的视频统一存储，不是遗留物。`private/videos/`、`group/videos/` 是统一前的旧位置，残留文件会在启动时自动搬入 `videos/`。


## 可靠性架构（asyncio + SQLite 队列）

消息处理管道分三层，核心原则：**先落库、后处理**——WS 收到的每条原始事件先写入
`bot.db` 的 `events` 表（WAL 模式），之后任何环节崩溃/超时都不丢消息：

1. **events 事件队列**：`new -> busy -> done`；处理异常转 `error` 按指数退避自动重试，
   穷尽（6 次）转 `dead`。进程重启时遗留的 `busy` 自动回队，未处理积压自动重放。
   `done` 事件保留 30 天后启动时自动清理，bot.db 不再无限膨胀。
2. **顺序消费**：单 worker 按序处理文本并写 md 视图 + `messages` 表（每消息段一行，
   含会话/发送人/时间/内容，供检索）。
3. **dl 下载重试队列**：图片/视频/文件/合并转发展开/引用回查登记为 `pending`，即时尝试一次；
   失败（如 NapCat `get_file` 超时、CDN url 过期）由 sweeper 每 20s 扫描、指数退避
   自动重做，最多 8 次转 `dead`。图片/视频失败后还会自动改走 `file_id -> get_file`
   取本地原件的兜底路径。
4. **停机补拉**：WS 连接建立/重连后，经 NapCat 历史消息 API（`get_group_msg_history` /
   `get_friend_msg_history`）把最近 14 天活跃会话的漫游历史拉回来，没归档过的走同一
   管道补录——bot 不在线/断线窗口的消息不再永久丢失。去重按新增的 `msg_id` 列精确
   匹配，老数据按「会话+发送人+秒」兜底；从新往旧扫，连续 30 条已知即停；进程不重启
   也每 6 小时补一轮。
5. **磁力链接登记**：`messages` 里落库一条含 `magnet:` 的消息（含合并转发展开、引用回查
   回填的正文）会立即唤醒 sweeper 同步一轮，不等定时班车。用 `extract_magnets.py` 的同一套
   解析规则抽出 btih，与 `磁力链接汇总.xlsx` 主表比对，没有的特征码追加一行。游标只在写表
   成功后推进、btih 内存去重，所以重复转发/大小写不同的同一条链接不会重复成行；Excel 开着
   该文件时先攒在内存，下一轮再写，一条不丢。

检索：`messages_fts`（FTS5 trigram 分词，中文子串免分词）+ `media`（MD5 内容登记）。
除 `openpyxl`（磁力登记表读写）外无新增第三方依赖（sqlite3 为 Python 标准库）。

```bash
# 全文检索（bot 运行中也可用）
python search.py 关键词
python search.py 关键词 --scope group --limit 100
python search.py --file abcd1234          # 按 MD5 前缀查媒体落点

# 查看队列状态；把 error/dead 项重置回队列重做
python replay.py
python replay.py --retry
```


## 前置条件

- Windows 系统
- Node.js（NapCatQQ 需要）
- Python 3.8+（脚本需要）
- 一个 QQ 小号（用于登录 NapCat）

### Python 依赖

```bash
pip install aiohttp openpyxl
```

## 配置

编辑 `config.json`：

```json
{
  "data_dir":  "D:\\QQBot_Data",
  "ws_url":  "ws://127.0.0.1:9501/",
  "api_url":  "http://127.0.0.1:9502",
  "ws_token":  "在此填写WebSocket的token",
  "magnet_xlsx":  "D:\\QQBot_Data\\磁力链接汇总.xlsx",
  "magnet_rules":  "D:\\QQBot_Data\\extract_magnets.py"
}
```

`magnet_xlsx` / `magnet_rules` 可省略，默认取 `data_dir` 下的同名文件；两者任一缺失或主表
表头认不出「特征码」列时，只是不再登记磁力链接，消息存档不受影响（看 `bot.log` 的 `[MAGNET]`）。

### NapCat 配置

NapCat Shell 的 OneBot 配置文件位于：
`napcat/config/onebot11.json`（此目录下的副本）

或实际的 NapCat 运行目录：
`D:\tool\NapCat.Shell.Windows.OneKey\NapCat.44498.Shell\config\onebot11.json`

关键配置项：

```json
{
  "network": {
    "websocketServers": [{
    "host": "127.0.0.1",
    "port": 9501,
    "messagePostFormat": "array",
    "token": "在此填写WebSocket的token"
    }],
    "httpServers": []
  },
  "enableLocalFile2Url": true
}
```

- `websocketServers`：必须启用并配置端口和 token。**端口必须与 `config.json` 的 `ws_url` 一致**（当前均为 `9501`）
- `messagePostFormat`：必须为 `"array"`（否则脚本无法解析）
- `httpServers`：可选，启用后可支持 `get_file` API
- `enableLocalFile2Url`：建议 `true`

## 使用

### 启动

双击 `start_hidden.vbs`（全程无黑框），或双击 `start.bat`（会留一个约 30 秒的启动窗口，便于看过程）。
两者都会：隐藏启动 NapCat → 轮询 WS 端口就绪（最多 90 秒，快于旧的固定 25 秒）→ 用 `pythonw.exe` 拉起 bot（无控制台窗口）。

手动执行等价于：

```bash
# 1. 隐藏启动 NapCatQQ
powershell -Command "Start-Process -WindowStyle Hidden -WorkingDirectory 'D:\tool\NapCat.Shell.Windows.OneKey\NapCat.44498.Shell' -FilePath 'D:\tool\NapCat.Shell.Windows.OneKey\NapCat.44498.Shell\NapCatWinBootMain.exe'"

# 2. 等待 QQ 登录（约 15-20 秒）

# 3. 无控制台启动保存脚本
start D:\Python311\pythonw.exe D:\opencword\bot\bot.py
```

没有窗口 = 看不到实时输出，运行状态一律看 `D:\QQBot_Data\bot.log`（未捕获异常以 `[FATAL]` 记录在那里）；
启动失败/进程没活下来的话，`D:\QQBot_Data\startup.log` 会有一行 `WARN no bot.py alive after launch`。

QQ 会话过期需要重新扫码时，先跑 `stop.bat`，再双击 NapCat 目录下的 `napcat.bat`（可见窗口）扫码，
登录成功后换回 `start_hidden.vbs`。

登录成功后，向机器人 QQ 号发送消息即可自动保存。

### 停止

`stop.bat` 停止 bot + NapCat + QQ（建议管理员运行）。
或手动结束 QQ.exe、NapCatWinBootMain.exe、pythonw.exe 进程。

### 备份

已注册每日 03:30 计划任务 `QQBot_Backup`，运行 `backup.py`：把 `bot.db`（SQLite
backup API 一致快照，bot 运行中也可取）、`磁力链接汇总.xlsx`、近 3 天的消息 md
拷到 `C:\QQBot_Backup\<日期>\`，保留 14 天；运行日志在 `C:\QQBot_Backup\backup.log`。
换目的地改 `backup.py` 顶部的 `BACKUP_DIR`；重建任务的命令：

```bash
schtasks /create /tn QQBot_Backup /tr "D:\Python311\python.exe D:\opencword\bot\backup.py" /sc daily /st 03:30 /f
```

## 支持的 QQ 消息

**私聊**：全量保存（文本前缀 `[昵称(QQ号)]`），媒体入 `private/`。

**群聊**：图片、视频、文件、合并转发落盘（媒体入 `group/`）；文本同样写入统一日记，前缀 `[群号/昵称]`。

| 类型 | 私聊 | 群聊 | 去重 |
|------|------|------|------|
| 文本 | .md 文件（带 [群号/昵称] 前缀） | .md 文件（带 [群号/昵称] 前缀） | — |
| 图片 | 按 MD5 命名下载 | 按 MD5 命名下载 | 全量 md5 去重，删过的不再重拉 |
| 表情包 | 记 `[表情包]` 占位，不下载（面板/收藏/商城表情及一切 GIF） | 同左 | — |
| 视频 | 按 MD5 命名下载（统一入 videos\） | 按 MD5 命名下载（统一入 videos\） | 全量 md5 去重，删过的不再重拉 |
| 文件 | 按 MD5 命名下载 | 按 MD5 命名下载 | 同名跳过（需 NapCat 支持 `get_file`） |
| 合并转发 | 递归展开内容（最多 3 层）写 .md | 递归展开内容（最多 3 层）写 .md | - |
| 回复（引用） | `get_msg` 取回原文，写 `[回复←昵称：原文]` | 同左 | 原文里的媒体仅留标签，不额外下载 |

## 安全说明

- `config.json` 含明文 `ws_token`，**已被 `.gitignore` 忽略，切勿提交**。
- 支持用环境变量覆盖任意配置（前缀 `QQBOT_`）：
  `QQBOT_DATA_DIR`、`QQBOT_WS_URL`、`QQBOT_WS_TOKEN`、`QQBOT_API_URL`、
  `QQBOT_MAGNET_XLSX`、`QQBOT_MAGNET_RULES`。
  例如 `set QQBOT_WS_TOKEN=你的token` 会覆盖 `config.json` 中的同名值。
- `config.example.json` 为占位模板，不含真实凭据。

## 已知限制

- **文件下载**：NapCat 的 `get_file` API 可能超时——下载失败不再丢消息，进入 dl 重试队列自动重做（约 8 次、总覆盖 40 分钟），仍失败转 dead，可 `replay.py --retry` 手动重放
- **嵌套合并转发**：NapCat 无法通过 API 展开内层合并转发，但可通过消息体中的 `content` 字段直接提取内容
- **引用消息（回复）**：事件里只有被引用消息的 id，需回查 `get_msg`；原消息被撤回或超出漫游范围时取不到，退避重试穷尽后 md 只留 `[回复]`（DB 行 `dl=dead`，可 `replay.py --retry` 重放）
- **磁力登记表**：写入列按主表**表头文字**定位（`序号/特征码(btih)/资源名称(dn)/标题/大小/磁力链接/备注`…），所以手工删过列的文件也能对齐；但若把「特征码」列改名或删掉，登记会自动停用（只影响登记，不影响存档）。实时补的行 `出现次数` 恒为 1，需要全量口径时再跑一次 `python D:\QQBot_Data\extract_magnets.py` 重算
- **管理员权限**：NapCat 启动的 QQ 进程无法被普通用户级别的 `taskkill` 终止
- **不支持的消息类型**：语音（voice）、位置分享、红包、卡片消息等仅记录占位符

## 技术栈

- **NapCatQQ Shell** — QQ 协议实现，对外暴露 OneBot 11 WebSocket 接口
- **Python asyncio** — 单事件循环：WebSocket 连接（心跳+自动重连）、消息处理、媒体下载
- **aiohttp** — WebSocket 客户端与 HTTP 文件下载
- **openpyxl** — 磁力登记表（xlsx）增量读写
- **SQLite (WAL + FTS5 trigram)** — 事件持久队列、消息/媒体索引、全文检索（标准库，无额外依赖）
