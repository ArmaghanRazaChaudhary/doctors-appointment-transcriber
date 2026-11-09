param([Parameter(Mandatory=$true)][string]$AudioPath)
$ErrorActionPreference = 'Stop'
$runRoot = $PSScriptRoot
$logDir = Join-Path $runRoot 'outputs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$statusPath = Join-Path $logDir 'launcher-status.txt'
Set-Content -LiteralPath $statusPath -Value 'waiting_for_key'
try {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    $form = New-Object System.Windows.Forms.Form
    $form.Text = 'Urdu ASR - OpenAI test'
    $form.Size = New-Object System.Drawing.Size(540,235)
    $form.StartPosition = 'CenterScreen'
    $form.TopMost = $true
    $label = New-Object System.Windows.Forms.Label
    $label.Location = New-Object System.Drawing.Point(18,18)
    $label.Size = New-Object System.Drawing.Size(495,65)
    $label.Text = "Paste your OpenAI API key below. It stays in memory for this run.`nRun test sends this recording to OpenAI using your API credit:`n$([IO.Path]::GetFileName($AudioPath))"
    $inputBox = New-Object System.Windows.Forms.TextBox
    $inputBox.Location = New-Object System.Drawing.Point(18,90)
    $inputBox.Size = New-Object System.Drawing.Size(485,25)
    $inputBox.UseSystemPasswordChar = $true
    $button = New-Object System.Windows.Forms.Button
    $button.Text = 'Run test'
    $button.Location = New-Object System.Drawing.Point(390,140)
    $button.DialogResult = [System.Windows.Forms.DialogResult]::OK
    $form.Controls.AddRange(@($label,$inputBox,$button))
    $form.AcceptButton = $button
    if ($form.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK -or [string]::IsNullOrWhiteSpace($inputBox.Text)) {
        Set-Content -LiteralPath $statusPath -Value 'cancelled'
        exit
    }
    $env:OPENAI_API_KEY = $inputBox.Text.Trim()
    $inputBox.Clear()
    $form.Dispose()
    Set-Content -LiteralPath $statusPath -Value 'running'
    $pythonPath = Join-Path $runRoot '.venv\Scripts\python.exe'
    $appPath = Join-Path $runRoot 'app.py'
    $process = Start-Process -FilePath $pythonPath -ArgumentList @('-u', ('"' + $appPath + '"'), '--audio', ('"' + $AudioPath + '"')) -WindowStyle Hidden -PassThru -Wait -RedirectStandardOutput (Join-Path $logDir 'latest-stdout.txt') -RedirectStandardError (Join-Path $logDir 'latest-stderr.txt')
    Set-Content -LiteralPath $statusPath -Value "finished_exit_$($process.ExitCode)"
} catch {
    Set-Content -LiteralPath $statusPath -Value 'launcher_failed'
} finally {
    Remove-Item Env:OPENAI_API_KEY -ErrorAction SilentlyContinue
}
