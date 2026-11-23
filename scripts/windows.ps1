param(
    [ValidateSet("api", "document", "checklist", "scoring", "download", "smoke", "native-smoke",
                 "v2-api", "v2-document", "v2-checklist", "v2-scoring")]
    [string]$Action = "smoke",
    [string]$PythonExe,
    [switch]$LiveLlm,
    [ValidateRange(1, 10)]
    [int]$Repeat = 1
)
$ErrorActionPreference = "Stop"
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
Push-Location -LiteralPath $root
try {
    if (!$PythonExe) {
        $venvPython = Join-Path $root ".venv\Scripts\python.exe"
        if (Test-Path -LiteralPath $venvPython) {
            $PythonExe = $venvPython
        } elseif (Test-Path -LiteralPath ".local\deps") {
            $runtimeBin = $env:Path.Split(";") | Where-Object { $_ -like "*CodexPrimaryRuntime*\dependencies\*" } |
                Select-Object -First 1
            if ($runtimeBin) {
                $runtimeRoot = $runtimeBin -replace "\\dependencies\\.*$", ""
                $PythonExe = Join-Path $runtimeRoot "dependencies\python\python.exe"
                $workspacePaths = @(".local\http-stack", ".local\deps", ".local\vl", ".local\ocr", ".local\agents") |
                    ForEach-Object { Join-Path $root $_ } | Where-Object { Test-Path -LiteralPath $_ }
                $env:PYTHONPATH = (@($workspacePaths) + @($env:PYTHONPATH) | Where-Object { $_ }) -join ";"
                # Limit Paddle's normally per-user cache to this process and workspace.
                $env:USERPROFILE = Join-Path $root ".local\runtime-profile"
                [IO.Directory]::CreateDirectory($env:USERPROFILE) | Out-Null
            }
        }
        if (!$PythonExe) { $PythonExe = "python" }
    }
    $temp = Join-Path $root ".local\runtime-temp"
    [IO.Directory]::CreateDirectory($temp) | Out-Null
    $env:TMP = $temp
    $env:TEMP = $temp
    $env:PYTHONUTF8 = "1"
    $env:PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK = "True"
    $env:HF_HUB_DISABLE_XET = "1"
    $argsLlm = @()
    if ($LiveLlm) { $argsLlm += "--live-llm" }
    switch ($Action) {
        "v2-api" { & $PythonExe -m uvicorn app.v2.api:app --host 127.0.0.1 --port 8000 }
        { $_.StartsWith("v2-") -and $_ -ne "v2-api" } { & $PythonExe -m app.v2.worker $Action.Substring(3) }
        "api" { & $PythonExe -m uvicorn app.api:app --host 127.0.0.1 --port 8000 }
        "download" { & $PythonExe -m scripts.download_models }
        "smoke" { & $PythonExe -u -m scripts.api_smoke --live-ocr --repeat $Repeat @argsLlm }
        "native-smoke" { & $PythonExe -u -m scripts.api_smoke --repeat $Repeat @argsLlm }
        default { & $PythonExe -m scripts.local_worker $Action }
    }
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE)" }
} finally {
    Pop-Location
}
