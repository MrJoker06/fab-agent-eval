param(
    [Parameter(Mandatory)][ValidateRange(0.001, 100000)][double]$Hours,
    [ValidateSet("official", "fab_agent")][string]$Profile = "official",
    [string]$Config = (Join-Path $PSScriptRoot "supplementary/config.json"),
    [string]$PythonExe = "",
    [string[]]$Model = @(),
    [ValidateSet("ruler", "bfcl")][string[]]$Datasets = @("ruler", "bfcl"),
    [int[]]$Lengths = @(),
    [string[]]$RulerTasks = @(),
    [string[]]$BfclCategories = @(),
    [ValidateRange(0, 1000000)][int]$Samples = 0,
    [switch]$Smoke,
    [switch]$DryRun,
    [switch]$CheckData,
    [switch]$ExistingServer
)
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$settings = Get-Content -LiteralPath $Config -Raw | ConvertFrom-Json
if (-not $PythonExe) {
    $PythonExe = $settings.python.Replace("{eval}", $PSScriptRoot).Replace("{assets}", $settings.assets_root)
}
if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "补充 Python 不存在：$PythonExe。请用本地 wheels 准备独立环境，或指定 -PythonExe。"
}
$options = @("--config", $Config, "--hours", "$Hours", "--profile", $Profile)
foreach ($pair in @(
    @("--models", $Model),
    @("--datasets", $Datasets),
    @("--lengths", $Lengths),
    @("--ruler-tasks", $RulerTasks),
    @("--bfcl-categories", $BfclCategories)
)) {
    if ($pair[1].Count) { $options += $pair[0]; $options += @($pair[1] | ForEach-Object { "$_" }) }
}
if ($Samples) { $options += @("--samples", "$Samples") }
foreach ($pair in @(
    @("--smoke", $Smoke), @("--dry-run", $DryRun),
    @("--check-data", $CheckData), @("--existing-server", $ExistingServer)
)) {
    if ($pair[1]) { $options += $pair[0] }
}
$options += @("--python", $PythonExe)
& $PythonExe (Join-Path $PSScriptRoot "supplementary/run_suite.py") @options
exit $LASTEXITCODE
