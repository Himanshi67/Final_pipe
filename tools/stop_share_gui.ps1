<#
.SYNOPSIS
    Stop the share-mode GUI that tools\share_gui.ps1 started.

.DESCRIPTION
    Reads the PID and the start time from runs\_share\share_info.txt. It
    kills that process tree only when the process start time matches the
    recorded one (a reused PID is never killed). Then it checks that the
    port is closed and deletes share_info.txt, so the password does not stay
    on disk.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$infoFile = Join-Path $repo 'runs\_share\share_info.txt'

if (-not (Test-Path -LiteralPath $infoFile)) {
    Write-Host "No share session found ($infoFile does not exist)."
    exit 0
}

$info = @{}
foreach ($line in Get-Content -LiteralPath $infoFile -Encoding UTF8) {
    $i = $line.IndexOf('=')
    if ($i -gt 0) { $info[$line.Substring(0, $i)] = $line.Substring($i + 1) }
}

$procId = 0
if (-not [int]::TryParse([string]$info['pid'], [ref]$procId) -or $procId -le 0) {
    throw "No valid pid in $infoFile."
}
$port = 0
if ($info['url'] -match ':(\d+)$') { $port = [int]$Matches[1] }

$p = $null
try { $p = Get-Process -Id $procId -ErrorAction Stop } catch { $p = $null }
if ($p) {
    $recorded = [datetime]::MinValue
    $same = [datetime]::TryParse([string]$info['started'], [ref]$recorded) -and
            ([math]::Abs(($p.StartTime - $recorded).TotalSeconds) -lt 2)
    if ($same) {
        & "$env:SystemRoot\System32\taskkill.exe" /PID $procId /T /F | Out-Null
        Write-Host "Stopped process tree $procId."
    }
    else {
        Write-Warning "PID $procId now belongs to another process ($($p.ProcessName)). It was NOT killed."
    }
}
else {
    Write-Host "Process $procId is not running."
}

if ($port -gt 0) {
    $deadline = (Get-Date).AddSeconds(15)
    $open = $true
    while ((Get-Date) -lt $deadline) {
        $open = [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
        if (-not $open) { break }
        Start-Sleep -Milliseconds 500
    }
    if ($open) {
        Write-Warning "Port $port is still listening. Check it with: Get-NetTCPConnection -LocalPort $port"
        exit 1
    }
    Write-Host "Port $port is closed."
}

Remove-Item -LiteralPath $infoFile -Force
Write-Host "Deleted $infoFile."
