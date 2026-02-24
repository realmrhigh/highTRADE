import subprocess
import os
import json

cmd = ['gemini', '-p', 'ping', '--model', 'gemini-2.5-flash', '--output-format', 'json']
env = {**os.environ, 'GEMINI_API_KEY': ''}

print(f"Running command: {' '.join(cmd)}")
try:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
    print(f"Return code: {result.returncode}")
    print(f"STDOUT: {result.stdout[:100]}...")
    print(f"STDERR: {result.stderr[:100]}...")
except Exception as e:
    print(f"Exception: {e}")
