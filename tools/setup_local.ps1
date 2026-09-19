param(
    [string]$Python = '',
    [switch]$Dev
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$pythonArgs = @()
if ([string]::IsNullOrWhiteSpace($Python)) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $Python = 'py'
        $pythonArgs = @('-3.11')
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        $Python = 'python'
    } else {
        throw 'Install Python 3.11 (64-bit), reopen PowerShell and retry.'
    }
}
& $Python @pythonArgs -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 11) and sys.maxsize > 2**32 else 1)"
if ($LASTEXITCODE -ne 0) {
    throw 'Python 3.11 (64-bit) is required. Use -Python with its full executable path.'
}
$venvDir = Join-Path $projectRoot '.venv-runtime'
$venvPython = Join-Path $venvDir 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    if (Test-Path -LiteralPath $venvDir) {
        throw 'An incomplete .venv-runtime exists. Rename it before retrying; setup will not delete it.'
    }
    & $Python @pythonArgs -m venv $venvDir
    if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
}
& $venvPython -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 11) and sys.maxsize > 2**32 else 1)"
if ($LASTEXITCODE -ne 0) { throw 'Existing virtual environment is not Python 3.11 (64-bit).' }
$installArgs = @('-m', 'pip', 'install', '-r', 'requirements.txt', '-r', 'requirements-import.txt')
if ($Dev) { $installArgs += @('-r', 'requirements-dev.txt') }
& $venvPython @installArgs
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check network access and pip output.' }
$env:PYTHONPATH = Join-Path $projectRoot 'src'
& $venvPython -c "import ortools, jsonschema, openpyxl; from planpilot.domain.importer import load_factory; from planpilot.domain.planning import build_candidates; plans=build_candidates(load_factory('examples/factory_demo.json')); assert len(plans)==3; print('PASS: imports and three local solver candidates')"
if ($LASTEXITCODE -ne 0) { throw 'Local solver smoke check failed.' }
Write-Host 'Setup complete. Run: powershell -ExecutionPolicy Bypass -File .\tools\start_local.ps1' -ForegroundColor Green
