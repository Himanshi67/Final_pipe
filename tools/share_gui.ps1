<#
.SYNOPSIS
    Start the pipeline GUI (gui.py) in share mode, on THIS PC ONLY.

.DESCRIPTION
    Binds Streamlit to 127.0.0.1 (loopback). No other computer can connect.
    This script starts NO tunnel and downloads nothing. A public address
    needs a separate tunnel step that the owner starts and stops.

    Share mode (docs\PIPELINE_GUIDE.md "Share mode"):
      - a password gate before anything renders;
      - publish, run delete, run archive, taxonomy edits and the client
        switch are off;
      - every enrichment run is capped at -Budget Serper credits, with no
        headless browser and no --force.

    The three GUI_SHARE_* variables are set for the child process ONLY, not
    for this PowerShell session.

    Writes runs\_share\share_info.txt (URL, password, PID, start time). runs\
    is gitignored. The password is NOT printed; read it from that file.
    Stop with tools\stop_share_gui.ps1 (it also deletes that file).

.EXAMPLE
    .\tools\share_gui.ps1
.EXAMPLE
    .\tools\share_gui.ps1 -Port 8502 -Budget 30
#>
[CmdletBinding()]
param(
    [string]$Password,
    [ValidateRange(1024, 65535)][int]$Port = 8501,
    [ValidateRange(1, 100000)][int]$Budget = 50,
    [ValidateRange(1, 200)][int]$MaxUploadMB = 20
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo '.venv\Scripts\python.exe'
$gui = Join-Path $repo 'gui.py'
$shareDir = Join-Path $repo 'runs\_share'
$infoFile = Join-Path $shareDir 'share_info.txt'
$logFile = Join-Path $shareDir 'streamlit.log'

if (-not (Test-Path -LiteralPath $python)) { throw "Python not found: $python" }
if (-not (Test-Path -LiteralPath $gui)) { throw "gui.py not found: $gui" }

if ($Password -and $Password.Length -lt 12) {
    throw 'The password must have at least 12 characters.'
}

if (Test-Path -LiteralPath $infoFile) {
    throw "$infoFile exists. A share session may be running. Run tools\stop_share_gui.ps1 first."
}

$listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listening) { throw "Port $Port is already in use. Use -Port to pick another port." }

if (-not $Password) {
    # 24 characters from a 56-symbol alphabet (no 0/O/1/l/I), about 139 bits.
    # Rejection sampling keeps every symbol equally likely.
    $alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789'
    $limit = 256 - (256 % $alphabet.Length)
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $buf = New-Object byte[] 1
    $chars = New-Object System.Text.StringBuilder
    try {
        while ($chars.Length -lt 24) {
            $rng.GetBytes($buf)
            if ($buf[0] -lt $limit) {
                [void]$chars.Append($alphabet[$buf[0] % $alphabet.Length])
            }
        }
    }
    finally { $rng.Dispose() }
    $Password = $chars.ToString()
}

New-Item -ItemType Directory -Force -Path $shareDir | Out-Null

$streamlitArgs = @(
    '-m', 'streamlit', 'run', "`"$gui`"",
    '--server.address', '127.0.0.1',
    '--server.port', "$Port",
    '--server.headless', 'true',
    '--browser.gatherUsageStats', 'false',
    '--server.maxUploadSize', "$MaxUploadMB",
    '--server.enableXsrfProtection', 'true',
    '--server.enableCORS', 'true',
    '--server.fileWatcherType', 'none',
    '--server.runOnSave', 'false',
    '--client.showErrorDetails', 'type',
    '--client.toolbarMode', 'viewer'
) -join ' '

# cmd.exe redirects the server output to a log file (an unread pipe would
# fill and stop the server). The stop script kills the whole PID tree.
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = "$env:SystemRoot\System32\cmd.exe"
$psi.Arguments = "/d /c `"`"$python`" $streamlitArgs > `"$logFile`" 2>&1`""
$psi.WorkingDirectory = $repo
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
# child-only environment: this session's variables do not change
$psi.EnvironmentVariables['GUI_SHARE_MODE'] = '1'
$psi.EnvironmentVariables['GUI_SHARE_PASSWORD'] = $Password
$psi.EnvironmentVariables['GUI_SHARE_BUDGET'] = "$Budget"
$psi.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
$psi.EnvironmentVariables['STREAMLIT_BROWSER_GATHER_USAGE_STATS'] = 'false'

$proc = [System.Diagnostics.Process]::Start($psi)
$started = $proc.StartTime
$url = "http://127.0.0.1:$Port"

@(
    "url=$url"
    "password=$Password"
    "pid=$($proc.Id)"
    "started=$($started.ToString('yyyy-MM-ddTHH:mm:ss.fff'))"
    "budget=$Budget"
    "log=$logFile"
) | Set-Content -LiteralPath $infoFile -Encoding UTF8
$Password = $null

# wait up to 60 s for the server to listen on loopback
$deadline = (Get-Date).AddSeconds(60)
$up = $false
while ((Get-Date) -lt $deadline) {
    if ($proc.HasExited) { break }
    if (Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
        $up = $true
        break
    }
    Start-Sleep -Milliseconds 500
}

if (-not $up) {
    Write-Warning "The server did not start listening on $url. See $logFile. Run tools\stop_share_gui.ps1 to clean up."
    exit 1
}

Write-Host "Share mode GUI is running on this PC only: $url"
Write-Host "Password and PID are in: $infoFile"
Write-Host "Stop it with: .\tools\stop_share_gui.ps1"
