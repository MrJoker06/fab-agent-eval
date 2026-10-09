param(
    [Parameter(Mandatory)][ValidateRange(0.001, 100000)][double]$SupplementaryHours,
    [ValidateRange(0.001, 100000)][double]$RequiredHours = 24,
    [ValidateSet("official", "fab_agent")][string]$Profile = "official",
    [string[]]$Model = @(),
    [string[]]$RequiredDatasets = @(),
    [ValidateSet("ruler", "bfcl")][string[]]$SupplementaryDatasets = @("ruler", "bfcl"),
    [string]$SupplementaryConfig = (Join-Path $PSScriptRoot "supplementary/config.json"),
    [string]$SupplementaryPython = "",
    [switch]$DryRun,
    [switch]$CheckData
)
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false
$stamp = [DateTimeOffset]::UtcNow.ToOffset([TimeSpan]::FromHours(8)).ToString("yyyyMMdd_HHmmss_fffffff")
$folder = Join-Path $PSScriptRoot "supplementary/runs/combined_${stamp}_$Profile"
New-Item -ItemType Directory -Path $folder -Force | Out-Null
$launcher = Join-Path $folder "invoke_layer.ps1"
@'
param([string]$Entry, [string]$Settings)
$ErrorActionPreference = "Stop"
$options = Get-Content -LiteralPath $Settings -Raw | ConvertFrom-Json -AsHashtable
& $Entry @options
exit $LASTEXITCODE
'@ | Set-Content -LiteralPath $launcher -Encoding utf8
$shell = (Get-Process -Id $PID).Path
$required = @{ Profile = $Profile; Hours = $RequiredHours }
$supplementary = @{ Profile = $Profile; Hours = $SupplementaryHours; Config = $SupplementaryConfig; Datasets = $SupplementaryDatasets }
if ($Model.Count) { $required.Model = $Model; $supplementary.Model = $Model }
if ($RequiredDatasets.Count) { $required.Datasets = $RequiredDatasets }
if ($SupplementaryPython) { $supplementary.PythonExe = $SupplementaryPython }
if ($DryRun) { $required.DryRun = $true; $supplementary.DryRun = $true }
if ($CheckData) { $required.CheckData = $true; $supplementary.CheckData = $true }
$status = @{
    profile = $Profile; required_hours = $RequiredHours; supplementary_hours = $SupplementaryHours
    planned_total_hours = $RequiredHours + $SupplementaryHours; layers = @(); passed = $false
}
foreach ($layer in @(
    @{ name = "required"; entry = (Join-Path $PSScriptRoot "run_all.ps1"); options = $required },
    @{ name = "supplementary"; entry = (Join-Path $PSScriptRoot "run_supplementary.ps1"); options = $supplementary }
)) {
    $settingsPath = Join-Path $folder ($layer.name + "_arguments.json")
    $logPath = Join-Path $folder ($layer.name + ".log")
    $layer.options | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $settingsPath -Encoding utf8
    New-Item -ItemType File -Path $logPath -Force | Out-Null
    Write-Host "RUN LAYER $($layer.name)"
    & $shell -NoProfile -File $launcher -Entry $layer.entry -Settings $settingsPath 2>&1 | Tee-Object -FilePath $logPath
    $code = $LASTEXITCODE
    $markers = @(Get-Content -LiteralPath $logPath | Where-Object { $_ -match "^(PLAN|RESULT) " })
    $status.layers += @{
        layer = $layer.name; exit_code = $code; log = $logPath
        status = $(if ($code -ne 0) { "failed" } elseif ($DryRun) { "planned" } elseif ($CheckData) { "checked" } else { "completed" })
        artifacts = $markers
    }
    $status | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath (Join-Path $folder "combined_status.json") -Encoding utf8
}
$status.passed = @($status.layers | Where-Object { $_.exit_code -ne 0 }).Count -eq 0
$status | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath (Join-Path $folder "combined_status.json") -Encoding utf8
Write-Host "RESULT $(Join-Path $folder 'combined_status.json')"
if (-not $status.passed) { exit 1 }
exit 0
