param(
    [ValidateSet('planner','manager')]
    [string]$Role = 'planner',
    [string]$User = 'local-user',
    [int]$Ttl = 3600,
    [switch]$Bedrock,
    [string]$BedrockKeyFile = '',
    [string]$BedrockRegion = 'ap-southeast-1',
    [ValidateSet('global.anthropic.claude-sonnet-4-5-20250929-v1:0')]
    [string]$BedrockModel = 'global.anthropic.claude-sonnet-4-5-20250929-v1:0',
    # This script is the LOCAL DEMO launcher: it pins the server to the fixed
    # scenario clock explicitly (G1.0.1 fail-safe policy makes 'wall' the
    # default everywhere else). Pass -ClockMode wall to test real expiry.
    [ValidateSet('scenario','wall')]
    [string]$ClockMode = 'scenario'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

if ($Bedrock) {
    if ([string]::IsNullOrWhiteSpace($BedrockKeyFile)) {
        if ($env:PLANPILOT_BEDROCK_KEY_FILE) {
            $BedrockKeyFile = $env:PLANPILOT_BEDROCK_KEY_FILE
        } elseif (Test-Path -LiteralPath (Join-Path $projectRoot 'secrets\bedrock-api-key.txt')) {
            $BedrockKeyFile = Join-Path $projectRoot 'secrets\bedrock-api-key.txt'
        } else {
            $BedrockKeyFile = Read-Host 'Enter the full path to your Bedrock API key file'
        }
    }
    if (-not (Test-Path -LiteralPath $BedrockKeyFile -PathType Leaf)) {
        throw 'Bedrock API key file not found.'
    }
    $env:PLANPILOT_BEDROCK_KEY_FILE = (Resolve-Path -LiteralPath $BedrockKeyFile).Path
    $env:PLANPILOT_BEDROCK_REGION = $BedrockRegion
    $env:PLANPILOT_BEDROCK_MODEL = $BedrockModel
    $env:PLANPILOT_FORBID_LLM_NETWORK = '0'
    Write-Host 'Bedrock configured. Sending an Agent message makes paid AWS requests.' -ForegroundColor Yellow
    Write-Host "Region: $BedrockRegion; Model: $BedrockModel" -ForegroundColor DarkGray
} else {
    $env:PLANPILOT_FORBID_LLM_NETWORK = '1'
}

$secret = Read-Host 'Enter numeric secret (at least 32 digits)'
if ($secret -notmatch '^[0-9]{32,}$') {
    throw 'The secret must contain at least 32 digits and no other characters.'
}

$env:PYTHONPATH = Join-Path $projectRoot 'src'
$env:PLANPILOT_AUTH_SECRET = $secret
$env:PLANPILOT_CLOCK_MODE = $ClockMode
$python = Join-Path $projectRoot '.venv-runtime\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw "Project Python environment not found: $python"
}

$token = & $python (Join-Path $projectRoot 'tools\issue_local_token.py') --user $User --role $Role --ttl $Ttl
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($token)) {
    throw 'Token generation failed.'
}

Set-Clipboard -Value $token.Trim()
Write-Host ''
Write-Host 'Token generated and copied to clipboard.' -ForegroundColor Green
Write-Host 'Open the web page and paste the token into the Bearer token field.' -ForegroundColor Cyan
Write-Host "Role: $Role; User: $User; Token lifetime: $Ttl seconds" -ForegroundColor DarkGray
$apiHost = if ($env:PLANPILOT_HOST) { $env:PLANPILOT_HOST } else { '127.0.0.1' }
$apiPort = if ($env:PLANPILOT_PORT) { $env:PLANPILOT_PORT } else { '8080' }
Write-Host "API: http://${apiHost}:${apiPort}/" -ForegroundColor Cyan
Write-Host ''

& $python (Join-Path $projectRoot 'tools\api_server.py')
