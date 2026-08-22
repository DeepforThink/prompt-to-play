param(
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

$openAiKey = ConvertFrom-MaskedInput "OpenAI API Key"
$tripoKey = $null
$imageKey = $null
if ($AssetMode -ne "off") {
    $tripoKey = ConvertFrom-MaskedInput "Tripo3D API Key"
    $imageLabel = if ($ImageProvider -eq "grok") { "xAI API Key" } else { "Gemini API Key" }
    $imageKey = ConvertFrom-MaskedInput $imageLabel
}

try {
    $env:PROMPT_TO_PLAY_PROVIDER = "openai"
    $env:PROMPT_TO_PLAY_API_STYLE = "responses"
    $env:PROMPT_TO_PLAY_MODEL = $Model
    $env:PROMPT_TO_PLAY_API_KEY = $openAiKey
    $env:PROMPT_TO_PLAY_ASSET_MODE = $AssetMode
    $env:PROMPT_TO_PLAY_ASSET_IMAGE_MODEL = $ImageProvider
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
    $openAiKey = $null
    $tripoKey = $null
    $imageKey = $null
    Remove-Item Env:PROMPT_TO_PLAY_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:PROMPT_TO_PLAY_MODEL -ErrorAction SilentlyContinue
    Remove-Item Env:TRIPO3D_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:XAI_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:GEMINI_API_KEY -ErrorAction SilentlyContinue
}
