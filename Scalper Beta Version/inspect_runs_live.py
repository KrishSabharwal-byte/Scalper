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

# Let us query /api/state using the admin token from logs or check active runs
"
"""

# Let us check recent journalctl logs for slicer grid generation, ticks, or errors
stdin, stdout, stderr = client.exec_command("journalctl -u astro_scalper -n 80 --no-pager")
print(stdout.read().decode('utf-8', errors='replace'))

client.close()
