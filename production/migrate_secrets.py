#!/usr/bin/env python3
"""Move legacy hard-coded dashboard secrets into a root-owned EnvironmentFile."""

import argparse
import ast
import os
import tempfile


SOURCE_TO_ENV = {
    "STEAM_API_KEY": "STEAM_API_KEY",
    "STEAM_FINANCIAL_KEY": "STEAM_FINANCIAL_KEY",
    "TELEGRAM_BOT_TOKEN": "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_IDS": "TELEGRAM_CHAT_IDS",
}


def read_legacy_values(source_path):
    with open(source_path, "r", encoding="utf-8") as source_file:
        tree = ast.parse(source_file.read(), filename=source_path)

    values = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or target.id not in SOURCE_TO_ENV:
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, TypeError):
            continue
        if isinstance(value, list):
            value = ",".join(str(item) for item in value)
        values[SOURCE_TO_ENV[target.id]] = str(value)
    return values


def systemd_quote(value):
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def write_environment_file(destination_path, values):
    required = ("STEAM_API_KEY", "STEAM_FINANCIAL_KEY")
    missing = [name for name in required if not values.get(name)]
    if missing:
        raise RuntimeError("Missing required legacy values: " + ", ".join(missing))

    destination_dir = os.path.dirname(destination_path)
    fd, temporary_path = tempfile.mkstemp(prefix=".steam-dashboard.", dir=destination_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as destination_file:
            for key in SOURCE_TO_ENV.values():
                destination_file.write(f"{key}={systemd_quote(values.get(key, ''))}\n")
            destination_file.write('STEAM_APP_ID="4451370"\n')
            destination_file.write('STEAM_LAUNCH_DATE="2026-03-13"\n')
            destination_file.write('STEAM_DASHBOARD_PORT="8081"\n')
            destination_file.write('STEAM_POLL_INTERVAL="300"\n')
            destination_file.write('STEAM_FULL_SCAN_INTERVAL="10800"\n')
            destination_file.write('STEAMWORKS_SNAPSHOT_JSON="{}"\n')
            destination_file.flush()
            os.fsync(destination_file.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, destination_path)
    except Exception:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--destination", required=True)
    args = parser.parse_args()
    values = read_legacy_values(args.source)
    write_environment_file(args.destination, values)
    print(f"Wrote {args.destination} with mode 0600; values were not printed.")


if __name__ == "__main__":
    main()
