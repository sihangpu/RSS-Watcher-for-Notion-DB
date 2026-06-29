#!/usr/bin/env python3
"""Background runner and Windows startup helpers for the RSS watcher."""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import rss_watcher

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "rss_config.json"
ENV_PATH = ROOT / ".env"
LOG_PATH = ROOT / "rss_watcher.log"
PID_PATH = ROOT / "rss_daemon.pid"
STOP_PATH = ROOT / "rss_daemon.stop"
TASK_NAME = "RSS-Notion-Watcher"

DEFAULT_CONFIG: dict[str, Any] = {
    "notion_token": "",
    "notion_database_id": "",
    "notion_database_name": rss_watcher.DEFAULT_DATABASE_NAME,
    "rss_url": rss_watcher.DEFAULT_RSS_URL,
    "rss_source_name": rss_watcher.DEFAULT_SOURCE_NAME,
    "rss_user_agent": "",
    "interval_minutes": 60,
    "seen_state_path": "",
    "bootstrap_seen": "1",
}

ENV_OVERRIDES = {
    "notion_token": "NOTION_TOKEN",
    "notion_database_id": "NOTION_DATABASE_ID",
    "notion_database_name": "NOTION_DATABASE_NAME",
    "rss_url": "RSS_URL",
    "rss_source_name": "RSS_SOURCE_NAME",
    "rss_user_agent": "RSS_USER_AGENT",
    "interval_minutes": "RSS_INTERVAL_MINUTES",
    "seen_state_path": "RSS_SEEN_STATE_PATH",
    "bootstrap_seen": "RSS_BOOTSTRAP_SEEN",
}

ENV_TEMPLATE = """# Local RSS-to-Notion settings. This file is ignored by git.
NOTION_TOKEN=
NOTION_DATABASE_ID=
NOTION_DATABASE_NAME=RSS Feeds
RSS_URL=https://eprint.iacr.org/rss/rss.xml?format=nonstandard
RSS_SOURCE_NAME=IACR ePrint
RSS_USER_AGENT=
RSS_INTERVAL_MINUTES=60
RSS_SEEN_STATE_PATH=
RSS_BOOTSTRAP_SEEN=1
"""


def normalize_config(config: dict[str, Any]) -> dict[str, Any]:
    normalized = DEFAULT_CONFIG | {key: config.get(key, value) for key, value in DEFAULT_CONFIG.items()}
    try:
        normalized["interval_minutes"] = max(1, int(normalized.get("interval_minutes") or 60))
    except (TypeError, ValueError):
        normalized["interval_minutes"] = 60
    for key in DEFAULT_CONFIG:
        if key != "interval_minutes":
            normalized[key] = str(normalized.get(key, "")).strip()
    return normalized


def parse_env_file(path: Path = ENV_PATH) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def apply_env_values(config: dict[str, Any], values: dict[str, str]) -> None:
    for key, env_name in ENV_OVERRIDES.items():
        value = values.get(env_name)
        if value:
            config[key] = value


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    config = DEFAULT_CONFIG.copy()
    if path.exists():
        with path.open("r", encoding="utf-8") as file:
            saved = json.load(file)
        if not isinstance(saved, dict):
            raise RuntimeError(f"{path} must contain a JSON object.")
        config.update(saved)
    apply_env_values(config, parse_env_file())
    apply_env_values(config, os.environ)
    return normalize_config(config)


def save_config(config: dict[str, Any], path: Path = CONFIG_PATH) -> None:
    normalized = normalize_config(config)
    path.write_text(json.dumps(normalized, indent=2) + "\n", encoding="utf-8")


def init_env_file(path: Path = ENV_PATH) -> bool:
    if path.exists():
        return False
    path.write_text(ENV_TEMPLATE, encoding="utf-8")
    return True


@contextmanager
def watcher_environment(config: dict[str, Any]):
    mapping = {
        "NOTION_TOKEN": config["notion_token"],
        "NOTION_DATABASE_ID": config["notion_database_id"],
        "NOTION_DATABASE_NAME": config["notion_database_name"],
        "RSS_URL": config["rss_url"],
        "RSS_SOURCE_NAME": config["rss_source_name"],
        "RSS_USER_AGENT": config["rss_user_agent"],
        "RSS_SEEN_STATE_PATH": config["seen_state_path"],
        "RSS_BOOTSTRAP_SEEN": config["bootstrap_seen"],
    }
    previous = {key: os.environ.get(key) for key in mapping}
    try:
        for key, value in mapping.items():
            if value:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def run_once(config: dict[str, Any] | None = None, item_limit: int | None = None) -> dict[str, Any]:
    config = load_config() if config is None else normalize_config(config)
    if not config["notion_token"]:
        raise RuntimeError("Missing Notion token. Save it in the wrapper or set NOTION_TOKEN.")
    if not config["rss_url"]:
        raise RuntimeError("Missing RSS URL.")

    with watcher_environment(config):
        token = config["notion_token"]
        all_items = rss_watcher.parse_rss(rss_watcher.fetch_text(config["rss_url"]))
        items = all_items
        if item_limit is not None:
            items = items[:item_limit]
        result = rss_watcher.create_new_items(
            config["notion_database_id"] or None,
            token,
            items,
            config["rss_source_name"],
            seen_items=all_items,
            database_name=config["notion_database_name"],
        )
        return {
            "rss_url": config["rss_url"],
            "database_id": config["notion_database_id"],
            "processed": result.processed,
            "created": result.created,
            "skipped_seen": result.skipped_seen,
            "bootstrapped_seen": result.bootstrapped_seen,
        }


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("rss_daemon")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def read_pid() -> int | None:
    try:
        return int(PID_PATH.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return None


def is_pid_running(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def sleep_until_next_run(seconds: int) -> bool:
    end_time = time.monotonic() + seconds
    while time.monotonic() < end_time:
        if STOP_PATH.exists():
            return False
        time.sleep(min(5, max(0, end_time - time.monotonic())))
    return not STOP_PATH.exists()


def watch_forever() -> None:
    logger = setup_logging()
    STOP_PATH.unlink(missing_ok=True)
    PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    logger.info("RSS watcher background service started.")
    try:
        while not STOP_PATH.exists():
            config = load_config()
            try:
                result = run_once(config)
                logger.info(
                    "Run complete: processed=%s created=%s skipped_seen=%s bootstrapped_seen=%s",
                    result["processed"],
                    result["created"],
                    result["skipped_seen"],
                    result["bootstrapped_seen"],
                )
            except Exception:
                logger.exception("Run failed.")
            interval_seconds = int(config["interval_minutes"]) * 60
            if not sleep_until_next_run(interval_seconds):
                break
    finally:
        logger.info("RSS watcher background service stopped.")
        PID_PATH.unlink(missing_ok=True)
        STOP_PATH.unlink(missing_ok=True)


def pythonw_executable() -> str:
    executable = Path(sys.executable)
    if os.name == "nt":
        candidate = executable.with_name("pythonw.exe")
        if candidate.exists():
            return str(candidate)
    return str(executable)


def background_command() -> list[str]:
    return [pythonw_executable(), str(ROOT / "rss_daemon.py"), "--watch"]


def start_background() -> bool:
    if is_pid_running(read_pid()):
        return False
    STOP_PATH.unlink(missing_ok=True)
    kwargs: dict[str, Any] = {
        "cwd": str(ROOT),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(background_command(), **kwargs)
    return True


def stop_background(timeout_seconds: int = 30) -> bool:
    pid = read_pid()
    if not is_pid_running(pid):
        PID_PATH.unlink(missing_ok=True)
        STOP_PATH.unlink(missing_ok=True)
        return True
    STOP_PATH.write_text("stop\n", encoding="utf-8")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not is_pid_running(pid):
            return True
        time.sleep(1)
    return False


def install_startup_task(trigger: str = "logon") -> subprocess.CompletedProcess[str]:
    if os.name != "nt":
        raise RuntimeError("Windows Task Scheduler is only available on Windows.")
    schedule = "ONSTART" if trigger == "boot" else "ONLOGON"
    task_command = subprocess.list2cmdline(background_command())
    return subprocess.run(
        [
            "schtasks",
            "/Create",
            "/TN",
            TASK_NAME,
            "/SC",
            schedule,
            "/TR",
            task_command,
            "/RL",
            "LIMITED",
            "/F",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def uninstall_startup_task() -> subprocess.CompletedProcess[str]:
    if os.name != "nt":
        raise RuntimeError("Windows Task Scheduler is only available on Windows.")
    return subprocess.run(
        ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
        capture_output=True,
        text=True,
        check=False,
    )


def task_installed() -> bool:
    if os.name != "nt":
        return False
    result = subprocess.run(
        ["schtasks", "/Query", "/TN", TASK_NAME],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def service_status() -> dict[str, Any]:
    pid = read_pid()
    return {
        "pid": pid,
        "running": is_pid_running(pid),
        "task_installed": task_installed(),
        "config_path": str(CONFIG_PATH),
        "env_path": str(ENV_PATH),
        "seen_state_path": str(rss_watcher.seen_state_path()),
        "log_path": str(LOG_PATH),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run or manage the RSS-to-Notion background watcher.")
    parser.add_argument("--once", action="store_true", help="Run the watcher one time.")
    parser.add_argument("--watch", action="store_true", help="Run forever, sleeping between checks.")
    parser.add_argument("--start", action="store_true", help="Start the background watcher.")
    parser.add_argument("--stop", action="store_true", help="Ask the background watcher to stop.")
    parser.add_argument("--status", action="store_true", help="Print background watcher status.")
    parser.add_argument("--install-startup", action="store_true", help="Install a Windows task that starts at login.")
    parser.add_argument("--install-boot", action="store_true", help="Install a Windows task that starts at boot.")
    parser.add_argument("--uninstall-startup", action="store_true", help="Remove the Windows startup task.")
    parser.add_argument("--init-env", action="store_true", help="Create an ignored .env template for local settings.")
    parser.add_argument("--limit", type=int, help="Limit feed items for --once.")
    args = parser.parse_args()

    if args.watch:
        watch_forever()
        return 0
    if args.once:
        result = run_once(item_limit=args.limit)
        print(
            "Processed {processed} feed items; created={created}, skipped_seen={skipped_seen}, "
            "bootstrapped_seen={bootstrapped_seen}".format(
                **result
            )
        )
        return 0
    if args.start:
        print("started" if start_background() else "already-running")
        return 0
    if args.stop:
        print("stopped" if stop_background() else "stop-requested")
        return 0
    if args.install_startup or args.install_boot:
        result = install_startup_task("boot" if args.install_boot else "logon")
        print((result.stdout or result.stderr).strip())
        return result.returncode
    if args.uninstall_startup:
        result = uninstall_startup_task()
        print((result.stdout or result.stderr).strip())
        return 0 if result.returncode in {0, 1} else result.returncode
    if args.init_env:
        print(f"created {ENV_PATH}" if init_env_file() else f"already exists {ENV_PATH}")
        return 0

    status = service_status()
    print(
        f"running={status['running']} pid={status['pid'] or ''} "
        f"task_installed={status['task_installed']} config={status['config_path']} "
        f"env={status['env_path']} seen_state={status['seen_state_path']} log={status['log_path']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
