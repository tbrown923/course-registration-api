import sys
sys.path.insert(0, ".")

try:
    from grader.grade_service import run
    from grader._svc import load_base_url, wake, format_report
except ModuleNotFoundError as e:
    print(f"Missing dependency '{e.name}'. Make sure you are using the active venv environment.")
    sys.exit(1)

# Point directly to your live local FastAPI server
base = "http://localhost:8000"

if not wake(base, "/", timeout=20):
    print(f"Service unreachable at {base} — make sure your uvicorn server is running in the other terminal tab!")
    sys.exit(1)

print(format_report(run(base, "./data/selftest")))