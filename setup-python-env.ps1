param(
    [string]$Project = (Get-Location).Path,
    [switch]$InstallRequirements
)

$ErrorActionPreference = 'Stop'
$projectPath = (Resolve-Path -LiteralPath $Project).Path
if (-not (Test-Path -LiteralPath $projectPath -PathType Container)) {
    throw "Project path is not a directory: $projectPath"
}

$envDir = Join-Path $projectPath '.venv'
$envPython = Join-Path $envDir 'Scripts\python.exe'
if (Test-Path -LiteralPath $envDir) {
    if (-not (Test-Path -LiteralPath $envPython -PathType Leaf)) {
        throw "The existing .venv has no Scripts\python.exe; it was left untouched: $envDir"
    }
    Write-Host "Using existing environment: $envDir"
} else {
    $pythonCommand = Get-Command python -CommandType Application -ErrorAction Stop | Select-Object -First 1
    & $pythonCommand.Source -m venv $envDir
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the virtual environment.' }
    Write-Host "Created environment: $envDir"
}

& $envPython -c 'import sys; print(sys.executable)'
if ($LASTEXITCODE -ne 0) { throw 'The virtual environment Python could not start.' }

if ($InstallRequirements) {
    $requirements = Join-Path $projectPath 'requirements.txt'
    if (-not (Test-Path -LiteralPath $requirements -PathType Leaf)) {
        throw "No requirements.txt found: $requirements"
    }
    & $envPython -m pip install -r $requirements
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
}

Write-Host 'Open the project folder in VS Code. Select .venv with Python: Select Interpreter if needed.'
