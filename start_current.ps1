[CmdletBinding()]
param()

$root = (Get-Item -LiteralPath $PSScriptRoot).FullName.TrimEnd('\')
# Prefer the 1.9.20 build (AI console / editable dialogue / director directive).
# Older dist dirs may still be locked by a running instance, so the new build
# has its own directory.
$targetDir = Join-Path $root 'dist-1.9.20\TSF_Galgame'
if (-not (Test-Path -LiteralPath (Join-Path $targetDir 'TSF_Galgame.exe') -PathType Leaf)) {
    $targetDir = Join-Path $root 'dist\TSF_Galgame'
}
$target = Join-Path $targetDir 'TSF_Galgame.exe'
if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
    Write-Error "Current onedir executable not found: $target"
    exit 1
}

function Get-TsfProcesses {
    $rootPrefix = $root + '\'
    $items = @()
    try {
        $items = @(Get-CimInstance Win32_Process -Filter "Name = 'TSF_Galgame.exe' OR Name = 'python.exe' OR Name = 'pythonw.exe'")
    } catch {
        throw "Cannot inspect running processes: $($_.Exception.Message)"
    }
    foreach ($item in $items) {
        $name = [string]$item.Name
        $path = [string]$item.ExecutablePath
        $cmd = [string]$item.CommandLine
        $match = $false
        if ($name -ieq 'TSF_Galgame.exe') {
            $match = (($path -and $path.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) -or
                      (-not $path -and $cmd -and $cmd -match [regex]::Escape($rootPrefix)))
        } elseif ($cmd -and $cmd.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase) -and
                  $cmd -match '(?i)(?:server|window_host)\.py') {
            $match = $true
        }
        if ($match) { $item }
    }
}

try {
    $old = @(Get-TsfProcesses)
    foreach ($item in $old) {
        Write-Output ("Stopping previous TSF instance PID {0}: {1}" -f $item.ProcessId, $item.Name)
        & "$env:SystemRoot\System32\taskkill.exe" /PID ([int]$item.ProcessId) /T /F *> $null
    }
    Start-Sleep -Milliseconds 900
    $left = @(Get-TsfProcesses)
    if ($left.Count -gt 0) {
        $ids = ($left | ForEach-Object { $_.ProcessId }) -join ', '
        throw "Previous TSF instance(s) still running: $ids"
    }
    Start-Process -FilePath $target -WorkingDirectory $targetDir
    Write-Output ("Started current TSF onedir executable: {0}" -f $target)
    exit 0
} catch {
    Write-Error $_.Exception.Message
    exit 1
}
