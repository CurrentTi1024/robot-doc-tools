param(
    [ValidateSet("v2-api", "v2-document", "v2-checklist", "v2-scoring", "test")]
    [string]$Action = "test",
    [string]$PythonExe
)

$ErrorActionPreference = "Stop"
Push-Location -LiteralPath (Join-Path $PSScriptRoot "..")
try {
    if (!$PythonExe) {
        $venvPython = Join-Path (Get-Location).Path ".venv\Scripts\python.exe"
        $PythonExe = if (Test-Path -LiteralPath $venvPython) { $venvPython } else { "python" }
    }
    $env:PYTHONUTF8 = "1"
    switch ($Action) {
        "v2-api" { & $PythonExe -m uvicorn app.v2.api:app --host 127.0.0.1 --port 8000 }
        "test" { & $PythonExe -m unittest scripts.test_v2 }
        default { & $PythonExe -m app.v2.worker $Action.Substring(3) }
    }
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE)" }
} finally {
    Pop-Location
}
