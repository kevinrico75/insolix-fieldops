$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$Host.UI.RawUI.WindowTitle = 'Rico FieldOps'

Write-Host ''
Write-Host '=============================================='
Write-Host '       RICO FIELDOPS - STARTING'
Write-Host '=============================================='
Write-Host ''

$python = $null
try {
    $python = (& py -3 -c "import sys; print(sys.executable)" 2>$null | Select-Object -First 1)
} catch {}
if (-not $python) {
    try {
        $python = (& python -c "import sys; print(sys.executable)" 2>$null | Select-Object -First 1)
    } catch {}
}

if (-not $python) {
    Write-Host 'ERROR: Python 3 is not installed or Windows cannot find it.'
    Write-Host 'Install Python 3.11+ from https://www.python.org/downloads/windows/'
    Write-Host 'During setup, check "Add python.exe to PATH".'
    Read-Host 'Press Enter to close'
    exit 1
}

Write-Host "Python found: $python"

if (-not (Test-Path '.venv\Scripts\python.exe')) {
    Write-Host 'First-time setup: creating local environment...'
    & $python -m venv .venv
}

$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$depsOk = $true
try { & $venvPython -c "import fastapi,uvicorn,jinja2,multipart" 2>$null } catch { $depsOk = $false }
if ($LASTEXITCODE -ne 0) { $depsOk = $false }

if (-not $depsOk) {
    Write-Host 'Installing required packages. This only needs to happen once...'
    & $venvPython -m pip install --upgrade pip
    & $venvPython -m pip install -r requirements.txt
}

Write-Host ''
Write-Host 'Rico FieldOps is starting at http://127.0.0.1:8000'
Write-Host 'Keep this window open while using the program.'
Start-Job -ScriptBlock { Start-Sleep 2; Start-Process 'http://127.0.0.1:8000' } | Out-Null
& $venvPython -m uvicorn app:app --host 127.0.0.1 --port 8000
Read-Host 'FieldOps stopped. Press Enter to close'
