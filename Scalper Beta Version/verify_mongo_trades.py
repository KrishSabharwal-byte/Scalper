import paramiko

def main():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect('62.72.59.120', username='root', password='Root@#1234567', timeout=15)
    
    cmd = "cd /root/astro_scalper_3005 && /root/astro_scalper_3005/venv/bin/python -c \"import sys; sys.path.insert(0, '.'); from mongo_service import mongo_service; print('Admin trades:', len(mongo_service.get_recent_trades('admin', limit=10))); print('User 1 trades:', len(mongo_service.get_recent_trades('User 1', limit=10)))\""
    stdin, stdout, stderr = ssh.exec_command(cmd)
    print(stdout.read().decode('utf-8', errors='replace'))
    err = stderr.read().decode('utf-8', errors='replace')
    if err:
        print("STDERR:", err)
    ssh.close()

if __name__ == '__main__':
    main()
