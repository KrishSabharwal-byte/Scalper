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
import json

token = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJhZG1pbiIsImNsaWVudF9pZCI6ImFkbWluIiwianRpIjoiYWMzNDBmMWE4MTY2NDg0NWJkNmQ1ZWU5YjRjNGUxNjIiLCJpYXQiOjE3ODkzNjA2NDgsImV4cCI6MTc4OTQ0NzA0OCwidG9rZW5fdHlwZSI6ImFjY2VzcyJ9.9DLXoGn7_GDpnBXIk6BGGTDCFhg8Tvq8wQquy6bS8o4'

req = urllib.request.Request('http://127.0.0.1:3001/api/state', headers={'Authorization': f'Bearer {token}'})
try:
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode('utf-8'))
        print('=== ACTIVE RUN SUMMARY ===')
        active_run = data.get('active_run', {})
        print('is_active:', active_run.get('is_active'))
        print('run_id:', active_run.get('run_id'))
        print('instrument:', active_run.get('instrument'))
        print('contract:', active_run.get('contract_symbol'))
        print('last_ltp:', active_run.get('last_ltp'))
        print('range_high:', active_run.get('range_high'), 'range_low:', active_run.get('range_low'))
        print('grid_ladder levels count:', len(active_run.get('grid_ladder', [])))
        for lvl in active_run.get('grid_ladder', []):
            print('  Level:', lvl)
        print('active_slices count:', len(active_run.get('active_slices', [])))
        for sl in active_run.get('active_slices', []):
            print('  Slice:', sl)
        print('runs list:')
        for r in data.get('runs', []):
            print('  Run:', r.get('run_id'), 'is_active:', r.get('is_active'), 'contract:', r.get('contract_symbol'), 'last_ltp:', r.get('last_ltp'))
except Exception as e:
    print('Error fetching state:', e)
"
"""

stdin, stdout, stderr = client.exec_command(cmd)
print(stdout.read().decode('utf-8', errors='replace'))
print(stderr.read().decode('utf-8', errors='replace'))

client.close()
