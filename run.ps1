# run.ps1 - single command to build and start the whole compliance dashboard stack.
# Usage:  .\run.ps1
#
# Tries "docker compose" first (fastest path on most machines). If that's not
# available (some locked-down corporate installs block the compose plugin),
# automatically falls back to plain "docker build"/"docker run" commands.

function Invoke-Checked {
    param([string]$Description, [scriptblock]$Command)
    Write-Host $Description
    & $Command
    if ($LASTEXITCODE -ne 0) {
        Write-Host "FAILED: $Description (exit code $LASTEXITCODE)" -ForegroundColor Red
        Write-Host "Stopping here so this doesn't fail silently further down." -ForegroundColor Red
        exit 1
    }
}

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

if (-not (Test-Path "aws.env")) {
    Write-Host "No aws.env file found. Create it with AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION first." -ForegroundColor Red
    exit 1
}

if (Test-ComposeAvailable) {
    Write-Host "Using docker compose..." -ForegroundColor Green
    docker compose up -d --build
    if ($LASTEXITCODE -ne 0) {
        Write-Host "docker compose up failed (exit code $LASTEXITCODE) - see output above." -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "docker compose plugin not available - using manual build/run fallback..." -ForegroundColor Yellow

    docker network inspect compliance-net *> $null
    if ($LASTEXITCODE -ne 0) {
        Invoke-Checked "Creating compliance-net network..." { docker network create compliance-net }
    }

    Invoke-Checked "Building jira-mcp..." { docker build -t jira-mcp-custom -f Dockerfile.jira-mcp . }
    docker rm -f jira-mcp *> $null
    Invoke-Checked "Starting jira-mcp..." {
        docker run -d --name jira-mcp --network compliance-net -p 8000:8000 --env-file jira.env `
            jira-mcp-custom --transport sse --port 8000
    }

    Invoke-Checked "Building compliance-dashboard..." { docker build -t compliance-dashboard -f Dockerfile . }
    docker rm -f compliance-dashboard *> $null
    Invoke-Checked "Starting compliance-dashboard..." {
        docker run -d --name compliance-dashboard --network compliance-net -p 5000:5000 `
            --env-file jira.env --env-file aws.env `
            -e MCP_SERVER_URL=http://jira-mcp:8000/sse `
            -v compliance-data:/app/data `
            compliance-dashboard
    }
}

Write-Host ""
Write-Host "== Done ==" -ForegroundColor Cyan
Write-Host "Dashboard: http://localhost:5000/dashboard" -ForegroundColor Green
Write-Host "Check status any time with: docker ps"
