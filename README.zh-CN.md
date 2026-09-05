# Project Continuity

这是一个仅依赖 Python 标准库的 Skill 与 CLI，用于在不同编码 Agent 之间维护
权威的当前项目进度。v2 在 `.relay/CURRENT.md` 中保存权威 JSON 和由它生成的人类可读
视图，并用 revision 校验、writer lease、operation receipt、本地文件锁、原子写入
和历史快照保护状态。

**版本：** `0.1.0-rc.1` 发布候选版，发布 Gate 尚未完成。项目计划覆盖 macOS、
Linux、Windows 及 Python 3.11、3.12、3.13；这只是 CI 矩阵目标，并不表示九种组合
已经全部实测通过。当前也不声称真实客户端或现有项目已完成迁移。

## 设计目的

普通进度笔记容易变成过期叙述或第二套真相源。新项目明确初始化 v2 后，
`.relay/CURRENT.md` 是当前项目进度的唯一权威。内置 `migrate` 只转换 v1 relay
格式，不能迁移任意业务 JSON 或 verifier；旧项目在字段映射与 reader 切换经审查并
完成前继续使用原有业务权威，完成明确的项目切换后才以 `CURRENT.md` 为唯一权威。
历史证据以及发布、部署、实验产物保持独立，任务 done 不代表这些结果已完成。工具
不会启动 watcher、提交或推送 Git，也不会调用模型或执行外部任务。

## 快速开始

在仓库或已安装 Skill 目录中运行：

```bash
# 只有明确 init 才会创建 .relay/
python scripts/write_current.py init --root /path/to/project
python scripts/write_current.py status --root /path/to/project
python scripts/write_current.py validate --root /path/to/project
```

从 `status` 读取 revision，再取得 lease。每次写操作都需要最新的 expected
revision、稳定的 writer ID 和唯一的 operation ID。

```bash
python scripts/write_current.py resume --root /path/to/project \
  --writer agent-a --expected-revision 0 --operation-id resume-001

python scripts/write_current.py update --root /path/to/project \
  --writer agent-a --expected-revision 1 --operation-id update-001 \
  --input examples/change.json

python scripts/write_current.py save --root /path/to/project \
  --writer agent-a --expected-revision 2 --operation-id save-001 \
  --input - < another-change.json
```

`save` 会释放 lease。默认 lease 为 30 分钟。`--allow-drift` 只表示你已经人工检查
Git 漂移，不是自动绕过。完整说明见[命令参考](references/commands.md)和
[协议说明](references/protocol.md)。

## 安全边界与限制

- 明确初始化或启用后，Agent 在任务开始、任务完成和 blocker 变化时更新；`save`
  释放 lease。用户指定只手动保存时遵从覆盖，不逐消息写入，也没有后台 daemon。
- v1 在明确执行 `migrate --apply` 前保持只读；`migrate` 默认仅 dry-run，apply
  还必须提供预览返回的 `source_sha256`。Apply 会设置
  `extensions.migration.mapping_review_required=true`，必须报告为 `UNVERIFIED`，不能
  称为项目迁移完成。切换期间暂停进度编辑，不得同时双写旧权威与 `CURRENT.md`。
- `recover` 只允许在 lease 过期后执行，并要求记录原因。
- 文档上限为 64 KiB。工具会拒绝明显秘密和危险路径，但 `.relay/` 绝不能作为
  凭据仓库。
- 操作系统文件锁只协调本机进程，不是分布式锁。
- Git 捕获直接哈希 index/tree 与工作区原始字节，不执行 clean filter，总量上限为
  64 MiB；符号链接和 submodule 会被拒绝，CRLF 或 clean-filter 仓库可能被保守地
  报告为 dirty。
- 默认 `.relay/.gitignore` 让运行态保持本地。若明确选择 Git 分享，只共享
  `CURRENT.md` 和对应的明确 ignore 配置，绝不共享整个 `.relay/`。

## 安装

解压带版本号的压缩包，然后把完整的 `project-continuity` 目录复制到客户端实际使用
的 Skills 目标目录，不要只复制部分文件。应从客户端当前配置确认目标位置，本项目不
硬编码全局路径。若目标目录已存在，立即停止：先检查并备份到独立的时间戳路径，再
决定是否替换，禁止隐式合并或覆盖。仅在安全时重启或重新加载客户端，随后验证 Skill
发现状态，并在安装后的目录中运行测试。

## 开发与打包

```bash
python -m unittest discover -s tests -v
python scripts/package_skill.py /tmp/project-continuity-v0.1.0-rc.1.zip
```

打包器使用显式仓库文件 allowlist、排序记录和固定元数据，并生成 SHA-256 sidecar；
如果任一输出已存在则拒绝覆盖。压缩包不会包含 Git 数据、缓存、relay 状态、生成
压缩包或私人文件。更多内容见 [CONTRIBUTING.md](CONTRIBUTING.md)、
[SECURITY.md](SECURITY.md) 和 [NOTICE](NOTICE)。

## 许可证

MIT，Copyright (c) 2026 seriousz158，见 [LICENSE](LICENSE)。[NOTICE](NOTICE)
保守保留了可能受 `liu676767/codex-project-checkpoint-memory` 影响部分的署名。
