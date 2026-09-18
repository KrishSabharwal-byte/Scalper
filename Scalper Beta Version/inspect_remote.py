import os
import time
import sys
import paramiko

HOST = "62.72.59.120"
USER = "root"
PASSWORD = "Root@#1234567"
PORT_DEPLOY = 3005
LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))

def safe_print(text):
    try:
        print(text)
    except Exception:
        print(text.encode('ascii', errors='replace').decode('ascii'))

def main():
    safe_print(f"Connecting to {USER}@{HOST}...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)
    safe_print("Connected successfully!")

    def run_remote(cmd, timeout=30):
        safe_print(f">>> Remote: {cmd}")
        stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode('utf-8', errors='replace')
        err = stderr.read().decode('utf-8', errors='replace')
        if out.strip():
            safe_print(out.strip())
        if err.strip():
            safe_print(f"STDERR: {err.strip()}")
        return out, err

    safe_print("\n--- Inspecting existing services and directories on server ---")
    run_remote("ls -la /root")
    run_remote("ls -la /etc/systemd/system/*.service | grep -E 'astro|scalper|slicer|3005'")
    run_remote(f"ss -tulpn | grep {PORT_DEPLOY}")
    run_remote("systemctl list-units --type=service --state=running | grep -E 'astro|scalper|slicer'")

    ssh.close()

if __name__ == "__main__":
    main()
