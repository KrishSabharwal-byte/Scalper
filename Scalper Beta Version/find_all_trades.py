import paramiko

def main():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect('62.72.59.120', username='root', password='Root@#1234567', timeout=15)
    
    cmd = '''
    python3 -u -c "
import glob, json, os

print('Scanning all json files in /root...', flush=True)
files = glob.glob('/root/**/*.json', recursive=True)
print(f'Total json files found: {len(files)}', flush=True)
for f in files:
    if 'node_modules' in f or 'venv' in f or '.cache' in f:
        continue
    try:
        with open(f, 'r', errors='ignore') as fp:
            content = fp.read()
        if 'trade_id' in content or 'pnl_rupees' in content or 'filled_at' in content:
            print(f'MATCH: {f} (size: {os.path.getsize(f)})', flush=True)
            d = json.loads(content)
            if isinstance(d, dict):
                m_trades = d.get('master_trade_history', [])
                print(f'   master_trade_history: {len(m_trades)}', flush=True)
                for rid, r in d.get('runs', {}).items():
                    if isinstance(r, dict):
                        th = r.get('trade_history', [])
                        if th:
                            print(f'   run {rid} trade_history: {len(th)}', flush=True)
            elif isinstance(d, list):
                print(f'   list of {len(d)} items', flush=True)
    except Exception as e:
        pass
print('Done scanning.', flush=True)
"
    '''
    stdin, stdout, stderr = ssh.exec_command(cmd)
    out = stdout.read().decode('utf-8', errors='replace').encode('ascii', errors='replace').decode('ascii')
    print(out)
    ssh.close()

if __name__ == '__main__':
    main()
