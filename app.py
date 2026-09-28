
"""
app.py
Root Entry Point untuk Web Dashboard Student Focus Monitoring & Adaptive Pomodoro.

Memungkinkan user menjalankan dashboard langsung dari root folder dengan:
    python app.py
atau:
    python web_dashboard/app.py
"""

import os
import sys
import subprocess

if __name__ == "__main__":
    dashboard_app = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "web_dashboard",
        "app.py"
    )
    cmd = [sys.executable, dashboard_app] + sys.argv[1:]
    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        pass
