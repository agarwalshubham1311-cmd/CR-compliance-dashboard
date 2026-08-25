# run.ps1 - single command to build and start the whole compliance dashboard stack.
# Usage:  .\run.ps1
#
# Tries "docker compose" first (fastest path on most machines). If that's not
# available (some locked-down corporate installs block the compose plugin),
# automatically falls back to plain "docker build"/"docker run" commands.

$ErrorActionPreference = "Stop"

function Test-DockerRunning {
    try { docker ps | Out-Null; return $true } catch { return $false }
}

function Test-ComposeAvailable {
    try { docker compose version | Out-Null; return $true } catch { return $false }
}

Write-Host "== Compliance Dashboard: setup ==" -ForegroundColor Cyan

if (-not (Test-DockerRunning)) {
    Write-Host "Docker doesn't seem to be running. Start Docker Desktop and try again." -ForegroundColor Red
    exit 1
}

if (-not (Test-Path "jira.env")) {
    Write-Host "No jira.env file found. Create it with JIRA_URL, JIRA_USERNAME, JIRA_API_TOKEN first." -ForegroundColor Red
    exit 1
}

if (Test-ComposeAvailable) {
    Write-Host "Using docker compose..." -ForegroundColor Green
    docker compose up -d --build
} else {
    Write-Host "docker compose plugin not available - using manual build/run fallback..." -ForegroundColor Yellow

    docker network inspect compliance-net *> $null
    if ($LASTEXITCODE -ne 0) { docker network create compliance-net }

    Write-Host "Building jira-mcp..."
    docker build -t jira-mcp-custom -f Dockerfile.jira-mcp .
    docker rm -f jira-mcp *> $null
    docker run -d --name jira-mcp --network compliance-net -p 8000:8000 --env-file jira.env `
        jira-mcp-custom --transport sse --port 8000

    Write-Host "Building ollama..."
    docker build -t ollama-custom -f Dockerfile.ollama .
    docker rm -f ollama *> $null
    docker run -d --name ollama --network compliance-net -p 11434:11434 -v ollama-data:/root/.ollama ollama-custom

    Write-Host "Building compliance-dashboard..."
    docker build -t compliance-dashboard -f Dockerfile .
    docker rm -f compliance-dashboard *> $null
    docker run -d --name compliance-dashboard --network compliance-net -p 5000:5000 `
        --env-file jira.env `
        -e MCP_SERVER_URL=http://jira-mcp:8000/sse `
        -e OLLAMA_URL=http://ollama:11434/v1/chat/completions `
        -v compliance-data:/app/data `
        compliance-dashboard
}

Write-Host "Waiting for containers to settle..." -ForegroundColor Cyan
Start-Sleep -Seconds 5

$modelCheck = docker exec ollama ollama list 2>$null
if ($modelCheck -notmatch "llama3.1:8b") {
    Write-Host "Pulling llama3.1:8b (first run only, ~5GB, may take a while)..." -ForegroundColor Yellow
    docker exec ollama ollama pull llama3.1:8b
} else {
    Write-Host "Model already present, skipping pull." -ForegroundColor Green
}

Write-Host ""
Write-Host "== Done ==" -ForegroundColor Cyan
Write-Host "Dashboard: http://localhost:5000/dashboard" -ForegroundColor Green
Write-Host "Check status any time with: docker ps"
