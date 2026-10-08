param(
    [ValidateSet("official", "fab_agent")][string]$Profile = "official",
    [double]$Hours = 24,
    [string[]]$Model = @(),
    [string[]]$Datasets = @(),
    [int]$Samples = 0,
    [switch]$Smoke,
    [switch]$DryRun,
    [switch]$CheckData,
    [switch]$ExistingServer
)
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

# ==================== 统一配置区：所有路径和参数都在这里修改 ====================
$ProjectRoot = Split-Path $PSScriptRoot -Parent
$PythonExe = Join-Path $ProjectRoot "laptop_lab/.venv-eval-win/Scripts/python.exe"
if (-not (Test-Path -LiteralPath $PythonExe)) { $PythonExe = "python" }
$LlamaServerExe = Join-Path $ProjectRoot "laptop_lab/runtimes/llama.cpp-cuda-win/llama-server.exe"
$ModelRoot = "D:/fab_insight/models"
$DataRoot = Join-Path $ProjectRoot "laptop_lab/datasets/evalscope_full"
$Seed = 42
$Port = 8080
$ServerReadyTimeout = 300

# llama.cpp 启动参数；model 中的 server_args 可以覆盖这些值。
$ServerArgs = [ordered]@{
    "--host" = "127.0.0.1"; "--port" = $Port
    "--gpu-layers" = "all"; "--ctx-size" = 16384
    "--batch-size" = 1024; "--ubatch-size" = 256; "--parallel" = 1
    "--cache-type-k" = "q8_0"; "--cache-type-v" = "q8_0"
    "--kv-offload" = $true; "--flash-attn" = "on"
    "--jinja" = $true; "--reasoning" = "auto"; "--reasoning-format" = "deepseek"
    "--fit" = "off"; "--no-context-shift" = $true; "--metrics" = $true
}
$Models = @(
    @{
        name = "qwen3_14b_q5_k_m"; family = "qwen3"
        gguf = Join-Path $ModelRoot "qwen3_14b_q5_k_m/Qwen3-14B-Q5_K_M.gguf"
        server_args = @{}; generation = @{}; dataset_overrides = @{}
    },
    @{
        name = "qwen38_27b_q4_k_m"; family = "qwen38"
        gguf = Join-Path $ModelRoot "qwen38_27b_q4_k_m/Qwen3.8-27B-Q4_K_M.gguf"
        server_args = @{}; generation = @{}; dataset_overrides = @{}
    },
    @{
        name = "qwen38_27b_ridge_37bpw"; family = "qwen38"
        gguf = Join-Path $ModelRoot "qwen38_27b_ridge_37bpw/Qwen3.8-27B-Ridge-3.7bpw.gguf"
        server_args = @{}; generation = @{}; dataset_overrides = @{}
    }
)

# 每个评测集：Samples 可自行填写；null 表示按整轮预算计算。
# generation 可直接填写 temperature、top_p、top_k、presence_penalty 等。
# 例如 math_500 的 generation = @{ top_k = 30 }；两个 27B 仍共用相同设置。
# seconds_per_sample 为规划假设，请替换成该 profile 下最慢模型的实测秒/题。
$EvalTasks = @(
    @{
        name = "math_500"; script = "eval_math500.py"; samples = $null
        thinking = $true; max_tokens = 8192; few_shot_num = 0; batch_size = 1
        seconds_per_sample = @{ official = 180; fab_agent = 180 }
        generation = @{}; dataset_args = @{}
        profile_overrides = @{}
    },
    @{
        name = "mmlu_pro"; script = "eval_mmlu_pro.py"; samples = $null
        thinking = $true; max_tokens = 4096; few_shot_num = 5; batch_size = 1
        seconds_per_sample = @{ official = 90; fab_agent = 90 }
        generation = @{}; dataset_args = @{}
        profile_overrides = @{}
    },
    @{
        name = "ifeval"; script = "eval_ifeval.py"; samples = $null
        thinking = $false; max_tokens = 2048; few_shot_num = 0; batch_size = 1
        seconds_per_sample = @{ official = 15; fab_agent = 15 }
        generation = @{}; dataset_args = @{}
        profile_overrides = @{}
    },
    @{
        name = "cmmlu"; script = "eval_cmmlu.py"; samples = $null
        thinking = $false; max_tokens = 2048; few_shot_num = 0; batch_size = 1
        seconds_per_sample = @{ official = 45; fab_agent = 45 }
        generation = @{}; dataset_args = @{}
        profile_overrides = @{}
    },
    @{
        name = "ceval"; script = "eval_ceval.py"; samples = $null
        thinking = $false; max_tokens = 2048; few_shot_num = 0; batch_size = 1
        seconds_per_sample = @{ official = 45; fab_agent = 45 }
        generation = @{}; dataset_args = @{}
        profile_overrides = @{}
    }
)

$Profiles = @{
    official = @{
        common = @{ top_k = 20; min_p = 0.0; repetition_penalty = 1.0; n = 1; stream = $true; timeout = 1800; retries = 0 }
        families = @{
            qwen3 = @{
                thinking = @{ temperature = 0.6; top_p = 0.95; presence_penalty = 0.0 }
                direct = @{ temperature = 0.7; top_p = 0.8; presence_penalty = 0.0 }
            }
            qwen38 = @{
                thinking = @{ temperature = 1.0; top_p = 0.95; presence_penalty = 0.0; reasoning_effort = "xhigh" }
                direct = @{ temperature = 0.7; top_p = 0.8; presence_penalty = 1.5 }
            }
        }
    }
    fab_agent = @{
        common = @{ top_k = 20; min_p = 0.0; repetition_penalty = 1.0; presence_penalty = 0.0; n = 1; stream = $true; timeout = 1800; retries = 0 }
        families = @{
            qwen3 = @{
                thinking = @{ temperature = 0.6; top_p = 0.95 }
                direct = @{ temperature = 0.1; top_p = 0.95 }
            }
            qwen38 = @{
                thinking = @{ temperature = 0.6; top_p = 0.95; reasoning_effort = "medium" }
                direct = @{ temperature = 0.1; top_p = 0.95 }
            }
        }
    }
}
# ==================== 配置区结束；下方负责传递配置 ====================

# 筛选单个模型/数据集时仍按原整轮规模分配预算，保持正式题数一致。
$BudgetModels = $Models.Count
$BudgetProjects = $EvalTasks.Count
if ($Model.Count) {
    $Models = @($Models | Where-Object { $_.name -in $Model })
    if ($Models.Count -ne $Model.Count) { throw "Model 中存在未配置的别名" }
}
if ($Datasets.Count) {
    $EvalTasks = @($EvalTasks | Where-Object { $_.name -in $Datasets })
    if ($EvalTasks.Count -ne $Datasets.Count) { throw "Datasets 中存在未配置的项目" }
}
$config = @{
    profile = $Profile; hours = $Hours; seed = $Seed; reserve = 0.2
    budget_models = $BudgetModels; budget_projects = $BudgetProjects
    models = @($Models); tasks = @($EvalTasks); profiles = $Profiles
    server = @{ executable = $LlamaServerExe; args = $ServerArgs; ready_timeout = $ServerReadyTimeout }
    python = $PythonExe; data_root = $DataRoot; eval_root = $PSScriptRoot
    sample_override = $Samples; smoke = [bool]$Smoke
}
$stamp = [System.TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTimeOffset]::UtcNow, "China Standard Time").ToString("yyyyMMdd_HHmmss_fffffff")
$runDir = Join-Path $PSScriptRoot "runs/${stamp}_$Profile"
New-Item -ItemType Directory -Path $runDir -Force | Out-Null
$config.run_dir = $runDir
$configPath = Join-Path $runDir "suite_config.json"
[System.IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json -Depth 30), [System.Text.UTF8Encoding]::new($false))
$options = @("--config", $configPath)
if ($DryRun) { $options += "--dry-run" }
if ($CheckData) { $options += "--check-data" }
if ($ExistingServer) { $options += "--existing-server" }
& $PythonExe (Join-Path $PSScriptRoot "run_suite.py") @options
exit $LASTEXITCODE
