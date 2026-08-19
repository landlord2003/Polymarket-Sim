# -*- coding: utf-8 -*-
"""Quant Trading Dashboard launcher.

Why this exists:
  Windows .bat files are read by CMD using the system ANSI codepage (GBK on
  Chinese Windows). Any UTF-8 Chinese / emoji in the .bat gets mangled and can
  break the script ("not a recognized command"). To avoid that entirely, the
  .bat stays pure ASCII and delegates ALL logic here, where Python handles
  encodings correctly.

What it does:
  1. Kill any process still listening on PORT (so we never run stale code).
  2. Locate a usable python interpreter.
  3. Launch a_share/webui.py with live stdout/stderr (you SEE the traceback if
     it crashes, instead of a silent flash).
  4. On exit, print the return code and wait for a key so the window stays open.
"""

import os
import sys
import subprocess

PORT = 8787
ROOT = os.path.dirname(os.path.abspath(__file__))


def kill_port(port):
    """Kill any process listening on the given TCP port (Windows netstat+taskkill)."""
    try:
        proc = subprocess.run(
            ["netstat", "-ano"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
        )
        # netstat on Chinese Windows emits GBK; decode tolerantly so a stray
        # non-UTF8 byte never crashes the launcher.
        out = (proc.stdout or b"").decode("gbk", errors="ignore")
    except Exception:
        return
    for line in out.splitlines():
        if ":{}".format(port) in line and "LISTENING" in line:
            parts = line.split()
            if not parts:
                continue
            pid = parts[-1]
            try:
                subprocess.run(
                    ["taskkill", "/F", "/PID", pid],
                    capture_output=True,
                    timeout=10,
                )
                print("  killed old process PID={} on port {}".format(pid, port))
            except Exception:
                pass


def find_python():
    candidates = [
        os.path.join(ROOT, ".venv", "Scripts", "python.exe"),
        os.path.join(
            os.environ.get("USERPROFILE", ""),
            ".workbuddy", "binaries", "python", "envs", "default",
            "Scripts", "python.exe",
        ),
        "python",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return "python"


def main():
    print("=" * 52)
    print("  Quant Trading Dashboard Launcher")
    print("=" * 52)
    print()

    print("[1/3] Cleaning old process on port {} ...".format(PORT))
    kill_port(PORT)

    print("[2/3] Locating Python ...")
    py = find_python()
    print("  Using: {}".format(py))
    if not os.path.exists(py):
        print("  [ERROR] Python not found: {}".format(py))
        print("  Please check your Python installation.")
        input("Press Enter to close ...")
        sys.exit(1)

    print("[3/3] Starting server at http://127.0.0.1:{}".format(PORT))
    print("  (Keep this window open. Close it to stop the server.)")
    print("  Browser should open automatically.")
    print()

    env = os.environ.copy()
    env["QT_WEB_PORT"] = str(PORT)
    env["PYTHONUNBUFFERED"] = "1"
    webui = os.path.join(ROOT, "a_share", "webui.py")

    try:
        proc = subprocess.run([py, webui], env=env, cwd=ROOT)
        rc = proc.returncode
    except Exception as e:  # pragma: no cover - defensive
        rc = -1
        print("[ERROR] Failed to start webui.py: {}".format(e))

    print()
    print("[Server stopped] exit code: {}".format(rc))
    if rc != 0:
        print("  Something went wrong. The traceback above shows the cause,")
        print("  or check webui.log in the project folder.")
    input("Press Enter to close ...")


if __name__ == "__main__":
    main()
