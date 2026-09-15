import sys
import paramiko
import json

sys.stdout.reconfigure(encoding='utf-8')

host = "62.72.59.120"
username = "root"
password = "Root@#1234567"

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(host, port=22, username=username, password=password, timeout=15)

cmd = """cd /root/astro_scalper && ./venv/bin/python3 -c "
import urllib.request
import urllib.error
import json

token = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJhZG1pbiIsImNsaWVudF9pZCI6ImFkbWluIiwianRpIjoiYWMzNDBmMWE4MTY2NDg0NWJkNmQ1ZWU5YjRjNGUxNjIiLCJpYXQiOjE3ODkzNjA2NDgsImV4cCI6MTc4OTQ0NzA0OCwidG9rZW5fdHlwZSI6ImFjY2VzcyJ9.9DLXoGn7_GDpnBXIk6BGGTDCFhg8Tvq8wQquy6bS8o4'

# 1. Fetch active state
req_state = urllib.request.Request('http://127.0.0.1:3001/api/state', headers={'Authorization': f'Bearer {token}'})
with urllib.request.urlopen(req_state) as resp:
    state_data = json.loads(resp.read().decode('utf-8'))
    active_run = state_data.get('active_run', {})
    slices = active_run.get('active_slices', [])
    print(f'Active Slices in run01: {len(slices)}')
    for s in slices:
        print('  Slice:', s.get('label'), 'order_id:', s.get('order_id'), 'level_price:', s.get('level_price'))

# 2. Try to exit Slice A via /runs/run01/slices/exit
payload = json.dumps({'slice_id': 'A'}).encode('utf-8')
req_exit = urllib.request.Request(
    'http://127.0.0.1:3001/runs/run01/slices/exit',
    data=payload,
    headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
    method='POST'
)

try:
    with urllib.request.urlopen(req_exit) as resp:
        print('Exit response code:', resp.status)
        print('Exit response body:', resp.read().decode('utf-8'))
except urllib.error.HTTPError as e:
    print('Exit HTTP Error:', e.code, e.read().decode('utf-8'))
except Exception as e:
    print('Exit Error:', e)

# 3. Check active state again after exit call
with urllib.request.urlopen(req_state) as resp:
    state_data_after = json.loads(resp.read().decode('utf-8'))
    slices_after = state_data_after.get('active_run', {}).get('active_slices', [])
    print(f'Active Slices AFTER exit: {len(slices_after)}')
"
"""

stdin, stdout, stderr = client.exec_command(cmd)
print(stdout.read().decode('utf-8', errors='replace'))
print(stderr.read().decode('utf-8', errors='replace'))

client.close()
