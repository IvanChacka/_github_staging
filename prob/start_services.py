"""Start server and scheduler as subprocesses. Called as: python start_services.py"""
import subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"

apps = [
    (VENV_PY, ROOT / "app" / "server.py", ROOT / "logs" / "server_stdout.log"),
    (VENV_PY, ROOT / "crawler" / "scheduler.py", ROOT / "logs" / "scheduler_stdout.log"),
]

procs = []
for py, script, log in apps:
    with open(log, "a", encoding="utf-8") as f:
        proc = subprocess.Popen(
            [str(py), str(script)],
            cwd=str(ROOT),
            stdout=f,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    procs.append(proc)
    print(f"Started {script.name} (pid={proc.pid})")
    time.sleep(2)

print(f"\n{len(procs)} services running.")
print("Keep this window open to keep them alive, or detach.")
sys.stdout.flush()

try:
    while True:
        time.sleep(10)
        for p in procs:
            if p.poll() is not None:
                print(f"WARNING: {p.args[1].name} died (rc={p.returncode})")
        if all(p.poll() is not None for p in procs):
            print("All services died.")
            break
except KeyboardInterrupt:
    for p in procs:
        p.terminate()
    print("Terminated.")
