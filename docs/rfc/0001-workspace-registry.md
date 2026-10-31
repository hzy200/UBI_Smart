---
layer: L2
doc_id: rfc-0001
title: 工作区注册表与托管存储
owner: 项目作者
status: accepted
created: 2026-10-25
last_reviewed: 2026-10-25
review_cadence: 状态变更或范围变化时
---

# RFC-0001：工作区注册表与托管存储

> 把“用户打开一个文件夹”从裸路径升级为一等实体 `workspace`，并在个人开发
> 与无内部 Git 的企业部署下给出一致的存储、版本与恢复语义。

## 1. 目标

- 工作区有稳定 `workspace_id`；会话绑定 ID，而不是可变路径。
- 根路径只由 A2 生成；客户端不能提交任意主机路径。
- 桌面默认把本地目录复制到 WSL2 ext4；企业只接受上传与模板。
- 每次导入产生可回滚的 revision，元数据在 MySQL，字节在 A4。
- 总开关关闭时，现有单机与同进程部署行为完全不变。
- 验证：`tests/unit/test_workspace_registry.py`、`test_workspace_paths.py`、
  `test_workspace_migration.py`、`test_workspace_config.py`。

## 2. 非目标

- 团队成员、共享工作区、团队级/系统级会话。
- 多人并行写同一工作区的分支合并审批。
- 内部 Git 服务或 `git clone`（本期配置默认关闭，不实现）。
- 对象存储、双向实时同步、CRDT/OT。
- 原生 Windows 工具层。

## 3. 背景与证据

当前工作区是贯穿 A2/A3 的裸字符串：

- `control_sessions.workspace`（`src/ubismart/control/schema.py:47`）；
- `AgentContext.workspace`（`src/ubismart/agent_protocol.py:70`）；
- `AgentRuntimeFactory.new()` 无条件 `mkdir`（`src/ubismart/agent_context.py:44`）；
- 桌面 `pick_folder()` 直接返回 `/mnt/...`（`src/ubismart/desktop/api.py:40`）。

后果：目录改名/换机后会话失去连续性；A3 只校验“以 `/` 开头”
（`src/ubismart/servers/agent_server.py:103`）；Windows 默认工作区在 9p 上；
没有工作区级配额、revision 与归档。

## 4. 受影响面

| 项 | 内容 |
|---|---|
| 模块 | `control/`、`web/routes/`、`desktop/`、`agent_server*`、`execution/` |
| 契约 | 新增配置项；领域事件暂不变 |
| 数据 | 新增 `workspaces`、`workspace_revisions`；`control_sessions.workspace_id` |
| 前端 | 后续 M5 增加项目列表与导入向导 |
| 权限 | 路径守卫、A2 不直接读写工作区、A3 只校验不创建 |

## 5. 安全与隐私

- 新增资产：工作区根路径、revision 归档（含源码）。
- 对手：通过 API 提交越界路径、符号链接逃逸、上传压缩炸弹。
- 控制：`ensure_inside` 词法校验 + A4/WSL 内 `realpath` 二次校验；
  客户端永不提交 `root_path`；归档目录 `0700`、不通过前端直接下载。
- MySQL 不存文件内容、checkpoint、明文 API Key（企业 provider Key 只存密文）。

## 6. 兼容与回滚

- 破坏性变更：无（`AGENT_WORKSPACE_REGISTRY` 默认 `false`）。
- 旧字段 `control_sessions.workspace` 保留，作为运行时解析结果。
- 回滚：关闭开关；Alembic `downgrade 0001`；旧数据回填脚本只读扫描、幂等。

## 7. 可观测性

- 后续事件：`workspace_created`、`workspace_imported`、
  `workspace_scan_failed`、`workspace_rolled_back`。
- 失败时审计记录 `workspace_id`、阶段（provisioning/active/failed）与原因。

## 8. 验收标准

```text
Given 一个租户与 owner
When  创建同名工作区两次
Then  第二次得到不同 workspace_id 或明确冲突，且 slug 稳定

Given 根目录 /srv/agent-ws 与路径 /srv/agent-ws/../../etc
When  解析工作区根
Then  拒绝并抛出路径越界错误

Given 旧库中已有 control_sessions.workspace
When  执行回填扫描两次
Then  只产生一份 workspace 记录，workspace_id 幂等
```

## 9. 替代方案与代价

| 方案 | 优点 | 代价 / 风险 | 为什么没选 |
|---|---|---|---|
| 继续用裸路径 | 改动最小 | 改名/迁移即断；无法配额/审计归属 | 不满足企业托管 |
| 路径作为主键 | 实现简单 | 路径可变、难迁移 | 与“可恢复/可审计”冲突 |
| 引入内部 Git 服务 | 版本协作完整 | 企业当前没有；运维成本高 | 本期用本地 Git + 归档 |
| 对象存储直接当工作区 | 弹性好 | 无 POSIX 语义，Agent 无法执行 | 运行时必须是本地文件系统 |

## 10. 决策与后果（批准/实现后填）

- 最终选择：本地 ext4 工作副本 + MySQL 元数据 + 本地归档盘；Git 仅作本地版本层。
- 批准人 / 日期：用户口头批准，2026-10-25。
- 真实结果与预期差异：待 M1 完成后补。
- 新增技术债：无内部 Git remote，代码缺少异地备份；需运维快照兜底。
- 关联 ADR：[ADR-0012](../adr/0012-workspace-entity.md)、
  [ADR-0013](../adr/0013-workspace-storage.md)。

## 11. 任务分解与验证计划

| 任务 | 负责模块 | 验证方式 |
|---|---|---|
| M0 配置与测试骨架 | `config.py` | `test_workspace_config.py` |
| M1 表与 Store | `control/schema.py`、`control/workspaces.py` | `test_workspace_registry.py` |
| M1 路径策略 | `control/workspaces.py` | `test_workspace_paths.py` |
| M1 旧数据回填 | `control/workspaces.py`、脚本 | `test_workspace_migration.py` |
| M2/M3 导入与 revision | `desktop/`、`web/routes/`、`execution/` | 后续 RFC/M 里程碑 |
