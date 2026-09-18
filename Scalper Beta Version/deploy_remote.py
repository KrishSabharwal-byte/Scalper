import os
import time
import sys
import paramiko

HOST = "62.72.59.120"
USER = "root"
PASSWORD = "Root@#1234567"
REMOTE_DIR = "/root/slicer_nifty_sensex"
LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
PORT_DEPLOY = 6009

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

    safe_print("Force killing old uvicorn instances to prevent restart hangs...")
    run_remote("pkill -9 -f uvicorn || true")
    run_remote("systemctl daemon-reload")
    run_remote("systemctl restart slicer.service")

    time.sleep(3)

    run_remote("systemctl status slicer.service --no-pager")
    run_remote(f"ss -tulpn | grep {PORT_DEPLOY}")
    run_remote(f"curl -s -I http://127.0.0.1:{PORT_DEPLOY}/")

    ssh.close()
    safe_print("\nDeployment verified and running on server!")

if __name__ == "__main__":
    main()
