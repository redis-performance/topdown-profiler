"""Generate and install systemd unit files."""

import os
import shutil
import sys
from pathlib import Path

UNIT_TEMPLATE = """\
[Unit]
Description=Top-Down Microarchitecture Analysis Agent ({process_name})
After=network.target

[Service]
Type=simple
ExecStart={topdown_bin} agent --process {process_name} --level {level} --every {every} --duration {duration}
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier={service_name}
{env_lines}

[Install]
WantedBy=multi-user.target
"""


def generate_unit_file(
    process_name: str,
    level: int = 2,
    every: str = "5m",
    duration: str = "30s",
    service_name: str = "topdown-agent",
    db_path: str | None = None,
    backend: str | None = None,
    dsn: str | None = None,
) -> str:
    """Generate systemd unit file content."""
    topdown_bin = shutil.which("topdown") or f"{sys.executable} -m topdown.cli"

    env_lines = ""
    envs = []
    if db_path:
        envs.append(f"Environment=TOPDOWN_DB_PATH={db_path}")
    if backend:
        envs.append(f"Environment=TOPDOWN_BACKEND={backend}")
    if dsn:
        envs.append(f"Environment=TOPDOWN_DSN={dsn}")
    env_lines = "\n".join(envs)

    return UNIT_TEMPLATE.format(
        process_name=process_name,
        level=level,
        every=every,
        duration=duration,
        topdown_bin=topdown_bin,
        service_name=service_name,
        env_lines=env_lines,
    )


def install_service(
    unit_content: str,
    service_name: str = "topdown-agent",
) -> str:
    """Write unit file to /etc/systemd/system/ and enable it."""
    unit_path = Path(f"/etc/systemd/system/{service_name}.service")

    if os.geteuid() != 0:
        raise PermissionError(
            "Root required to install systemd service. "
            "Run: sudo topdown install-service ..."
        )

    unit_path.write_text(unit_content)

    os.system("systemctl daemon-reload")
    os.system(f"systemctl enable {service_name}")

    return str(unit_path)


def get_unit_file_preview(
    process_name: str,
    level: int = 2,
    every: str = "5m",
    duration: str = "30s",
    service_name: str = "topdown-agent",
) -> str:
    """Generate and return unit file content for preview (no install)."""
    return generate_unit_file(
        process_name=process_name,
        level=level,
        every=every,
        duration=duration,
        service_name=service_name,
    )
