# ADR-0024：文档/图像管线复用 orchestrator 动态任务图，解析放宿主

- **Status**：accepted
- **Date**：2026-10-30
- **Deciders**：项目作者
- **Owner**：项目作者

## Context

需要让 Agent 阅读 PDF/Office 文档、理解图片、提炼关键信息并撰写报告。
现有能力有三个硬约束：

- 主图 `planner → act → verify → review` 的 `plan` 是线性步骤列表，
  不适合“多文档并发提取 + 汇总 + 撰写 + 校验”的 DAG；
- `file_read` 只接受 UTF-8 文本，解析库也不在 WSL 沙箱里；
- orchestrator 已有事件驱动 DAG、依赖、重试、动态任务提议与 fan-in，
  但角色只有 explore/test/review/implement，且依赖结果不会传给下游。

可选做法：

1. 在主图中新增 document/vision/author 节点，并扩展 `estimate_recursion_limit`；
2. 为文档任务单独起一条新的 LangGraph 子图；
3. 扩展现有 orchestrator：新增 workflow、角色、阶段与依赖上下文；
4. 保持 direct 模式，用提示词让主 agent 串行调用文档工具。

## Decision

选择第三种：**文档/图像/报告任务全部落在现有 orchestrator 子图内**。

- 新增 workflow `document_pipeline`，判定放在 `rule_workflow` 的文件数门槛之前；
- `TaskRole` 增加 `document` / `vision` / `author`；
- `TaskSpec` 增加 `stage`，高阶段 join 任务等低阶段任务不再 pending；
- scheduler 向下游 worker 注入有界依赖上下文（摘要、artifact 路径、引用），
  完整内容由 worker 读 artifact；
- 动态任务提议只允许 `document` / `vision`，`author` / `implement` 不可由
  worker 自行追加；
- 不新增主图节点，`graph/build.py::estimate_recursion_limit` 不变。

配套决策：

- **解析在宿主进程**：`tools/documents/` 通过 `FileAccess.read_bytes` 取字节，
  用 pypdf/python-docx/openpyxl/xlrd/python-pptx 解析；不给 WSL 装解析库。
- **旧格式不转换**：`.doc` / `.ppt` 返回“请先转为 docx/pptx”，
  不引入 LibreOffice/antiword/pandoc。
- **视觉用默认多模态 LLM**：不新增独立 vision provider；图片在工具/worker
  内部完成理解，返回文本 + 结构化结果，不把 base64 长期塞进主对话历史。
- **不支持时明确失败**：端点不支持图片、格式不支持、解析失败都返回可操作错误，
  不静默降级或假装成功。

不选方案 1：新增主图节点会改变主图拓扑与 recursion limit，且文档任务天然可并发，
放进线性主图会把 orchestrator 已有能力重做一遍。不选方案 2：独立子图会复制
scheduler/fan-in/steering，维护成本高于扩展现有子图。不选方案 4：direct 串行
无法利用多文档并发，也无法把引用索引交给撰写任务。

## Consequences

### 好的

- 复用已测试的 scheduler、依赖、重试、fan-in、artifact 与 steering；
- 文档任务可并发，单文档失败不阻塞整体，缺口可追溯；
- 宿主解析不污染 WSL，host/sandbox 两种工作区走同一套代码；
- 主图拓扑与安全不变量不变，回滚只需关闭 workflow 开关；
- 旧格式边界明确，不引入外部转换进程与新的执行面。

### 坏的 / 代价

- orchestrator 角色与配置增多，任务图校验、预算、并发都需要按角色区分；
- `stage` 与依赖上下文是新语义，必须补调度单测，否则容易出现 author 提前启动
  或下游看不到上游结果；
- 文档解析依赖可选 extra，未安装时相关任务只能明确失败；
- 多文档 pipeline 的 token 成本高于单 agent 串行读取，需要缓存与分页控制；
- PPTX 只承诺结构化内容，不承诺像素级还原；
- 动态提议仍可能产生低价值任务，需要去重、上限与 steering 干预。

### 中性 / 需要留意

- `.agent/documents/` 与 `.agent/artifacts/reports/` 是新增缓存/产物目录，
  需要按现有保留策略清理；
- 文档内容是不可信输入，提示注入防护依赖系统提示与工具边界，
  不是完整 DLP；
- 视觉能力依赖模型端点真实支持图片输入，需保留失败路径与配置说明。

## 复核

- **Verified by**：`tests/unit/test_orchestrator_document_pipeline.py`
  覆盖 workflow 选择、角色校验、stage 屏障、角色并发/预算与依赖上下文；
  `tests/unit/test_document_tools.py` 覆盖宿主解析、缓存、图片提取与
  ZIP/XXE 安全限制；`tests/unit/test_document_pipeline_e2e.py` 覆盖
  scheduler 级多文档链路与取消。
- **Revisit when**：文档任务占主导、orchestrator 出现明显复杂度瓶颈时，
  考虑抽独立子图；若模型 provider 拆分出专用 vision 端点，重新评估
  “默认多模态 LLM”的决定；若旧格式成为高频需求，单独评估转换器方案。

## 关联

- RFC：[`../rfc/0005-document-image-and-report-pipeline.md`](../rfc/0005-document-image-and-report-pipeline.md)
- 威胁模型：[`../THREAT_MODEL.md`](../THREAT_MODEL.md)
- 验收：[`../acceptance/acceptance-criteria.yaml`](../acceptance/acceptance-criteria.yaml)
