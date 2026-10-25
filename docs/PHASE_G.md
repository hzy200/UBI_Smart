---
layer: L3
doc_id: phase-g
title: Phase G：工作区注册表与部署验收
owner: 项目作者
status: active
last_reviewed: 2026-10-25
review_cadence: 部署拓扑、工作区流程或验收脚本变动时
---

# Phase G：工作区注册表与部署验收

Phase G 把“打开文件夹”升级为托管工作区，并在专用 WSL 发行版上做真实
导入/归档/回滚验收。设计背景见 [RFC-0001](rfc/0001-workspace-registry.md)、
[ADR-0012](adr/0012-workspace-entity.md)、[ADR-0013](adr/0013-workspace-storage.md)。

## 1. 验证命令

```bash
conda activate agent
python scripts/phase_g_smoke.py --workspace /tmp/agent-e2e-smoke
python scripts/phase_g_e2e.py --source "D:\\User\\e2e-src"
```

前置：专用发行版已导入，且当前代码已安装进它的 venv：

```bash
wsl --import CodingAgent "$LOCALAPPDATA\\CodingAgent\\wsl\\CodingAgent" \
  packaging/wsl/CodingAgent-rootfs.tar
wsl -d CodingAgent -u root -- /opt/coding-agent/venv/bin/pip install \
  --no-deps --force-reinstall /mnt/d/User/PycharmProjects/CodingAgent
```

## 2. 实测结果（2026-10-25）

环境：

```text
发行版：CodingAgent（专用，默认用户 agent）
uid：1000（非 root）
/etc/wsl.conf：[interop] appendWindowsPath=false
工具：Python 3.12.3 / git 2.43.0 / tar 1.35 / bwrap
工作区：/tmp 与 /home 均为 ext4
```

E2E（不调用模型）：

```text
源：/mnt/d/User/e2e-src（3 个文件）
目标：/home/agent/agent-ws/users/alice/alice/e2e-project（ext4）
首次导入：active，3 个文件
revision：git commit + tar.gz + sha256
第二次导入（active 工作区）：新增 revision，VERSION2 生效
会话：绑定 workspace_id
回滚到首次导入：生成 pre-rollback revision；VERSION2 消失
回滚后 revision 数：3
灾难恢复：删除 README 后从 v1 归档恢复；README 回来，revision 数 4
真实模型：1 次极简运行，事件 run_started → plan_created →
          assistant_token → cost_report → run_finished
真实审批：file_write 触发 1 次审批，resume 后创建 approval.txt；
          snapshots=1，restore 删除该文件并完成回滚
浏览器上传：zip → active，2 个文件，scan=skipped，revision 1
HMAC 联调：--hmac 启用 AGENT_INTERNAL_HMAC_KEY，A2→A3 请求全部验签通过
L3 拒绝：rm -rf 触发 2 次审批（L3/L2），全部拒绝；sentinel 仍在，
          run_finished
```

第二次导入复用当前 `.git` 历史（硬链接对象），staging 完成后原子替换；
失败时旧工作区保持不变。

`applied` 的配置：

```text
AGENT_WORKSPACE_REGISTRY=true
AGENT_WORKSPACE_ROOT=/home/agent/agent-ws/users
AGENT_WORKSPACE_ARCHIVE_ROOT=/home/agent/.coding-agent/revisions
AGENT_WORKSPACE_MOUNT_ROOTS=/mnt/d
AGENT_WSL_REQUIRE_HARDENING=true
```

## 3. 控制面 MySQL 初始化

脚本只建库、建用户、授权，**不安装 MySQL**；迁移仍由管理员账号执行。

```bash
# 先看 SQL，不连接数据库
python scripts/setup_control_mysql.py --dry-run

# 有建库/建用户权限的账号执行
python scripts/setup_control_mysql.py \
  --admin-url "mysql+pymysql://root:***@127.0.0.1:3306/" \
  --show-url
```

默认授权 `SELECT/INSERT/UPDATE/DELETE`，不授予 DDL；输出里的
`AGENT_CONTROL_DB_URL` 交给 A2，迁移使用管理员账号
`control_server --migrate`。

## 4. 多实例事件传输

A2↔A3 默认使用内存事件缓冲；配置 `AGENT_EVENT_TRANSPORT=redis` 后写入
Redis Streams，并按 `seq` 重放。这里**刻意不使用 consumer group**：
每个 A2 实例都需要完整事件流来服务自己的 SSE 客户端，consumer group
会把同一条事件只投递给一个消费者。当前重放仍按 `seq` 过滤，后续可用
阻塞读或专用索引优化扫描量。决策见
[ADR-0014](adr/0014-event-fanout-not-consumer-group.md)。

## 5. 已知限制

- 导入/上传任务状态落盘到 A3 数据目录；A3 重启后未完成任务标记 failed、
  清理 staging，终态任务保留 `AGENT_WORKSPACE_JOB_RETENTION_DAYS` 天。
- 上传仍是单包，无分片/断点续传；浏览器式 multipart 上传已通过真实 E2E。
- active 更新使用“staging + 原子替换”；极小概率在两次 rename 之间崩溃时，
  下次任务会从 `.old-*` 备份恢复，但恢复是 best-effort。
- `.git` 历史通过硬链接复制；不支持跨文件系统硬链接时自动退化为复制。
- 归档未加密，依赖磁盘权限与运维备份。
- 回滚走本地 git；归档恢复需要归档内包含 `.git`，并拒绝符号链接。
- 归档恢复会校验路径与 sha256，恢复前自动生成 pre-restore revision。
- 恢复中心会为最多 10 个暂停/中断会话加载真实 pending approvals；
  单个会话加载失败按 0 处理，不阻塞列表。
- 真实模型**基础运行**、一次 `file_write` 审批暂停/恢复、快照回滚已验证
  （key 由 A2 短期注入 A3）。更高风险命令（L2/L3）的审批仍需按需演练。
- 现有 Ubuntu 发行版未加固；不要直接用它承载托管工作区。
