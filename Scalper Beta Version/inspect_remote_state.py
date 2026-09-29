import paramiko

def main():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect('62.72.59.120', username='root', password='Root@#1234567', timeout=15)
    
    cmd = '''
    python3 -c "
import json
try:
    with open('/root/astro_scalper_3005/users_state.json') as f:
        u = json.load(f)
    print('Users in state:', list(u.keys()))
except Exception as e:
    print('Error users:', e)

try:
    with open('/root/astro_scalper_3005/credentials_state.json') as f:
        c = json.load(f)
    print('Credentials in state:', list(c.keys()))
except Exception as e:
    print('Error credentials:', e)
"
    '''
    stdin, stdout, stderr = ssh.exec_command(cmd)
    out = stdout.read().decode('utf-8', errors='replace').encode('ascii', errors='replace').decode('ascii')
    print(out)
    err = stderr.read().decode('utf-8', errors='replace').encode('ascii', errors='replace').decode('ascii')
    if err:
        print('STDERR:', err)
    ssh.close()

if __name__ == '__main__':
    main()
