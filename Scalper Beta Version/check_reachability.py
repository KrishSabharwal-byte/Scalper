import paramiko

def main():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect('62.72.59.120', username='root', password='Root@#1234567', timeout=15)
    
    commands = [
        "ufw status verbose",
        "ss -tulpn | grep 3005",
        "curl -s -I http://127.0.0.1:3005/",
        "curl -s -I http://62.72.59.120:3005/ || true",
        "ps aux | grep 3005",
        "systemctl is-active astro_scalper_3005.service",
        "systemctl status slicer.service || true",
        "ss -tulpn | grep 6009 || true"
    ]
    
    for cmd in commands:
        print(f"=== {cmd} ===", flush=True)
        stdin, stdout, stderr = ssh.exec_command(cmd)
        out = stdout.read().decode('utf-8', errors='replace').encode('ascii', errors='replace').decode('ascii')
        err = stderr.read().decode('utf-8', errors='replace').encode('ascii', errors='replace').decode('ascii')
        if out.strip():
            print(out.strip(), flush=True)
        if err.strip():
            print("STDERR:", err.strip(), flush=True)
    
    ssh.close()

if __name__ == '__main__':
    main()
