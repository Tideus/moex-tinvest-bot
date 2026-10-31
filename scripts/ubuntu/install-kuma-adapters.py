"""Install pilot observer and private relay without displaying or copying broker tokens."""
import argparse
import grp
import ipaddress
import os
import shutil
import stat
import subprocess
from pathlib import Path
from uuid import uuid4

from dotenv import dotenv_values


def private_config_write(path, content, group):
    """No world-readable creation window, symlink following or partial secret file."""
    for parent in (path.parent, *path.parent.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("private config requires root-owned non-writable parent directories")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("private config target must be a regular file, not a symlink")
    temporary = path.with_name("." + path.name + "." + uuid4().hex + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchown(stream.fileno(), 0, group)
            os.fchmod(stream.fileno(), 0o640)
        temporary.replace(path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def main():
    if os.geteuid() != 0:
        raise PermissionError("root required")
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    group = grp.getgrnam("moexbot").gr_gid
    # Permit running adapters, without access to root-only Kuma .env/data.
    os.chown("/opt/moex-monitoring", 0, group)
    config = Path("/etc/moex-observer")
    config.mkdir(mode=0o750, exist_ok=True)
    if config.is_symlink():
        raise ValueError("private config directory must not be a symlink")
    os.chown(config, 0, group)
    state = Path("/var/lib/moex-observer")
    state.mkdir(mode=0o750, exist_ok=True)
    import pwd
    os.chown(state, pwd.getpwnam("moexbot").pw_uid, group)
    gateway = subprocess.check_output([
        "docker", "network", "inspect", "moex-monitoring_default", "--format",
        "{{range .IPAM.Config}}{{.Gateway}}{{end}}"], text=True).strip()
    address = subprocess.check_output([
        "docker", "inspect", "moex-monitoring-uptime-kuma-1", "--format",
        "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"], text=True).strip()
    if not ipaddress.ip_address(gateway).is_private or not ipaddress.ip_address(address).is_private:
        raise ValueError("private Docker network required")
    values = dotenv_values("/etc/moex-tinvest-bot/bot.env")
    relay = {key: values[key] for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALERT_CHAT_ID")}
    relay.update(KUMA_BIND_IP=gateway, KUMA_ALLOWED_IP=address)
    env = config / "relay.env"
    for value in relay.values():
        if not value or any(character in value for character in "\r\n\"\\"):
            raise ValueError("invalid relay setting")
    private_config_write(env, "".join(f'{key}="{value}"\n' for key, value in relay.items()), group)
    for name in ("kuma-exporter.py", "kuma-telegram-relay.py"):
        target = Path("/opt/moex-monitoring") / name
        shutil.copyfile(args.source / name, target)
        target.chmod(0o644)
    for name in ("moex-observer.service", "moex-observer.timer", "moex-kuma-relay.service"):
        shutil.copyfile(args.source / name, Path("/etc/systemd/system") / name)
    subprocess.run(["/opt/moex-tinvest-bot/.venv/bin/python",
                    "/opt/moex-monitoring/kuma-exporter.py", "--provision"], check=True)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", "moex-kuma-relay.service"], check=True)
    subprocess.run(["systemctl", "restart", "moex-kuma-relay.service"], check=True)
    subprocess.run(["systemctl", "start", "moex-observer.service"], check=True)
    subprocess.run(["systemctl", "enable", "--now", "moex-observer.timer"], check=True)
    print(f"Private relay endpoint: http://{gateway}:9088/kuma; source restricted to Kuma IP")


if __name__ == "__main__":
    main()
