import sys
import paramiko

sys.stdout.reconfigure(encoding='utf-8')

host = "62.72.59.120"
username = "root"
password = "Root@#1234567"

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(host, port=22, username=username, password=password, timeout=15)

cmd = """cd /root/astro_scalper && ./venv/bin/python3 -c "
import urllib.request
import json

# Let us check what users are logged in by inspecting the process memory via /api/runs or auth tokens
import glob
print('Checking logs...')
"
"""

stdin, stdout, stderr = client.exec_command("journalctl -u astro_scalper -n 100 --no-pager | grep -E 'client=|Astro Auto-Trigger|Starting|started'")
print("=== RECENT RELEVANT LOGS ===")
print(stdout.read().decode('utf-8', errors='replace'))

client.close()
