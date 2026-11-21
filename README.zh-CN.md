# redhand

[![CI](https://github.com/Yuh302331-ctrl/redhand/actions/workflows/ci.yml/badge.svg)](https://github.com/Yuh302331-ctrl/redhand/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/redhand)](https://pypi.org/project/redhand/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**当场抓获文件改动。** 一个跨 AI 代理的文件夹改动日志：这个文件是谁改的、什么时候、改了什么——不满意就一键还原。

[English](README.md)

---

## 痛点

你同时跑着好几个 AI 编码代理——Claude Code、Codex、Cursor、Aider、Gemini CLI……有时它们还在同一个文件夹里干活。然后某一天：

- 一个你没碰过的文件变了。**是谁改的？** 代理自己的日志只覆盖它自己的会话，`git log` 只覆盖已提交的内容，而你在意的很多文件夹（笔记、同步目录、导出产物、配置文件）根本不是 git 仓库。
- 代理把你关心的文件改坏了。**怎么还原？** 时间机器最快也是每小时一档；`git checkout` 需要一个从未存在过的提交。

redhand 在**文件夹层面**、对**所有操作者**、**持续地**回答这两个问题。

## 它做什么

```console
$ redhand watch
redhand watching /tmp/rhdemo  (interval 2s, ctrl-c to stop)
first run: scanning and seeding shadow…
seeded 4 files in 0.0s (4 restorable, 0 hash-only, 0 B over cap)
17:24:10   modified  app.py (+7 B)  codex ✓
17:24:10    created  feature.py  codex ✓
17:24:10   modified  secrets/key.txt (-2 B)  codex ✓
  ⚠ protected path: secrets/key.txt (codex) — enforcing…
  ✓ reverted secrets/key.txt
17:24:12    deleted  feature.py  codex ✓

session: 5s · 12 events · 1 touches · 1 protected · 1 reverted
```

- **改动日志**——以 *shadow*（影子：每个文件的最后已知状态）为基线记录每一次创建/修改/删除，跨重启也能接上，会话之间不会有遗漏。
- **归属判定**——监控进程表并核实工作目录，把改动归到具体 AI 代理（或人工）。三级诚实的置信度（见下文）。
- **还原**——限额内的文本文件都有内容快照。统一的语义：`restore <event>` 把文件放回**该事件发生前一刻**的状态。撤销链完整——撤销"还原事件"等于重做改动。
- **保护模式**——配置 `secrets/`、`*.key` 这类模式；受保护路径一旦被改立即告警，`watch --enforce` 还会自动回滚。
- **摘要**——`redhand digest` 产出 Markdown 报告：谁在什么时候改了什么。
- **存储克制**——快照 zlib 压缩、按内容寻址（相同字节只存一份），单文件与全局双上限 + FIFO 逐出，`forget` 可修剪。典型项目日志只有几十 KB。

零运行时依赖。Python 3.9+。macOS、Linux 和 Windows。

## 安装

```console
$ pipx install redhand        # 或 uv tool install redhand
$ pip install redhand         # 也可以
```

## 快速上手

```console
$ cd your-project
$ redhand init                # 写入 .whodunit/config.json
$ redhand watch               # 开始记录（Ctrl-C 停止）

$ redhand log --limit 20      # 最近发生了什么
$ redhand digest --since 2h   # Markdown 摘要：谁改了什么
$ redhand restore --list      # 哪些可以还原
$ redhand restore 42          # 还原到 42 号事件之前的状态
$ redhand restore --path src/app.py    # 撤销某文件的最近一次改动

$ redhand protect add secrets/ *.key .env
$ redhand watch --enforce     # 受保护路径被改时自动回滚

$ redhand actors              # 此刻有哪些代理进程在跑
$ redhand status              # 跟踪与存储概览
$ redhand forget --before 30d # 修剪日志
```

## 归属是怎么做的

每个有改动的轮询周期，redhand 对进程表做快照，把配置的代理特征串（内置 20 个，可在 `config.json` 增删）与进程名、完整命令行做词边界匹配，再用 `lsof`（Linux 亦可用 `/proc/<pid>/cwd`）解析每个匹配进程的**当前工作目录**，判断它是否覆盖被改文件。Windows 上没有公开 API 能读取其他进程的工作目录，因此每个代理命中一律封顶 `medium`——归属在那里只是“谁正在运行”的实时信号，不会猜测是哪一个改的文件：

| 置信度 | 含义 |
|---|---|
| `high` ✓ | 某代理的工作目录就是被监控根目录，或是被改文件的祖先目录 |
| `medium` ? | 有代理在跑，但没有一个覆盖该路径——归为 `unverified-agent` |
| `low` · | 改动发生时没有任何代理进程——归为 `manual` |

**这是相关性判定，不是司法取证。** `high` 的意思是"改动发生的那一刻，可证实有代理正在这棵目录树里工作"，而不是密码学证明。如果你让代理跑在整个家目录上，你自己在这个范围内的终端改动也会落在它的 cwd 附近——把 `high` 理解为"极可能是这个代理"，发现可疑归属时用摘要抽查。

## 什么能还原

每个事件的撤销目标都以压缩 blob 保存，受三个约束：

- **单文件上限** `blob_limit_kb`（默认 256 KB）：更大的文件只按哈希与大小跟踪，不还原内容；
- **总上限** `blob_total_mb`（默认 64 MB）：超出后按 FIFO 逐出**最老**的 blob；shadow（所有被跟踪文件的当前状态）永不逐出，近期撤销链始终完整；
- **仅文本**：无法按 UTF-8 解码的文件只记哈希，不存内容。

已知限制（都是设计使然，`status` 里可见）：

- 轮询扫描（`interval` 默认 2 秒；活动突发期自动快跟约 interval/6）：一个窗口内的两次改动只会看到最终状态；
- **Windows 上归属封顶 `medium`**：没有受支持的方式读取其他进程的工作目录，那里对「谁改的」只能答「有代理在跑」；
- 大文件（仅哈希跟踪）的同尺寸、同 mtime 修改无法检测；
- 二进制文件：不还原内容，但改动仍会被记录；
- 特殊文件（FIFO/套接字/设备）只记录元数据、永不打开——打开命名管道会永久阻塞，redhand 不做这种事；
- 符号链接按链接本身跟踪；内容快照以**目标文件**的真实大小做门槛，指向大文件的链接不会被整读进内存；
- 每个根目录一个 watcher（POSIX 用 flock，Windows 用 msvcrt.locking）；另一终端执行 `restore` 是安全的——记录前的影子新鲜度检查会拦下重复条目。

## 命令一览

| 命令 | 用途 |
|---|---|
| `init` | 写入初始 `.whodunit/config.json` |
| `watch` | 实时记录改动（`--once`、`--interval 秒`、`--enforce`） |
| `digest` | Markdown 报告（`--since 2h`、`--actor claude`、`--out file.md`） |
| `log` | 最近事件，新的在前（`--since`、`--actor`、`--path`、`--event`） |
| `restore` | `--list` / `<事件号>` / `--path 路径` / `--dry-run` |
| `protect` | 查看 / 添加 / 移除受保护模式 |
| `status` | 根目录、跟踪与存储概览 |
| `actors` | 探测正在运行的代理进程 |
| `forget` | 修剪某时间点之前的记录（`--before 30d`、`--yes`） |

全局参数：`--root 目录`（位置不限）、`--no-color`、`--version`。

## 配置

`.whodunit/config.json`（所有键可选）：

```json
{
  "interval": 2.0,
  "ignore": [".git/", "node_modules/", "__pycache__/", ".venv/", "*.pyc", ".DS_Store"],
  "agents": ["claude", "codex", "cursor", "aider", "my-company-agent"],
  "protect": ["secrets/", "*.key", ".env"],
  "hash_limit_mb": 10,
  "blob_limit_kb": 256,
  "blob_total_mb": 64
}
```

模式规则：`name` 匹配任意层级同名路径分量 · `dir/` 剪掉整个目录 · `*.log` 对文件名或完整相对路径做通配 · `/path` 锚定到监控根目录。

## 后台常驻

macOS（launchd）——`~/Library/LaunchAgents/com.redhand.watch.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.redhand.watch</string>
  <key>ProgramArguments</key><array>
    <string>/opt/homebrew/bin/redhand</string>
    <string>watch</string><string>--root</string><string>/Users/you/projects</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/redhand.log</string>
  <key>StandardErrorPath</key><string>/tmp/redhand.log</string>
</dict></plist>
```

```console
$ launchctl load ~/Library/LaunchAgents/com.redhand.watch.plist
```

Linux（systemd 用户服务）——`~/.config/systemd/user/redhand.service`：

```ini
[Unit]
Description=redhand watcher

[Service]
ExecStart=%h/.local/bin/redhand watch --root %h/projects
Restart=on-failure

[Install]
WantedBy=default.target
```

Windows（schtasks）——管理员提示符下执行一次：

```console
$ schtasks /create /tn "redhand watch" /sc onlogon /rl limited ^
  /tr "\"%LOCALAPPDATA%\Programs\Python\Python313\Scripts\redhand.exe\" watch --root C:\Users\you\projects"
```

注意：日志文件要放在**被监控目录之外**，否则 redhand 会忠实地把自己输出也记进日志。

## 和……有什么区别

- **git**——git 跟踪 git 仓库里的已提交工作；redhand 跟踪每一次改动，包括从未提交的工作，也不要求是 git 仓库。两者互补。
- **fswatch / watchman**——面向脚本的实时事件流；redhand 是带归属与还原能力的持久可查询日志。
- **时间机器**——系统级小时快照；redhand 按改动粒度、按文件、带操作者名，只占几 KB。

## 开发

```console
$ git clone https://github.com/Yuh302331-ctrl/redhand && cd redhand
$ pip install -e ".[dev]"
$ pytest tests/ --cov=redhand          # 全量测试，约 90% 覆盖
```

模块结构：`src/redhand/` —— `scanner.py`（目录扫描 + 影子比对） · `actors.py`（进程归属） · `journal.py`（SQLite：影子、事件、blob） · `watch.py`（引擎） · `restore.py` · `digest.py` · `cli.py` · `config.py` · `util.py`。

## 生态

**trueup**（[agenttrueup](https://github.com/Yuh302331-ctrl/agenttrueup)）是审计侧的搭档：它按 schema 契约只读本日志（见 [docs/JOURNAL.md](docs/JOURNAL.md)），把 AI 代理「声称做过的事」与事件流逐条对账。redhand 是摄像头，trueup 是审计师。

## Roadmap

- fsevents/inotify 后端，实现亚秒级检测（轮询引擎保留为后备）
- 归属显示名规则（如在 git 仓库里按提交作者归属）
- HTML 摘要 + diff 视图
- ~~Windows 支持~~——0.2.0 已发布（归属封顶 `medium`，见「归属是怎么做的」）

## 许可

[MIT](LICENSE)
