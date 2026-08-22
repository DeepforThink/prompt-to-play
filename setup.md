# Prompt-to-Play Workstation Setup

The supported desktop workflow targets Windows and Godot 4.7 .NET. The launcher accepts a Prompt and optional reference images, calls a Responses-compatible model endpoint, builds the generated project, runs structural and interaction checks, captures evaluation images, and launches the selected game.

## Required Software

- Python 3.11 or newer
- .NET 8 SDK
- Godot 4.7.x .NET/Mono edition
- PowerShell 5.1 or newer
- Git

The standard non-.NET Godot build cannot load the trusted C# harness. Install the .NET/Mono distribution even when a generated game uses GDScript.

## Verify the Toolchain

~~~powershell
python --version
dotnet --version
godot --version
~~~

Expected major versions are Python 3.11+, .NET 8, and Godot 4.7.x with <code>mono</code> in the version or package name.

Prompt-to-Play checks explicit configuration first, then workspace-local <code>.tools/</code> directories, and finally <code>PATH</code>. If automatic discovery does not find the correct executables, set process-local paths before launching:

~~~powershell
$env:PTP_DOTNET_EXE = "C:\path\to\dotnet.exe"
$env:PTP_GODOT_EXE = "C:\path\to\Godot_v4.7-stable_mono_win64.exe"
~~~

Do not commit personal absolute paths.

## Start the Desktop Interface

From the repository root, choose a provider and model explicitly:

~~~powershell
powershell -ExecutionPolicy Bypass -File scripts/start_prompt_to_play_api.ps1 -ApiProvider openai -Model <model-id>
powershell -ExecutionPolicy Bypass -File scripts/start_prompt_to_play_api.ps1 -ApiProvider custom -BaseUrl https://example.com/v1 -Model <model-id>
~~~

The launcher asks for the API key using masked input and passes it only to the child process.

## Security

- Never place an API Key in source code, README examples, command-line arguments, committed environment files, screenshots, or issue text.
- Prefer the masked launcher. It clears its local key copy after starting the desktop process.
- Generated projects and evaluation evidence are written below <code>../output/generated/</code> and are ignored by Git.
- Rotate any key that may have been disclosed.

## Development Checks

~~~powershell
python -m pytest -q
python -m compileall -q prompt_to_play scripts tests
ruff check prompt_to_play scripts tests
~~~

The runtime does not require the Codex CLI.
