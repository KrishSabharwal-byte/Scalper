import sys
import paramiko

sys.stdout.reconfigure(encoding='utf-8')

host = "62.72.59.120"
username = "root"
password = "Root@#1234567"

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(host, port=22, username=username, password=password, timeout=15)

# Let us check what users are currently connected in uvicorn access logs or recent logs
stdin, stdout, stderr = client.exec_command("journalctl -u astro_scalper -n 30 --no-pager")
print(stdout.read().decode('utf-8', errors='replace'))

client.close()
