"""Native app-server is PID1; the image-owned fixed socket relay is its child."""
import os
import subprocess
import sys

subprocess.Popen([sys.executable, '/usr/local/lib/rocoto/relay.py'],
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
os.execv('/usr/local/bin/codex', ['codex', 'app-server', '--listen', 'stdio://', *sys.argv[1:]])
