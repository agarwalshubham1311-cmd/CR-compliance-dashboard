param(
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$Root = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
Set-Location $Root

function Write-Step([string]$Message) {
    Write-Host "`n== $Message ==" -ForegroundColor Cyan
}

function Invoke-Step([string]$Description, [scriptblock]$Action) {
    Write-Host $Description -ForegroundColor Green
    if (-not $DryRun) {
        & $Action
    }
}

Write-Step "Local build + launch"
Write-Host "Project root: $Root"

$frontendDir = Join-Path $Root 'frontend'
$distDir = Join-Path $frontendDir 'dist'
$staticReactDir = Join-Path $Root 'static\react'
$requirementsFile = Join-Path $Root 'requirements.txt'
$jiraEnvFile = Join-Path $Root 'jira.env'
$awsEnvFile = Join-Path $Root 'aws.env'

if (-not (Test-Path $frontendDir)) { throw "Missing frontend directory: $frontendDir" }
if (-not (Test-Path $requirementsFile)) { throw "Missing requirements file: $requirementsFile" }
if (-not (Test-Path $jiraEnvFile)) { throw "Missing jira.env file: $jiraEnvFile" }
if (-not (Test-Path $awsEnvFile)) { throw "Missing aws.env file: $awsEnvFile" }

if (-not (Get-Command node -ErrorAction SilentlyContinue)) { throw "Node.js is not installed or not on PATH." }
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { throw "npm is not installed or not on PATH." }
if (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw "Python is not installed or not on PATH." }

Invoke-Step "1/5 Installing frontend dependencies and building React app..." {
    Push-Location $frontendDir
    try {
        npm install
        npm run build
    }
    finally {
        Pop-Location
    }
}

if (-not $DryRun) {
    if (-not (Test-Path $distDir)) { throw "Frontend build output not found: $distDir" }

    Write-Step "2/5 Staging frontend build for Flask"
    New-Item -ItemType Directory -Force -Path $staticReactDir | Out-Null
    Remove-Item -Path (Join-Path $staticReactDir '*') -Recurse -Force -ErrorAction SilentlyContinue
    Copy-Item -Path (Join-Path $distDir '*') -Destination $staticReactDir -Recurse -Force

    Write-Step "3/5 Installing backend dependencies"
    python -m pip install -r $requirementsFile

    Write-Step "4/5 Starting jira-mcp (Docker Compose) if available"
    if (Get-Command docker -ErrorAction SilentlyContinue) {
        & docker compose version *> $null
        if ($LASTEXITCODE -eq 0) {
            & docker compose up -d jira-mcp
            if ($LASTEXITCODE -ne 0) {
                Write-Host "docker compose could not start jira-mcp. The Flask app will still start, but login API calls may fail until jira-mcp is running." -ForegroundColor Yellow
            }
        }
        else {
            Write-Host "Docker Compose not available. Start jira-mcp separately if needed (see run.ps1)." -ForegroundColor Yellow
        }
    }
    else {
        Write-Host "Docker is not installed. Start jira-mcp separately if needed (see run.ps1)." -ForegroundColor Yellow
    }

    if (-not $env:FLASK_SECRET_KEY) { $env:FLASK_SECRET_KEY = 'dev-key-change-in-production' }
    if (-not $env:FLASK_ENV) { $env:FLASK_ENV = 'development' }
    if (-not $env:MCP_SERVER_URL) { $env:MCP_SERVER_URL = 'http://localhost:8000/sse' }

    Write-Step "5/5 Starting Flask backend"
    Write-Host "Open: http://localhost:5000/" -ForegroundColor Green
    Write-Host "If you already have a session, use an InPrivate/Incognito window to see the login page again." -ForegroundColor Yellow
    python app.py
}
else {
    Write-Host "Dry run only - no files copied and no servers started."
    Write-Host "Would copy: $distDir -> $staticReactDir"
    Write-Host "Would start: python app.py"
}

