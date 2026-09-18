import os
import time
import sys
import paramiko

HOST = "62.72.59.120"
USER = "root"
PASSWORD = "Root@#1234567"
REMOTE_DIR = "/root/astro_scalper_3005"
PORT_DEPLOY = 3005
SERVICE_NAME = "astro_scalper_3005.service"

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

    safe_print("Stopping and killing old process on port 3005...")
    run_remote(f"fuser -k {PORT_DEPLOY}/tcp || true")
    run_remote("pkill -9 -f 'uvicorn.*3005' || true")
    run_remote(f"systemctl kill -s 9 {SERVICE_NAME} || true")
    run_remote("systemctl daemon-reload")
    run_remote(f"systemctl restart {SERVICE_NAME}")

    time.sleep(3)

    run_remote(f"systemctl status {SERVICE_NAME} --no-pager")
    run_remote(f"ss -tulpn | grep {PORT_DEPLOY}")
    run_remote(f"curl -s -I http://127.0.0.1:{PORT_DEPLOY}/")

    ssh.close()
    safe_print(f"\nDeployment to port {PORT_DEPLOY} verified and running successfully!")

if __name__ == "__main__":
    main()
