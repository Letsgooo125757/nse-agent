"""DATABASE_URL=embedded: data persists across separate command runs."""
import os
import subprocess
import sys


def run(args, env):
    return subprocess.run([sys.executable, "-m", "nse_agent", *args], env=env,
                          capture_output=True, text=True, timeout=180)


def test_embedded_database_persists(tmp_path):
    env = {**os.environ, "DATABASE_URL": "embedded", "PGDATA_DIR": str(tmp_path / "pgdata")}
    r = run(["init-db"], env)
    assert r.returncode == 0, r.stderr
    assert "65 companies" in r.stdout
    r = run(["status"], env)                       # a new process, same data
    assert r.returncode == 0, r.stderr
    assert "embedded" in r.stdout and "companies            65" in r.stdout


def test_unreachable_database_gives_friendly_error():
    env = {**os.environ, "DATABASE_URL": "postgresql://x:y@127.0.0.1:1/none?connect_timeout=2"}
    r = run(["status"], env)
    assert r.returncode == 3
    assert "DATABASE_URL=embedded" in r.stderr and "Traceback" not in r.stderr
