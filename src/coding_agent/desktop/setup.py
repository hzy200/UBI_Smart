"""首次启动向导（Phase 1，pywebview 原生窗口）。"""

from __future__ import annotations

import json
from dataclasses import dataclass

from coding_agent.desktop.config import LauncherConfig, config_path
from coding_agent.desktop.wsl import (
    ensure_dir,
    home,
    list_distros,
    python_status,
    to_wsl_path,
)

_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<style>
body{font-family:Segoe UI,Microsoft YaHei,sans-serif;margin:24px;background:#0f1117;color:#e6e9f0}
h2{margin:0 0 14px;color:#5b9dff}
label{display:block;margin:12px 0 4px;color:#8b93a7;font-size:13px}
input,select{width:100%;padding:8px;background:#161a23;color:#e6e9f0;border:1px solid #2b3242;border-radius:6px;box-sizing:border-box}
button{margin-top:10px;padding:8px 12px;background:#5b9dff;color:#fff;border:0;border-radius:6px;cursor:pointer}
button.secondary{background:#1d2230;border:1px solid #2b3242;color:#e6e9f0}
pre{background:#161a23;border:1px solid #2b3242;padding:10px;border-radius:6px;white-space:pre-wrap;max-height:140px;overflow:auto}
.row{display:flex;gap:8px}.row input{flex:1}
.note{color:#8b93a7;font-size:12px;white-space:pre-wrap}
.bad{color:#ef6b73}.ok{color:#4cc38a}
</style></head><body>
<h2>CodingAgent 首次设置</h2>
<div class="note">Agent 将运行在 WSL2 内。Windows 只负责窗口和启动。</div>
<label>WSL 发行版</label>
<select id="distro"></select>
<label>WSL 工作区</label>
<div class="row"><input id="workspace" placeholder="/home/you/agent-ws">
<button class="secondary" onclick="pickFolder()">选择 Windows 文件夹</button></div>
<div class="note" id="mode"></div>
<label>WSL Python（Agent Server 解释器）</label>
<input id="python" placeholder="/home/you/.coding-agent/venv/bin/python">
<div class="note" id="status">正在检测…</div>
<label><input type="checkbox" id="allowWrite" style="width:auto"> 允许会话选择“审批写/自动写”（默认仍为只读）</label>
<div style="margin-top:16px">
<button onclick="save()">保存并启动</button>
<button class="secondary" onclick="refresh()">重新检测</button>
</div>
<pre id="log"></pre>
<script>
async function api(){ return window.pywebview.api; }
async function refresh(){
  const d = await (await api()).detect();
  const sel=document.getElementById('distro'); sel.innerHTML='';
  for (const name of d.distros){ const o=document.createElement('option'); o.value=name; o.textContent=name; sel.appendChild(o); }
  if (sel.options.length===0){ const o=document.createElement('option'); o.value=''; o.textContent='（未检测到 WSL 发行版）'; sel.appendChild(o); }
  sel.value=d.distro||'';
  document.getElementById('workspace').value=d.workspace||'';
  document.getElementById('python').value=d.python||'';
  showMode(d.workspace||'');
  const st=document.getElementById('status');
  st.textContent=d.python_message||'';
  st.className=d.python_ok?'ok':'bad';
}
function showMode(path){
  const el=document.getElementById('mode');
  if(!path){ el.textContent=''; return; }
  if(path.indexOf('/mnt/')===0){
    el.textContent='提示：/mnt 是 Windows 盘挂载，读写较慢。进入主界面后可一键复制到 WSL 原生目录。';
    el.className='note bad';
  }else{
    el.textContent='位置：WSL 原生目录（推荐）';
    el.className='note ok';
  }
}
async function pickFolder(){
  const p=await (await api()).pick_folder();
  if(p){ document.getElementById('workspace').value=p; showMode(p); }
}
async function save(){
  const payload={distro:document.getElementById('distro').value,
    workspace:document.getElementById('workspace').value,
    python:document.getElementById('python').value,
    allow_write:document.getElementById('allowWrite').checked};
  const r=await (await api()).finish(payload);
  if(!r.ok){ document.getElementById('log').textContent=(r.errors||[]).join('\\n'); }
}
window.addEventListener('pywebviewready', refresh);
</script></body></html>"""


@dataclass
class _State:
    result: LauncherConfig | None = None


class SetupApi:
    def __init__(self) -> None:
        self.state = _State()

    def detect(self) -> dict:
        distros = list_distros()
        distro = distros[0] if distros else ""
        workspace = ""
        python = ""
        python_ok = False
        python_message = "未检测到 WSL 发行版"
        if distro:
            try:
                home_dir = home(distro)
                workspace = f"{home_dir}/.coding-agent/workspaces/default"
                candidates = [
                    f"{home_dir}/.coding-agent/venv/bin/python",
                    "python3",
                ]
                for candidate in candidates:
                    ok, detail = python_status(distro, candidate)
                    if ok:
                        python = candidate
                        python_ok = True
                        python_message = f"依赖检查通过：{candidate}"
                        break
                    python = python or candidate
                    python_message = (
                        f"{candidate}: {detail or '不可用'}\n"
                        "请先在 WSL 内安装项目依赖。"
                    )
            except Exception as exc:  # noqa: BLE001
                python_message = f"检测失败：{type(exc).__name__}: {exc}"
        return {
            "distros": distros,
            "distro": distro,
            "workspace": workspace,
            "python": python,
            "python_ok": python_ok,
            "python_message": python_message,
            "config_path": str(config_path()),
        }

    def pick_folder(self) -> str:
        try:
            import webview

            window = webview.windows[0]
            picked = window.create_file_dialog(webview.FileDialog.FOLDER)
            if picked:
                return to_wsl_path(picked[0])
        except Exception:  # noqa: BLE001
            pass
        return ""

    def finish(self, payload: dict) -> dict:
        try:
            data = payload if isinstance(payload, dict) else json.loads(payload)
        except (TypeError, ValueError):
            return {"ok": False, "errors": ["无效的配置"]}
        config = LauncherConfig(
            distro=str(data.get("distro") or ""),
            workspace=to_wsl_path(str(data.get("workspace") or "")),
            python=str(data.get("python") or ""),
            allow_write=bool(data.get("allow_write", False)),
        )
        errors = config.validate()
        if not errors and config.distro:
            try:
                ensure_dir(config.distro, config.workspace)
                ok, detail = python_status(config.distro, config.python)
                if not ok:
                    errors.append(
                        f"WSL Python 不可用：{detail or config.python}"
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")
        if errors:
            return {"ok": False, "errors": errors}
        self.state.result = config
        try:
            import webview

            webview.windows[0].destroy()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True}


def run_setup() -> LauncherConfig | None:
    try:
        import webview
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            '未安装桌面依赖。请运行：pip install -e ".[desktop]"'
        ) from exc
    api = SetupApi()
    webview.create_window(
        "CodingAgent 设置",
        html=_HTML,
        js_api=api,
        width=760,
        height=700,
        min_size=(620, 520),
    )
    webview.start()
    return api.state.result
