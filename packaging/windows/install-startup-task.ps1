# 注册 UBISmart native 进程为 Windows 开机任务（SYSTEM）。
# 这不是 SCM 服务：进程由 Task Scheduler 启动/重启；需要真正的 Windows
# Service 时可在外层套 NSSM 或 pywin32 服务包装。
#
#   powershell -ExecutionPolicy Bypass -File install-startup-task.ps1 -Target agent
#   powershell -ExecutionPolicy Bypass -File install-startup-task.ps1 -Target control
#   powershell ... -Target agent -Remove
param(
  [Parameter(Mandatory = $true)]
  [ValidateSet("agent", "control")]
  [string]$Target,
  [string]$InstallRoot = "$env:LOCALAPPDATA\Programs\CodingAgent",
  [string]$DataRoot = "$env:PROGRAMDATA\CodingAgent",
  [switch]$Remove
)

$ErrorActionPreference = "Stop"
$taskName = "CodingAgent-$Target"

if ($Remove) {
  Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
  Write-Host "已移除任务 $taskName"
  exit 0
}

$exeName = if ($Target -eq "agent") { "UBISmartAgent" } else { "UBISmartControl" }
$exe = Join-Path $InstallRoot "$exeName\$exeName.exe"
if (-not (Test-Path $exe)) {
  throw "未找到 $exe；请先用 scripts/build_services.py --target $Target 构建并复制到 InstallRoot"
}

$envFile = Join-Path $DataRoot "$Target.env"
if (-not (Test-Path $envFile)) {
  New-Item -ItemType Directory -Force -Path $DataRoot | Out-Null
  Copy-Item "$PSScriptRoot\$Target.env.example" $envFile
  icacls $envFile /inheritance:r /grant:r "SYSTEM:(R)" "Administrators:(R)" | Out-Null
  Write-Host "已创建 $envFile；请填入 token/数据库后再运行本脚本。"
  exit 2
}

$port = if ($Target -eq "agent") { 8790 } else { 8780 }
$wrapper = Join-Path $DataRoot "run-$Target.ps1"
$wrapperContent = @"
`$ErrorActionPreference = 'Stop'
Get-Content '$envFile' | Where-Object { `$_ -match '^[A-Za-z_][A-Za-z0-9_]*=' } | ForEach-Object {
  `$k, `$v = `$_ -split '=', 2
  Set-Item -Path "Env:`$k" -Value `$v
}
& '$exe' --host 127.0.0.1 --port $port
"@
Set-Content -Path $wrapper -Value $wrapperContent -Encoding UTF8

$action = New-ScheduledTaskAction `
  -Execute "powershell.exe" `
  -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$wrapper`""
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
  -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
  -StartWhenAvailable
Register-ScheduledTask `
  -TaskName $taskName -Action $action -Trigger $trigger `
  -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "已注册开机任务 $taskName；日志请重定向或查看任务历史。"
