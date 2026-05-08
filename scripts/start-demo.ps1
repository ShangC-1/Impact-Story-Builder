param(
  [int]$Port = 4173,
  [ValidateSet("manual_invite", "local_dev")]
  [string]$AuthMode = "manual_invite",
  [string]$DevUserEmail = "dev@local"
)

$repoRoot = Split-Path -Parent $PSScriptRoot
$bundledPython = "C:\Users\RoooC\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

if (Test-Path $bundledPython) {
  $pythonCommand = $bundledPython
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
  $pythonCommand = (Get-Command python).Source
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
  $pythonCommand = "py"
} else {
  throw "Python was not found. Install Python or update scripts/start-demo.ps1 with a valid interpreter path."
}

Set-Location $repoRoot
$env:AUTH_MODE = $AuthMode
$env:DEV_USER_EMAIL = $DevUserEmail
Write-Host "Serving Impact Story Builder from $repoRoot" -ForegroundColor Cyan
Write-Host "Open http://127.0.0.1:$Port in your browser. Press Ctrl+C to stop the server." -ForegroundColor Yellow
Write-Host "The server reads panel defaults from your environment or a local .env file, but API keys are entered in the UI and kept in memory only." -ForegroundColor DarkYellow
Write-Host "Auth mode: $AuthMode" -ForegroundColor DarkCyan
if ($AuthMode -eq "manual_invite") {
  Write-Host "Manual Invite Pilot mode expects DEMO_ALLOWED_EMAILS and DEMO_SHARED_PASSWORD from your environment or .env file." -ForegroundColor DarkYellow
}

if ($pythonCommand -eq "py") {
  & py .\server.py --host 127.0.0.1 --port $Port
} else {
  & $pythonCommand .\server.py --host 127.0.0.1 --port $Port
}
