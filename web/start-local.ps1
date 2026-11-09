param([switch]$Rebuild, [int]$Port = 8766)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$pythonExe = Join-Path (Split-Path $PSScriptRoot) 'openai_trial\.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $pythonExe)) { throw 'The openai_trial Python environment was not found.' }
if ($Rebuild -or !(Test-Path 'dist\client\index.html')) {
    $nodeExe = (Get-Command node).Source   # Node 22+
    & $nodeExe node_modules\vinext\dist\cli.js build
    if ($LASTEXITCODE -ne 0) { throw 'Build failed.' }
}
Write-Host "Open http://localhost:$Port in Chrome or Edge. Keep this console running."
& $pythonExe backend\server.py --port $Port
