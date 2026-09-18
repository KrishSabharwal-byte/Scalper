import os
import time
import sys
import datetime
import paramiko

HOST = "62.72.59.120"
USER = "root"
PASSWORD = "Root@#1234567"
REMOTE_DIR = "/root/astro_scalper_3005"
LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
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

    # Step 1: Backup current remote directory
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = f"/root/astro_scalper_backup_3005_{timestamp}"
    safe_print(f"Creating backup: {REMOTE_DIR} -> {backup_dir}...")
    run_remote(f"cp -r {REMOTE_DIR} {backup_dir} || true")

    # Step 2: Upload local files to REMOTE_DIR
    sftp = ssh.open_sftp()

    def upload_dir(local_path, remote_path):
        try:
            sftp.mkdir(remote_path)
        except IOError:
            pass
        for item in os.listdir(local_path):
            l_item = os.path.join(local_path, item)
            r_item = f"{remote_path}/{item}"
            if os.path.isdir(l_item):
                if item in ['.git', '__pycache__', '.pytest_cache', 'logs', 'venv', 'tests']:
                    continue
                upload_dir(l_item, r_item)
            else:
                if item.endswith('.pyc') or item.startswith('deploy_') or item.startswith('inspect_'):
                    continue
                safe_print(f"Uploading {item} -> {r_item}")
                sftp.put(l_item, r_item)

    safe_print(f"Uploading files from {LOCAL_DIR} to {REMOTE_DIR}...")
    upload_dir(LOCAL_DIR, REMOTE_DIR)
    sftp.close()

    # Step 3: Ensure venv & pip dependencies
    safe_print("Installing / verifying dependencies in venv...")
    run_remote(f"python3 -m venv {REMOTE_DIR}/venv")
    run_remote(f"{REMOTE_DIR}/venv/bin/pip install --upgrade pip")
    run_remote(f"{REMOTE_DIR}/venv/bin/pip install -r {REMOTE_DIR}/requirements.txt")

    # Step 4: Firewall configuration
    run_remote(f"ufw allow {PORT_DEPLOY}/tcp || true")

    # Step 5: Systemd service file
    service_content = f"""[Unit]
Description=Astro Scalper Trading Service (Port {PORT_DEPLOY})
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory={REMOTE_DIR}
ExecStart={REMOTE_DIR}/venv/bin/uvicorn app:app --host 0.0.0.0 --port {PORT_DEPLOY}
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
"""
    sftp = ssh.open_sftp()
    with sftp.file(f"/etc/systemd/system/{SERVICE_NAME}", "w") as f:
        f.write(service_content)
    sftp.close()

    # Step 6: Restart service
    safe_print(f"Reloading systemd and restarting {SERVICE_NAME}...")
    run_remote(f"fuser -k {PORT_DEPLOY}/tcp || true")
    run_remote(f"pkill -9 -f 'uvicorn.*{PORT_DEPLOY}' || true")
    run_remote(f"systemctl kill -s 9 {SERVICE_NAME} || true")
    run_remote("systemctl daemon-reload")
    run_remote(f"systemctl enable {SERVICE_NAME}")
    run_remote(f"systemctl restart {SERVICE_NAME}")

    time.sleep(3)

    # Step 7: Verify service and ports
    run_remote(f"systemctl status {SERVICE_NAME} --no-pager")
    run_remote(f"ss -tulpn | grep {PORT_DEPLOY}")
    run_remote(f"curl -s -I http://127.0.0.1:{PORT_DEPLOY}/")

    ssh.close()
    safe_print(f"\nDeployment to port {PORT_DEPLOY} completed successfully!")

if __name__ == "__main__":
    main()
