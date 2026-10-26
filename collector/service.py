"""Render scheduler snippets for cron, systemd, launchd, and Task Scheduler.

Writing unit files is local config. Enabling the timer is a separate step
so a preview never starts a background job.
"""
from __future__ import annotations

import os
import platform
import shlex
import sys
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape

from collector.config import env_file_path, repo_root, state_dir


def cron_expression(minutes: int) -> str:
    minutes = max(int(minutes), 1)
    if minutes < 60 and 60 % minutes == 0:
        return f"*/{minutes} * * * *"
    if minutes % 60 == 0:
        hours = minutes // 60
        if hours == 1:
            return "0 * * * *"
        if hours < 24 and 24 % hours == 0:
            return f"0 */{hours} * * *"
    return "*/30 * * * *"


def cron_line(
    *,
    python: str,
    repo: Path,
    interval_minutes: int,
    log_path: Path,
) -> str:
    expr = cron_expression(interval_minutes)
    return (
        f"{expr} cd {shlex.quote(str(repo))} && "
        f"{shlex.quote(python)} -m collector once "
        f">> {shlex.quote(str(log_path))} 2>&1"
    )


def render_systemd(*, python: str, repo: Path, interval_minutes: int, env_file: Path) -> tuple[str, str]:
    service = "\n".join(
        [
            "[Unit]",
            "Description=Token 消耗排行榜本地采集（只上传 Token 数字）",
            "After=network-online.target",
            "",
            "[Service]",
            "Type=oneshot",
            f"WorkingDirectory={repo}",
            f"Environment=PYTHONPATH={repo}",
            f"EnvironmentFile=-{env_file}",
            f"ExecStart={python} -m collector once",
            "",
        ]
    )
    timer = "\n".join(
        [
            "[Unit]",
            f"Description=每 {interval_minutes} 分钟采集一次本地 AI 编程工具用量",
            "",
            "[Timer]",
            "OnBootSec=2min",
            f"OnUnitActiveSec={interval_minutes}min",
            "AccuracySec=1min",
            "Persistent=true",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ]
    )
    return service, timer


def render_launchd(
    *,
    python: str,
    repo: Path,
    interval_minutes: int,
    log_path: Path,
    env_file: Path,
) -> str:
    interval = max(int(interval_minutes), 1) * 60
    args = "\n".join(
        f"    <string>{escape(part)}</string>"
        for part in (python, "-m", "collector", "once")
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.tokenrankings.collector</string>
  <key>WorkingDirectory</key>
  <string>{escape(str(repo))}</string>
  <key>ProgramArguments</key>
  <array>
{args}
  </array>
  <key>StartInterval</key>
  <integer>{interval}</integer>
  <key>RunAtLoad</key>
  <true/>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONPATH</key>
    <string>{escape(str(repo))}</string>
  </dict>
  <key>StandardOutPath</key>
  <string>{escape(str(log_path))}</string>
  <key>StandardErrorPath</key>
  <string>{escape(str(log_path))}</string>
  <key>Comment</key>
  <string>只上传 Token 数字。密钥放在 {escape(str(env_file))}，由采集进程自己读取。</string>
</dict>
</plist>
"""


def render_windows_ps1(*, python: str, repo: Path, interval_minutes: int) -> str:
    return "\n".join(
        [
            "# Token 消耗排行榜本地采集。只上传 Token 数字。",
            f"$env:PYTHONPATH = '{repo}'",
            f"Set-Location -LiteralPath '{repo}'",
            f"& '{python}' -m collector once",
            f"# 注册计划任务（每 {interval_minutes} 分钟）：",
            (
                "schtasks /Create /F /TN \"TokenRankingsCollector\" "
                f"/SC MINUTE /MO {max(int(interval_minutes), 1)} "
                f"/TR \"`\"{python}`\" -m collector once\""
            ),
            "",
        ]
    )


def detect_os(name: Optional[str] = None) -> str:
    if name and name != "auto":
        return name
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    if system == "windows":
        return "windows"
    return "linux"


def render(os_name: str, *, interval_minutes: int, python: Optional[str] = None, repo: Optional[Path] = None) -> str:
    py = python or sys.executable
    root = repo or repo_root()
    log_path = state_dir() / "collector.log"
    env_file = env_file_path()
    if os_name == "cron":
        return cron_line(
            python=py,
            repo=root,
            interval_minutes=interval_minutes,
            log_path=log_path,
        ) + "\n"
    if os_name == "linux":
        service, timer = render_systemd(
            python=py,
            repo=root,
            interval_minutes=interval_minutes,
            env_file=env_file,
        )
        return (
            "# ~/.config/systemd/user/token-rankings-collector.service\n"
            + service
            + "\n# ~/.config/systemd/user/token-rankings-collector.timer\n"
            + timer
        )
    if os_name == "macos":
        return render_launchd(
            python=py,
            repo=root,
            interval_minutes=interval_minutes,
            log_path=log_path,
            env_file=env_file,
        )
    if os_name == "windows":
        return render_windows_ps1(python=py, repo=root, interval_minutes=interval_minutes)
    raise ValueError(f"不认识的系统：{os_name}")


def install_files(os_name: str, *, interval_minutes: int) -> list[Path]:
    """Write unit files. Does not enable them."""
    py = sys.executable
    root = repo_root()
    log_path = state_dir() / "collector.log"
    state_dir().mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    if os_name == "linux":
        unit_dir = Path.home() / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        service, timer = render_systemd(
            python=py,
            repo=root,
            interval_minutes=interval_minutes,
            env_file=env_file_path(),
        )
        service_path = unit_dir / "token-rankings-collector.service"
        timer_path = unit_dir / "token-rankings-collector.timer"
        service_path.write_text(service, encoding="utf-8")
        timer_path.write_text(timer, encoding="utf-8")
        written.extend([service_path, timer_path])
        return written
    if os_name == "macos":
        dest_dir = Path.home() / "Library" / "LaunchAgents"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / "com.tokenrankings.collector.plist"
        dest.write_text(
            render_launchd(
                python=py,
                repo=root,
                interval_minutes=interval_minutes,
                log_path=log_path,
                env_file=env_file_path(),
            ),
            encoding="utf-8",
        )
        written.append(dest)
        return written
    if os_name == "windows":
        dest = state_dir() / "register-task.ps1"
        dest.write_text(
            render_windows_ps1(python=py, repo=root, interval_minutes=interval_minutes),
            encoding="utf-8",
        )
        written.append(dest)
        return written
    if os_name == "cron":
        dest = state_dir() / "collector.cron"
        dest.write_text(
            cron_line(
                python=py,
                repo=root,
                interval_minutes=interval_minutes,
                log_path=log_path,
            )
            + "\n",
            encoding="utf-8",
        )
        written.append(dest)
        return written
    raise ValueError(f"不认识的系统：{os_name}")


def enable_hint(os_name: str) -> str:
    if os_name == "linux":
        return (
            "systemctl --user daemon-reload && "
            "systemctl --user enable --now token-rankings-collector.timer"
        )
    if os_name == "macos":
        plist = Path.home() / "Library" / "LaunchAgents" / "com.tokenrankings.collector.plist"
        return f"launchctl load -w {shlex.quote(str(plist))}"
    if os_name == "windows":
        return "在 PowerShell 里查看已写出的 register-task.ps1，确认路径后执行其中的 schtasks。"
    if os_name == "cron":
        return "crontab -e 后粘贴 collector.cron 里的那一行。已有 crontab 时先 crontab -l 核对，避免覆盖。"
    return ""


def try_enable_systemd() -> str:
    """Best-effort enable. Failure is returned as text, not an exception."""
    import subprocess

    if not os.environ.get("XDG_RUNTIME_DIR"):
        return "没有 XDG_RUNTIME_DIR，跳过 systemctl --user。请在图形会话或 lingering 用户下手动执行。"
    cmd = ["systemctl", "--user", "daemon-reload"]
    try:
        subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=20)
        result = subprocess.run(
            ["systemctl", "--user", "enable", "--now", "token-rankings-collector.timer"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"systemctl 未执行：{exc}"
    text = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        return f"systemctl 退出 {result.returncode}：{text.strip()}"
    return "已 enable --now token-rankings-collector.timer"
