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

## 3. 已知限制

- 导入/上传任务状态落盘到 A3 数据目录；A3 重启后未完成任务标记 failed、
  清理 staging，终态任务保留 `AGENT_WORKSPACE_JOB_RETENTION_DAYS` 天。
- 上传仍是单包，无分片/断点续传。
- active 更新使用“staging + 原子替换”；极小概率在两次 rename 之间崩溃时，
  下次任务会从 `.old-*` 备份恢复，但恢复是 best-effort。
- `.git` 历史通过硬链接复制；不支持跨文件系统硬链接时自动退化为复制。
- 归档未加密，依赖磁盘权限与运维备份。
- 回滚走本地 git；归档恢复需要归档内包含 `.git`，并拒绝符号链接。
- 归档恢复会校验路径与 sha256，恢复前自动生成 pre-restore revision。
- 真实模型运行、审批、快照链路仍需配置 provider key 后人工验收。
- 现有 Ubuntu 发行版未加固；不要直接用它承载托管工作区。
