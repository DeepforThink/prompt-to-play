param(
    [ValidateSet("micu", "openai")]
    [string]$ApiProvider = "micu",
    [ValidateSet("grok", "gemini")]
    [string]$ImageProvider = "gemini",
    [ValidateSet("off", "auto", "required")]
    [string]$AssetMode = "auto",
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

$apiBaseUrl = if ($ApiProvider -eq "micu") {
    "https://www.micuapi.ai/v1"
}
else {
    "https://api.openai.com/v1"
}
$apiUserAgent = if ($ApiProvider -eq "micu") {
    "codex_cli_rs/0.77.0 (Windows 10.0.26100; x86_64) WindowsTerminal"
}
else {
    "prompt-to-play/1.0"
}
$apiKeyLabel = if ($ApiProvider -eq "micu") { "Micu API Key" } else { "OpenAI API Key" }
$apiKey = $null
$tripoKey = $null
$imageKey = $null

$managedEnvironmentNames = @(
    "PROMPT_TO_PLAY_PROVIDER",
    "PROMPT_TO_PLAY_API_STYLE",
    "PROMPT_TO_PLAY_BASE_URL",
    "PROMPT_TO_PLAY_USER_AGENT",
    "PROMPT_TO_PLAY_MODEL",
    "PROMPT_TO_PLAY_API_KEY",
    "PROMPT_TO_PLAY_ASSET_MODE",
    "PROMPT_TO_PLAY_ASSET_IMAGE_MODEL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "TRIPO3D_API_KEY",
    "XAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY"
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
    if ($AssetMode -ne "off") {
        $tripoKey = ConvertFrom-MaskedInput "Tripo3D API Key"
        $imageLabel = if ($ImageProvider -eq "grok") { "xAI API Key" } else { "Gemini API Key" }
        $imageKey = ConvertFrom-MaskedInput $imageLabel
    }

    $env:PROMPT_TO_PLAY_PROVIDER = "http"
    $env:PROMPT_TO_PLAY_API_STYLE = "responses"
    $env:PROMPT_TO_PLAY_BASE_URL = $apiBaseUrl
    $env:PROMPT_TO_PLAY_USER_AGENT = $apiUserAgent
    $env:PROMPT_TO_PLAY_MODEL = $Model
    $env:PROMPT_TO_PLAY_API_KEY = $apiKey
    $env:PROMPT_TO_PLAY_ASSET_MODE = $AssetMode
    $env:PROMPT_TO_PLAY_ASSET_IMAGE_MODEL = $ImageProvider
    Remove-Item Env:OPENAI_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:OPENAI_BASE_URL -ErrorAction SilentlyContinue
    Remove-Item Env:OPENAI_MODEL -ErrorAction SilentlyContinue
    Remove-Item Env:GOOGLE_API_KEY -ErrorAction SilentlyContinue
    if ($AssetMode -eq "off") {
        Remove-Item Env:TRIPO3D_API_KEY -ErrorAction SilentlyContinue
        Remove-Item Env:XAI_API_KEY -ErrorAction SilentlyContinue
        Remove-Item Env:GEMINI_API_KEY -ErrorAction SilentlyContinue
    }
    else {
        $env:TRIPO3D_API_KEY = $tripoKey
        if ($ImageProvider -eq "grok") {
            $env:XAI_API_KEY = $imageKey
            Remove-Item Env:GEMINI_API_KEY -ErrorAction SilentlyContinue
        }
        else {
            $env:GEMINI_API_KEY = $imageKey
            Remove-Item Env:XAI_API_KEY -ErrorAction SilentlyContinue
        }
    }

    $process = Start-Process `
        -FilePath $PythonExecutable `
        -ArgumentList @("-m", "prompt_to_play.pipeline") `
        -WorkingDirectory $repoRoot `
        -PassThru
    Write-Host "Prompt-to-Play started (PID $($process.Id))."
}
finally {
    $apiKey = $null
    $tripoKey = $null
    $imageKey = $null
    foreach ($environmentName in $managedEnvironmentNames) {
        [Environment]::SetEnvironmentVariable($environmentName, $null, "Process")
    }
    foreach ($entry in $previousEnvironment.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, "Process")
    }
    $previousEnvironment = $null
}
