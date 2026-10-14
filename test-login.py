#!/usr/bin/env python
"""
Test script for Jira Dashboard Login Page
Checks frontend build, Flask app, and API endpoints
"""
import os
import sys

print("=" * 50)
print("Jira Dashboard Login Page Test")
print("=" * 50)
print()

# Test 1: Frontend files
print("Test 1: Checking frontend build...")
if os.path.exists("static/react/index.html"):
    print("✓ Frontend built and staged")
else:
    print("✗ Frontend NOT found - building now...")
    os.system("cd frontend && npm run build")
    if not os.path.exists("static/react"):
        os.makedirs("static/react", exist_ok=True)
    os.system("xcopy /E /I /Y frontend\\dist\\* static\\react\\")
    if os.path.exists("static/react/index.html"):
        print("✓ Frontend built and staged successfully")
    else:
        print("✗ Failed to stage frontend")
        sys.exit(1)

print()

# Test 2: Flask app imports
print("Test 2: Checking Flask app...")
try:
    from app import app
    print("✓ Flask app imports successfully")
except RecursionError as e:
    print(f"✗ RecursionError in app: {str(e)[:100]}")
    sys.exit(1)
except Exception as e:
    print(f"✗ Error importing app: {str(e)[:100]}")
    sys.exit(1)

print()

# Test 3: Flask endpoints
print("Test 3: Testing Flask endpoints...")
try:
    with app.test_client() as client:
        r1 = client.get("/")
        if r1.status_code == 200:
            print("✓ GET / returns 200 OK")
        else:
            print(f"✗ GET / returned {r1.status_code}")

        r2 = client.get("/api/jira/check-session")
        if r2.status_code == 200:
            print("✓ GET /api/jira/check-session returns 200 OK")
        else:
            print(f"✗ GET /api/jira/check-session returned {r2.status_code}")
except RecursionError as e:
    print(f"✗ RecursionError testing endpoints: {str(e)[:100]}")
    sys.exit(1)
except Exception as e:
    print(f"✗ Error testing endpoints: {str(e)[:100]}")
    sys.exit(1)

print()
print("=" * 50)
print("All tests passed!")
print()
print("To start the login page:")
print("  python app.py")
print()
print("Then open: http://localhost:5000/")
print("=" * 50)

