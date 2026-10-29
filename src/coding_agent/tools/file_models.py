"""文件工具的输入模型与读行数上限。"""

from __future__ import annotations

from pydantic import BaseModel, Field

DEFAULT_READ_LINES = 400

# limit 只有下界时，模型传 `limit=100000` 就能把整个文件（上限 2MB）一次性灌进
# 上下文 —— 而上下文裁剪**不碰最近 keep_recent 条**，任何裁剪都拦不住它。
# 这里是一道理智闸门；真正的上下文保证在 `llm/context.py` 的单条消息上限。
MAX_READ_LINES = 2_000


class ReadInput(BaseModel):
    path: str = Field(description="文件路径，工作区内相对路径或绝对路径")
    reason: str = Field(description="读取这个文件的意图，一句话说明")
    offset: int | None = Field(default=None, description="起始行号（从 1 开始），省略则从头读")
    limit: int | None = Field(
        default=None,
        ge=1,
        le=MAX_READ_LINES,
        description=f"最多读取行数（{DEFAULT_READ_LINES} 以内效果最好，上限 {MAX_READ_LINES}）",
    )


class WriteInput(BaseModel):
    path: str = Field(description="文件路径，工作区内相对路径或绝对路径")
    content: str = Field(description="写入的完整内容")
    reason: str = Field(description="写这个文件的意图，一句话说明")


class EditInput(BaseModel):
    path: str = Field(description="文件路径，工作区内相对路径或绝对路径")
    old_string: str = Field(
        min_length=1,
        description="要被替换掉的原文，**不能为空**，必须与文件内容逐字符一致且唯一",
    )
    new_string: str = Field(description="替换成的新内容；传空字符串表示删除这段")
    reason: str = Field(description="这次修改的意图，一句话说明")
    replace_all: bool = Field(default=False, description="old_string 出现多次时是否全部替换")


class EditSpec(BaseModel):
    path: str = Field(description="文件路径")
    old_string: str = Field(
        min_length=1, description="要替换的原文，必须在当前文件中唯一"
    )
    new_string: str = Field(description="替换后的内容")
    replace_all: bool = Field(
        default=False, description="old_string 出现多次时是否全部替换"
    )


class EditManyInput(BaseModel):
    edits: list[EditSpec] = Field(
        min_length=1,
        max_length=20,
        description="按顺序应用的编辑；同一文件可以有多条",
    )
    reason: str = Field(description="批量修改的意图，一句话说明")


class RestoreInput(BaseModel):
    path: str | None = Field(
        default=None, description="要回滚的文件；省略则回滚最近一次文件改动"
    )
    snapshot_id: str | None = Field(
        default=None, description="指定快照 id；省略则用该文件最近一次留底"
    )
    reason: str = Field(description="回滚的意图，一句话说明")


class MoveInput(BaseModel):
    source: str = Field(description="要移动的文件路径")
    destination: str = Field(description="目标路径（可改名或换目录）")
    reason: str = Field(description="移动的意图，一句话说明")
    overwrite: bool = Field(
        default=False, description="目标已存在时是否覆盖（覆盖前会留底）"
    )


class CopyInput(BaseModel):
    source: str = Field(description="要复制的文件路径")
    destination: str = Field(description="目标路径")
    reason: str = Field(description="复制的意图，一句话说明")
    overwrite: bool = Field(
        default=False, description="目标已存在时是否覆盖（覆盖前会留底）"
    )


class DeleteInput(BaseModel):
    path: str = Field(description="要删除的文件路径（只支持普通文件）")
    reason: str = Field(description="删除的意图，一句话说明")


class MkdirInput(BaseModel):
    path: str = Field(description="要创建的目录路径（会自动创建父目录）")
    reason: str = Field(description="创建目录的意图，一句话说明")


class ListDirInput(BaseModel):
    path: str = Field(default=".", description="目录路径，默认工作区根目录")
    max_depth: int = Field(default=1, ge=1, le=5, description="递归深度")
    limit: int = Field(default=200, ge=1, le=1000, description="最多返回多少项")
    reason: str = Field(description="列目录的意图，一句话说明")
