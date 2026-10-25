# 先激活项目环境：conda activate agent（或 .venv）
# 需要先装开发依赖：pip install -e ".[dev]"
# （pytest 的 addopts 带 `-n auto`，缺 pytest-xdist 会直接报错；
#   `make cov` 另外需要 pytest-cov）
.PHONY: check lint test test-unit test-integration smoke regression agent-e2e system-e2e incremental test-all cov docs check-compat

check: lint test  ## 提交前跑这个：lint + 快速测试

docs:  ## 文档一致性 + 漂移 + 环境快照 + 契约兼容（需要在 agent 环境）
	python scripts/check_docs.py --strict
	python scripts/check_drift.py
	python scripts/snapshot_env.py --check
	python scripts/gen_contracts.py --check-compat --base HEAD

check-compat:  ## 与 HEAD 比较破坏性契约变更（提交前跑）
	python scripts/gen_contracts.py --check-compat --base HEAD

lint:
	ruff check src tests

test:  ## 快反馈：单元 + TUI + Web（不需要 WSL / API Key）
	pytest -m "not wsl and not llm"

test-unit:  ## 只跑单元层
	pytest tests/unit -m "not wsl and not llm"

test-integration:  ## 只跑集成层（不含真实 WSL / LLM）
	pytest tests/integration -m "not wsl and not llm"

smoke:  ## 冒烟：主链路可用性，秒级、无外部依赖
	pytest -m smoke

regression:  ## 回归：承重不变量与历史事故用例
	pytest -m regression

agent-e2e:  ## 智能体级验收：真实 WSL + 真实模型（会消耗额度）
	AGENT_WSL_DISTRO=CodingAgent pytest -m agent_e2e tests/integration/test_agent_e2e.py

system-e2e:  ## A2/A3/A4 全链路：导入/回滚/归档/上传/HMAC/审批/快照（需专用发行版与密钥）
	AGENT_SYSTEM_E2E=1 AGENT_WSL_DISTRO=CodingAgent pytest -m system_e2e tests/integration/test_system_e2e.py -n 0

incremental:  ## 增量：按 git diff 选择受影响测试；无映射时回退全量
	python scripts/select_tests.py --exec

test-all:  ## 全量：含真实 WSL 沙箱
	pytest -m "not llm"

cov:  ## 与 CI 同口径：快反馈子集 + 覆盖率门槛（76，见 pyproject）
	pytest -m "not wsl and not llm" --cov --cov-report=term-missing
