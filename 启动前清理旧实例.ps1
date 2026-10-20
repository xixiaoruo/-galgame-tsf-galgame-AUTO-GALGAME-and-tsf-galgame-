[CmdletBinding()]
param(
    [switch]$DryRun
)

# Unified launcher guard. Only targets this game's exact executable name and
# source entry scripts; never kills generic Python, cmd, browser, or port owners.
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$rootPattern = [regex]::Escape((Get-Item -LiteralPath $root).FullName.TrimEnd('\'))
$sourcePattern = '(?i)(?:server|window_host)\.py'
$targets = @{}

try {
    $processes = Get-CimInstance Win32_Process -Filter "Name = 'TSF_Galgame.exe' OR Name = 'python.exe' OR Name = 'pythonw.exe'"
} catch {
    Write-Error "Cannot inspect running processes: $($_.Exception.Message)"
    exit 1
}

foreach ($process in $processes) {
    $name = [string]$process.Name
    $path = [string]$process.ExecutablePath
    $commandLine = [string]$process.CommandLine
    $isTarget = $false

    if ($name -ieq 'TSF_Galgame.exe') {
        # The exact image name is the project's published executable name.
        $isTarget = $true
    } elseif ($commandLine -and $commandLine -match $rootPattern -and
              $commandLine -match $sourcePattern) {
        # Source-mode compatibility: only server.py/window_host.py under this root.
        $isTarget = $true
    }

    if ($isTarget -and -not $targets.ContainsKey([int]$process.ProcessId)) {
        $targets[[int]$process.ProcessId] = [pscustomobject]@{
            Pid = [int]$process.ProcessId
            Name = $name
            Path = $path
            CommandLine = $commandLine
        }
    }
}

if ($targets.Count -eq 0) {
    Write-Output 'No previous TSF Galgame instance found.'
    exit 0
}

foreach ($target in $targets.Values) {
    if ($DryRun) {
        Write-Output ("[DRY-RUN] Would terminate PID {0}: {1}" -f $target.Pid, $target.Name)
        continue
    }
    Write-Output ("Terminating PID {0}: {1}" -f $target.Pid, $target.Name)
    & "$env:SystemRoot\System32\taskkill.exe" /PID $target.Pid /T /F *> $null
    if ($LASTEXITCODE -ne 0) {
        # PyInstaller 的父/子进程可能在同一 /T 请求中互相退出；
        # 只有 PID 仍存在才算真正失败。
        if (Get-Process -Id $target.Pid -ErrorAction SilentlyContinue) {
            Write-Error ("Failed to terminate PID {0}" -f $target.Pid)
            exit 1
        }
    }
}

if (-not $DryRun) {
    Start-Sleep -Milliseconds 800
}
exit 0
