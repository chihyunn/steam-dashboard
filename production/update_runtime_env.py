#!/usr/bin/env python3
"""Update selected systemd EnvironmentFile values without printing secrets."""

import argparse
import json
import os
import shlex
import stat
import tempfile
from pathlib import Path


def decode_value(raw_value):
    stripped = raw_value.strip()
    if stripped.startswith(("{", "[")):
        return stripped
    tokens = shlex.split(stripped, posix=True)
    if len(tokens) != 1:
        raise ValueError("environment value must decode to one token")
    return tokens[0]


def encode_value(value):
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def split_assignment(value):
    if "=" not in value:
        raise argparse.ArgumentTypeError("expected KEY=VALUE")
    key, item_value = value.split("=", 1)
    if not key or not key.replace("_", "a").isalnum() or key[0].isdigit():
        raise argparse.ArgumentTypeError("invalid environment key")
    return key, item_value


def update_environment_file(path, plain_updates, json_updates):
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines()
    trailing_newline = original.endswith("\n")
    positions = {}
    existing = {}

    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        positions[key] = index
        existing[key] = raw_value

    updates = dict(plain_updates)
    for key, patch_path in json_updates.items():
        current = {}
        if key in existing:
            decoded = decode_value(existing[key])
            parsed = json.loads(decoded)
            if not isinstance(parsed, dict):
                raise ValueError(f"{key} must contain a JSON object")
            current = parsed
        patch = json.loads(patch_path.read_text(encoding="utf-8"))
        if not isinstance(patch, dict):
            raise ValueError(f"JSON patch for {key} must be an object")
        current.update(patch)
        updates[key] = json.dumps(current, ensure_ascii=False, separators=(",", ":"))

    for key, value in updates.items():
        rendered = f"{key}={encode_value(value)}"
        if key in positions:
            lines[positions[key]] = rendered
        else:
            positions[key] = len(lines)
            lines.append(rendered)

    rendered_file = "\n".join(lines)
    if trailing_newline or lines:
        rendered_file += "\n"

    original_stat = path.stat()
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(rendered_file)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, stat.S_IMODE(original_stat.st_mode))
        os.chown(temporary_path, original_stat.st_uid, original_stat.st_gid)
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()

    return sorted(updates)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("env_file", type=Path)
    parser.add_argument("--set", action="append", default=[], type=split_assignment)
    parser.add_argument("--merge-json", action="append", default=[], type=split_assignment)
    return parser.parse_args()


def main():
    args = parse_args()
    plain_updates = dict(args.set)
    json_updates = {key: Path(value) for key, value in args.merge_json}
    updated = update_environment_file(args.env_file, plain_updates, json_updates)
    print("updated keys: " + ", ".join(updated))


if __name__ == "__main__":
    main()
