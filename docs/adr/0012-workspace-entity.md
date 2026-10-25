# ADR-0012：工作区成为一等实体

- **Status**：accepted
- **Date**：2026-10-25
- **Deciders**：项目作者
- **Owner**：项目作者

## Context

单机模式下，工作区就是进程的当前目录；分层后它变成贯穿 A2/A3/A4 的裸字符串。
路径会随目录改名、机器迁移、挂载点变化而失效，也无法承载配额、归属、版本与审计。
企业部署还要求“客户端不能指定任意主机路径”。

备选方案：

1. 继续传裸路径；
2. 把路径当主键；
3. 引入 `workspace_id`，路径只作为运行时解析结果。

## Decision

采用方案 3。

- `control/workspaces.py` 拥有 `workspace_id`、`root_path`、状态机与路径策略；
- A2 生成并解析路径，A3 只校验，A4 二次校验；
- 会话绑定 `workspace_id`，一个会话不可改绑；
- `control_sessions.workspace` 保留为解析结果，保证旧数据与前端兼容。

不选方案 1：无法支持改名、迁移、配额与审计归属。
不选方案 2：路径可变，迁移与回滚都会破坏外键和恢复语义。

## Consequences

### 好的

- 会话、审计、记忆可以稳定关联到同一工作区；
- 客户端不再拥有“指定任意路径”的能力；
- 为后续上传、revision、配额提供挂载点。

### 坏的 / 代价

- 多一层解析与注册流程；桌面首次导入需要复制，不再“立即打开”；
- 旧会话必须迁移，`legacy` 记录无法享受新配额与 revision；
- A2 与 A3 的路径语义必须一致，否则会出现“注册成功但运行失败”。

### 中性 / 需要留意

- `slug` 创建后不可变，重命名只改 `display_name`；
- Windows 上 A2 无法对 WSL 路径做 `realpath`，符号链接二次校验必须由 A4 在沙箱内完成。

## 复核

- **Verified by**：`pytest tests/unit/test_workspace_registry.py tests/unit/test_workspace_paths.py`。
- **Revisit when**：引入团队共享工作区，或需要跨主机迁移工作区时。
