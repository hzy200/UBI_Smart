# ADR-0013：工作区存储与 revision 归档

- **Status**：accepted
- **Date**：2026-10-25
- **Deciders**：项目作者
- **Owner**：项目作者

## Context

企业当前没有内部 Git 服务，但需要托管代码、支持回滚与灾难恢复。
可选形态包括文件服务器（SMB/NFS）、对象存储、纯 Git 仓库、本地文件系统目录。

Agent 执行依赖 POSIX 语义：文件锁、可执行位、符号链接、`git index` 原子 rename、
`inotify`、随机写。网络文件系统与对象存储在这些点上不可靠或不可用；
纯 Git 仓库没有工作副本，且未跟踪文件/构建产物/附件不在版本内。

## Decision

- **运行时工作区**：A4 同机本地 ext4 目录（Linux 数据盘 / WSL2 VHDX）。
- **元数据**：A2 MySQL，保存 `workspace_id`、`root_path`、配额、revision 指针。
- **版本层**：工作区内 `git init` 本地仓库，不配置 remote；每次导入一个 commit。
- **归档**：本地归档盘 `revisions/<workspace_id>/<revision_id>.tar.gz` +
  manifest（commit、sha256、文件数、扫描结果）。选择 gzip 而不是 zstd，
  是因为 Python 3.11 stdlib 不支持 zstd，避免给 A3 增加外部命令依赖。
- **保留策略**：默认最近 10 个 revision；pinned 不自动清理；只删归档文件，
  DB 行置 `pruned`，审计保留。
- **恢复**：正常回滚走 `git reset --hard`；归档仅用于工作区损坏、误删或换机。

不选 SMB/NFS：锁、权限与性能不满足执行热路径。
不选对象存储：没有目录语义与随机写。
不选纯 bare Git：没有可写工作副本，且不覆盖未跟踪文件。

## Consequences

### 好的

- Agent 获得完整 POSIX 语义与低延迟 I/O；
- 无内部 Git 服务也能版本化、回滚、审计；
- 归档与工作盘分离，误删工作区仍可恢复。

### 坏的 / 代价

- 代码没有第二份远端；必须依赖归档盘快照/RAID/异地备份；
- 桌面首次导入需要复制，大型仓库耗时且有磁盘占用；
- revision 清理是本地策略，不做跨机复制；
- 归档只保存代码快照，不包含 checkpoint/审计链（另行备份）。

### 中性 / 需要留意

- 归档盘与工作盘建议分开挂载；
- 单文件上限、解压比与配额由 M3 上传实现；
- 未来接入内部 Git 时，`git_clone` 可作为新的 source，不改变工作区模型。

## 复核

- **Verified by**：`tests/unit/test_workspace_registry.py`、
  `tests/unit/test_workspace_quota_revision.py`。
- **Revisit when**：企业部署内部 Git 服务，或需要多机共享工作区时。
