<#
  stira_ctl.ps1 - stira_server (StiraCore 3.11+sokudan) の起動/確認/停止を1コマンドに。

  使い方:
    powershell -ExecutionPolicy Bypass -File scripts\stira_ctl.ps1 start
    powershell -ExecutionPolicy Bypass -File scripts\stira_ctl.ps1 status
    powershell -ExecutionPolicy Bypass -File scripts\stira_ctl.ps1 stop
    powershell -ExecutionPolicy Bypass -File scripts\stira_ctl.ps1 restart
    powershell -ExecutionPolicy Bypass -File scripts\stira_ctl.ps1 distill

  設計メモ:
    - distill はサーバーを起動せず同期CLIを実行する。既定rule modeはBedrock課金ゼロ。
    - start は投げっぱなし起動。health は status で別途確認する。
    - .venv311\Scripts\python.exe は uv python 実体への薄いラッパで、起動すると必ず 2 PID (ランチャ+実体) 見える。
      片方を Force kill すると親子連鎖で両方落ちる。だから停止は「PID個別 kill」ではなく taskkill /T (ツリー一括) で行う。
    - 停止対象はプロセス名とこの checkout の絶対スクリプトパスの両方で確認する。ポート所有だけでは停止しない。
#>

param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "status", "stop", "restart", "distill")]
    [string]$Action = "status"
)

# 外部コマンド(taskkill)が already-dead な PID に空振りしても全体を止めない。
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Root ".venv311\Scripts\python.exe"
$Entry = Join-Path $Root "dashboard\stira_server.py"
$Port = if ($env:STIRA_PORT) { [int]$env:STIRA_PORT } else { 5555 }
$Base = "http://127.0.0.1:$Port"
$OutLog = Join-Path $Root ".local\stira.out.log"
$ErrLog = Join-Path $Root ".local\stira.err.log"
# STIRA_DATA_DIR (#272): the server's data directory when set. Checked by the server's
# own validator (the same rules: fully qualified only, so C:data and \data are refused;
# blank = unset); a refusal prints the reason only, never the value.
$StateDir = Join-Path $Root ".local\stira"
if ($env:STIRA_DATA_DIR) {
    $savedPythonPath = $env:PYTHONPATH
    $env:PYTHONPATH = (Join-Path $Root "src") + $(if ($savedPythonPath) { [System.IO.Path]::PathSeparator + $savedPythonPath } else { "" })
    $checked = @(& $Py -m stiracore.config.data_dir_check 2>&1 | ForEach-Object { "$_" })
    $checkCode = $LASTEXITCODE
    $env:PYTHONPATH = $savedPythonPath
    if ($checkCode -ne 0) {
        $reason = @($checked | Where-Object { $_ -like "Stira: *" })
        if ($reason) { [Console]::Error.WriteLine($reason[0]) }
        else { [Console]::Error.WriteLine("stira: STIRA_DATA_DIR could not be checked") }
        exit 2
    }
    $line = @($checked | Where-Object { $_ -like "data_dir=*" }) | Select-Object -Last 1
    if ($line -and $line.Length -gt 9) { $StateDir = $line.Substring(9) }
}
$MemoryBase = Join-Path $StateDir "memory"
$AgentConfig = Join-Path $StateDir "agent-config.json"

function Test-StiraProcess($ProcessInfo) {
    # Win32_Process does not expose CWD. Require our absolute entrypoint as a
    # complete argument (not a basename/subdirectory/prefix match). Older
    # relative-path launches are deliberately left alone: ownership is unknown.
    if (-not $ProcessInfo -or $ProcessInfo.Name -notmatch '(?i)(python|stira)') {
        return $false
    }
    if ([string]::IsNullOrWhiteSpace($ProcessInfo.CommandLine)) { return $false }
    $command = $ProcessInfo.CommandLine.Replace('/', '\')
    $entryPattern = [regex]::Escape($Entry.Replace('/', '\'))
    return $command -match ('(?i)(?:^|\s)(?:"' + $entryPattern + '"|' + $entryPattern + ')(?=\s|$)')
}

function Get-StiraPids {
    Get-CimInstance Win32_Process |
        Where-Object { Test-StiraProcess $_ } |
        Select-Object -ExpandProperty ProcessId
}

function Get-PortOwner {
    (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue).OwningProcess
}

function Show-Status {
    $owner = Get-PortOwner
    if (-not $owner) {
        Write-Host "stira: DOWN (port $Port free)"
        return $false
    }
    try {
        $r = Invoke-RestMethod -Uri "$Base/api/gateway/status" -TimeoutSec 5
        Write-Host "stira: UP (pid $owner, port $Port)"
        Write-Host "breaker: $($r.breaker.level)   model: $($r.model)"
        Write-Host "Neural State:"
        $ns = $r.neural_state
        foreach ($k in ($ns.PSObject.Properties.Name | Sort-Object)) {
            "{0,-13} {1}" -f $k, $ns.$k | Write-Host
        }
        return $true
    }
    catch {
        Write-Host "stira: process up (pid $owner) but HTTP not ready yet: $($_.Exception.Message)"
        return $false
    }
}

function Stop-Stira {
    $pids = @(Get-StiraPids)
    $owner = Get-PortOwner
    if ($owner) { $pids += $owner }
    $pids = $pids | Sort-Object -Unique
    if (-not $pids) {
        Write-Host "stira: already down"
        return
    }
    foreach ($procId in $pids) {
        # Re-query immediately before termination, including port-derived PIDs.
        # Fail closed when identity/command line cannot be read.
        $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue
        if (-not (Test-StiraProcess $processInfo)) {
            Write-Warning "Skipping pid ${procId}: not a verified chat server process for this checkout."
            continue
        }
        # /T = プロセスツリー一括。親子ラッパ連鎖を取りこぼさない。
        taskkill /PID $procId /T /F 2>$null | Out-Null
    }
    Start-Sleep -Seconds 2
    if (Get-PortOwner) { Write-Host "stira: STOP failed (port still held)" }
    else { Write-Host "stira: STOPPED (killed: $($pids -join ', '))" }
}

function Start-Stira {
    if (Get-PortOwner) {
        Write-Host "stira: already UP on port $Port -- use 'restart' to relaunch"
        return
    }
    $env:STIRA_PORT = "$Port"
    New-Item -ItemType Directory -Force -Path (Split-Path $OutLog) | Out-Null
    Start-Process -FilePath $Py -ArgumentList ('"{0}"' -f $Entry) -WorkingDirectory $Root `
        -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog -WindowStyle Hidden | Out-Null
    # 投げっぱなしで即戻る。health/Neural State は別途 'status' で確認する
    # (health待ちループを持たせると 1 ツール呼びが最大数十秒ブロックして「無限に待つ」ように見えるため)。
    Write-Host "stira: launch requested on port $Port (3.11+sokudan)."
    Write-Host "  sokudan の初回ロードで health まで ~10-30s。確認は: stira_ctl.ps1 status"
}

function Invoke-Distillation {
    # Distillation is a finite synchronous command, not a server. It neither
    # probes nor owns port 5555 and must not use Start-Process.
    & $Py -m stiracore.control.distillation_cli `
        --base-dir $MemoryBase --agent-id stira --config $AgentConfig
    if ($LASTEXITCODE -ne 0) {
        throw "distillation failed with exit code $LASTEXITCODE"
    }
}

switch ($Action) {
    "start"   { Start-Stira }
    "status"  { Show-Status | Out-Null }
    "stop"    { Stop-Stira }
    "restart" { Stop-Stira; Start-Stira }
    "distill" { Invoke-Distillation }
}
