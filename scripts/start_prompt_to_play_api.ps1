param(
    [ValidateSet("micu", "openai", "custom")]
    [string]$ApiProvider = "micu",
    [string]$BaseUrl = "",
    [string]$Model = "gpt-5.6-sol",
    [string]$PythonExecutable = ""
)

$ErrorActionPreference = "Stop"

function ConvertFrom-MaskedInput {
    param([Parameter(Mandatory = $true)][string]$Label)

    $secure = Read-Host $Label -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
        if ([string]::IsNullOrWhiteSpace($plain)) {
            throw "$Label cannot be empty."
        }
        return $plain
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
if ([string]::IsNullOrWhiteSpace($Model)) {
    throw "Model cannot be empty."
}
if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
    $pythonCommand = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($null -eq $pythonCommand) {
        $pythonCommand = Get-Command python.exe -ErrorAction Stop
    }
    $PythonExecutable = $pythonCommand.Source
}
elseif (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
    throw "Python executable not found: $PythonExecutable"
}

$apiBaseUrl = $BaseUrl.TrimEnd("/")
if ([string]::IsNullOrWhiteSpace($apiBaseUrl)) {
    $apiBaseUrl = switch ($ApiProvider) {
        "micu" { "https://www.micuapi.ai/v1" }
        "openai" { "https://api.openai.com/v1" }
        default { throw "-BaseUrl is required when -ApiProvider custom is selected." }
    }
}
$apiUserAgent = if ($ApiProvider -eq "micu") {
    "codex_cli_rs/0.77.0 (Windows 10.0.26100; x86_64) WindowsTerminal"
}
else {
    "prompt-to-play/1.0"
}
$apiKeyLabel = if ($ApiProvider -eq "micu") { "Micu API Key" } else { "OpenAI API Key" }
if ($ApiProvider -eq "custom") {
    $apiKeyLabel = "API Key"
}
$apiKey = $null

$managedEnvironmentNames = @(
    "PROMPT_TO_PLAY_PROVIDER",
    "PROMPT_TO_PLAY_API_STYLE",
    "PROMPT_TO_PLAY_STRUCTURED_OUTPUT_MODE",
    "PROMPT_TO_PLAY_STREAM_RESPONSES",
    "PROMPT_TO_PLAY_BASE_URL",
    "PROMPT_TO_PLAY_USER_AGENT",
    "PROMPT_TO_PLAY_MODEL",
    "PROMPT_TO_PLAY_TIMEOUT_SECONDS",
    "PROMPT_TO_PLAY_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL"
)
$previousEnvironment = @{}
foreach ($environmentName in $managedEnvironmentNames) {
    $previousValue = [Environment]::GetEnvironmentVariable($environmentName, "Process")
    if ($null -ne $previousValue) {
        $previousEnvironment[$environmentName] = $previousValue
    }
}

try {
    Write-Host "Model endpoint: $apiBaseUrl/responses ($ApiProvider)"
    $apiKey = ConvertFrom-MaskedInput $apiKeyLabel

    $env:PROMPT_TO_PLAY_PROVIDER = "http"
    $env:PROMPT_TO_PLAY_API_STYLE = "responses"
    # Micu documents the Codex-style Responses subset. The model still receives
    # the full JSON Schema in instructions and Prompt-to-Play validates the
    # returned document locally before it can write any project file.
    $env:PROMPT_TO_PLAY_STRUCTURED_OUTPUT_MODE = if ($ApiProvider -eq "micu") {
        "prompt"
    }
    else {
        "native"
    }
    $env:PROMPT_TO_PLAY_STREAM_RESPONSES = if ($ApiProvider -eq "micu") {
        "true"
    }
    else {
        "false"
    }
    $env:PROMPT_TO_PLAY_BASE_URL = $apiBaseUrl
    $env:PROMPT_TO_PLAY_USER_AGENT = $apiUserAgent
    $env:PROMPT_TO_PLAY_MODEL = $Model
    # Large Godot file packages and repair patches can take several minutes on
    # reasoning models. Keep the GUI worker alive rather than turning a healthy
    # long generation into a misleading transport failure after 120 seconds.
    $env:PROMPT_TO_PLAY_TIMEOUT_SECONDS = "600"
    $env:PROMPT_TO_PLAY_API_KEY = $apiKey
    Remove-Item Env:OPENAI_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:OPENAI_BASE_URL -ErrorAction SilentlyContinue
    Remove-Item Env:OPENAI_MODEL -ErrorAction SilentlyContinue

    $process = Start-Process `
        -FilePath $PythonExecutable `
        -ArgumentList @("-m", "prompt_to_play.direct_pipeline") `
        -WorkingDirectory $repoRoot `
        -PassThru
    Write-Host "Prompt-to-Play started (PID $($process.Id))."
}
finally {
    $apiKey = $null
    foreach ($environmentName in $managedEnvironmentNames) {
        [Environment]::SetEnvironmentVariable($environmentName, $null, "Process")
    }
    foreach ($entry in $previousEnvironment.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, "Process")
    }
    $previousEnvironment = $null
}
