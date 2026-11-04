---
layer: L3
doc_id: metrics
title: 指标快照（生成）
owner: 项目作者
status: active
last_reviewed: 2026-10-28
review_cadence: 由 CI 覆盖率任务生成；不要手改
---

# 指标快照（生成）

> 由 `scripts/snapshot_metrics.py` 从覆盖率报告与 `pytest --collect-only`
> 生成。**不要手改**；重跑命令：
>
> ```bash
> pytest -m "not wsl and not wsl_env and not llm" --cov --cov-report=json:coverage.json
> python scripts/snapshot_metrics.py --coverage coverage.json --collect --write
> ```

| 指标 | 值 |
| --- | --- |
| 快反馈用例 | **3,606** |
| 覆盖率 | **83.71%** |
| 总语句 | **47,848** |
| 未覆盖 | **7,793** |
| 承重模块目标 | **85%** |

## 承重模块

| 模块 | 当前 | 棘轮下限 | 目标 |
| --- | ---: | ---: | ---: |
| `control/accounts.py` | 87.2% | 87.1% | 85% |
| `control/approvals.py` | 98.2% | 96.8% | 85% |
| `control/credentials.py` | 95.8% | 94.8% | 85% |
| `control/mfa.py` | 92.8% | 91.8% | 85% |
| `control/oidc.py` | 99.2% | 98.2% | 85% |
| `control/store/__init__.py` | 100.0% | 99.0% | 85% |
| `control/store/audit.py` | 81.1% | 80.0% | 85% |
| `control/store/base.py` | 93.0% | 92.0% | 85% |
| `control/store/notifications.py` | 89.6% | 88.6% | 85% |
| `control/store/search.py` | 89.8% | 87.8% | 85% |
| `control/store/sessions.py` | 100.0% | 99.0% | 85% |
| `control/store/usage.py` | 100.0% | 99.0% | 85% |
| `control/store/usage_common.py` | 100.0% | 99.0% | 85% |
| `control/store/usage_record.py` | 94.4% | 82.3% | 85% |
| `control/store/usage_report.py` | 97.7% | 96.7% | 85% |
| `control/store/usage_reserve.py` | 90.5% | 89.5% | 85% |
| `execution/service.py` | 92.0% | 91.0% | 85% |
| `graph/nodes/approve.py` | 100.0% | 99.0% | 85% |
| `host_broker/consent.py` | 96.9% | 95.9% | 85% |
| `sandbox/fs.py` | 95.4% | 94.9% | 85% |
| `sandbox/fs_scripts.py` | 95.8% | 94.8% | 85% |
| `sandbox/pathguard.py` | 100.0% | 99.0% | 85% |
| `sandbox/policy.py` | 92.0% | 91.0% | 85% |
| `sandbox/snapshots.py` | 76.8% | 75.8% | 85% |
| `servers/agent_server_routes/workspace_import.py` | 88.7% | 87.1% | 85% |
| `tools/code_intel.py` | 98.9% | 97.9% | 85% |
| `tools/files.py` | 88.5% | 88.3% | 85% |
| `tools/git.py` | 96.0% | 95.8% | 85% |
| `web/routes/admin.py` | 90.8% | 89.3% | 85% |
| `web/routes/auth.py` | 100.0% | 99.0% | 85% |
| `web/routes/runs.py` | 86.5% | 85.5% | 85% |

WSL 门禁（`.github/workflows/sandbox.yml`）另设下限：
`sandbox/fs.py`、`sandbox/snapshots.py`、`tools/files.py`。

> 历史数字不再手抄；以本文件与 CI 输出为准。
