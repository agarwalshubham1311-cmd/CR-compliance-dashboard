param(
    [switch]$Verbose
)

$ErrorActionPreference = 'Stop'
$Root = (Get-Location).Path

Write-Host "=== Jira Dashboard Login Page Test ===" -ForegroundColor Cyan
Write-Host "Testing login form and API endpoints..."
Write-Host ""

# Test 1: Frontend files exist
Write-Host "Test 1: Checking frontend build..." -ForegroundColor Yellow
if (Test-Path "$Root\static\react\index.html") {
    Write-Host "✓ Frontend built and staged at static/react" -ForegroundColor Green
} else {
    Write-Host "✗ Frontend NOT found at static/react" -ForegroundColor Red
    Write-Host "  Building now..."
    Push-Location "$Root\frontend"
    npm run build
    Pop-Location

    Copy-Item -Path "$Root\frontend\dist\*" -Destination "$Root\static\react" -Recurse -Force
    if (Test-Path "$Root\static\react\index.html") {
        Write-Host "✓ Frontend built and staged successfully" -ForegroundColor Green
    } else {
        Write-Host "✗ Failed to stage frontend" -ForegroundColor Red
        exit 1
    }
}

Write-Host ""

# Test 2: Flask app imports
Write-Host "Test 2: Checking Flask app imports..." -ForegroundColor Yellow
try {
    $output = python -u -c "from app import app; print('SUCCESS')" 2>&1 | Out-String
    if ($output -match "SUCCESS") {
        Write-Host "✓ Flask app imports successfully" -ForegroundColor Green
    } else {
        Write-Host "✗ Flask import failed:" -ForegroundColor Red
        Write-Host $output
        exit 1
    }
} catch {
    Write-Host "✗ Error importing Flask app: $_" -ForegroundColor Red
    exit 1
}

Write-Host ""

# Test 3: Login endpoints
Write-Host "Test 3: Testing login endpoints..." -ForegroundColor Yellow
python -u -c @"
import sys
sys.path.insert(0, '$Root')
from flask import Flask
from app import app

# Test route responses
with app.test_client() as client:
    # Test root endpoint
    r1 = client.get('/')
    if r1.status_code == 200:
        print('✓ GET / returns 200 OK')
    else:
        print(f'✗ GET / returned {r1.status_code}')

    # Test API endpoint
    r2 = client.get('/api/jira/check-session')
    if r2.status_code == 200:
        print('✓ GET /api/jira/check-session returns 200 OK')
    else:
        print(f'✗ GET /api/jira/check-session returned {r2.status_code}')
"@ 2>&1

if ($LASTEXITCODE -eq 0) {
    Write-Host "✓ All endpoints responding" -ForegroundColor Green
} else {
    Write-Host "✗ Endpoint test failed" -ForegroundColor Red
}

Write-Host ""
Write-Host "=== Test Complete ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "To start the login page:"  -ForegroundColor Green
Write-Host "  python app.py"
Write-Host ""
Write-Host "Then open: http://localhost:5000/" -ForegroundColor Green

