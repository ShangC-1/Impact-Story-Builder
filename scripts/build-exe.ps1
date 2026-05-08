param(
  [switch]$Clean
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
  throw "Python was not found. Install Python or update scripts/build-exe.ps1 with a valid interpreter path."
}

function Invoke-PythonModule {
  param(
    [string[]]$Arguments
  )

  if ($pythonCommand -eq "py") {
    & py @Arguments
  } else {
    & $pythonCommand @Arguments
  }
}

Set-Location $repoRoot
Write-Host "Building Impact Story Builder Windows app from $repoRoot" -ForegroundColor Cyan

Invoke-PythonModule -Arguments @("-m", "pip", "install", "pyinstaller")
if ($LASTEXITCODE -ne 0) {
  throw "Unable to install PyInstaller."
}

$buildArgs = @("-m", "PyInstaller", "--noconfirm")
if ($Clean) {
  $buildArgs += "--clean"
}
$buildArgs += "ImpactStoryBuilder.spec"

Invoke-PythonModule -Arguments $buildArgs
if ($LASTEXITCODE -ne 0) {
  throw "PyInstaller build failed."
}

Write-Host "Build complete." -ForegroundColor Green
Write-Host "Executable: $repoRoot\dist\Impact Story Builder.exe" -ForegroundColor Yellow
