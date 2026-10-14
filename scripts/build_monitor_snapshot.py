"""Build and verify a bounded, secret-filtered review snapshot."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 40_000_000
ALLOWED_SUFFIXES = {
    ".css", ".cs", ".html", ".js", ".json", ".jsonc", ".md", ".mjs",
    ".ps1", ".py", ".rs", ".sql", ".toml", ".ts", ".tsx", ".txt", ".yaml", ".yml",
}
BLOCKED_PARTS = {".cache", ".claude", ".codex", ".git", ".venv", "node_modules", "private", "raw", "secrets"}
SENSITIVE_NAME = re.compile(r"(^|[._-])(api[_-]?key|credential|password|secret|token|private[_-]?key)([._-]|$)", re.IGNORECASE)


def git_paths(*args: str) -> set[str]:
    result = subprocess.run(
        ["git", *args, "-z"], cwd=ROOT, capture_output=True, check=True, timeout=30
    )
    return {
        item.decode("utf-8")
        for item in result.stdout.split(b"\0")
        if item
    }


def allowed(relative: Path) -> bool:
    if relative.is_absolute() or ".." in relative.parts:
        return False
    if any(part.lower() in BLOCKED_PARTS for part in relative.parts):
        return False
    if any(SENSITIVE_NAME.search(part) for part in relative.parts):
        return False
    if relative.name.startswith(".env"):
        return False
    return relative.suffix.lower() in ALLOWED_SUFFIXES


def snapshot(destination: Path, manifest_path: Path) -> None:
    destination = destination.resolve()
    manifest_path = manifest_path.resolve()
    if not destination.is_relative_to(ROOT / ".cache" / "design-monitor"):
        raise ValueError("snapshot must stay under the monitor cache")
    if destination.exists():
        raise FileExistsError(destination)
    destination.mkdir(parents=True)

    untracked = git_paths("ls-files", "--others", "--exclude-standard")
    candidates = git_paths("ls-files", "--cached", "--others", "--exclude-standard")
    changed = git_paths("diff", "--name-only", "HEAD") | untracked
    try:
        recent = git_paths("log", "--since=7.days", "--name-only", "--format=")
    except subprocess.CalledProcessError:
        recent = set()

    files: dict[str, str] = {}
    modified_times: dict[str, int] = {}
    total_bytes = 0
    for relative_name in sorted(candidates):
        relative = Path(relative_name)
        if not allowed(relative):
            continue
        source = ROOT / relative
        if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(ROOT):
            continue
        size = source.stat().st_size
        if size > MAX_FILE_BYTES or total_bytes + size > MAX_TOTAL_BYTES:
            continue
        content = source.read_bytes()
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        files[relative.as_posix()] = hashlib.sha256(content).hexdigest()
        modified_times[relative.as_posix()] = source.stat().st_mtime_ns
        total_bytes += size

    required = {"AGENTS.md", "AGENT_HARNESS.md", "CLAUDE.md", "DEVELOPMENT_PROTOCOL.md", "MONITOR_REVIEW.md", "PROJECT_STYLE.md"}
    missing = sorted(required - files.keys())
    if missing:
        raise RuntimeError(f"snapshot is missing required instructions: {missing}")
    manifest = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "file_hashes": files,
        "file_mtimes_ns": modified_times,
        "uncommitted_paths": sorted(set(files) & changed),
        "recent_commit_paths": sorted(set(files) & recent),
        "total_bytes": total_bytes,
    }
    encoded = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    (destination / "SOURCE_MANIFEST.json").write_text(encoded, encoding="utf-8")
    manifest_path.write_text(encoded, encoding="utf-8")
    print(f"Snapshot: {len(files)} files, {total_bytes} bytes")


def verify(destination: Path, manifest_path: Path) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = manifest["file_hashes"]
    actual = {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file() and path.name != "SOURCE_MANIFEST.json"
    }
    if actual != set(expected):
        raise RuntimeError("monitor changed the snapshot file set")
    for relative, expected_hash in expected.items():
        content = (destination / relative).read_bytes()
        if hashlib.sha256(content).hexdigest() != expected_hash:
            raise RuntimeError(f"monitor changed snapshot file: {relative}")
    if (destination / "SOURCE_MANIFEST.json").read_bytes() != manifest_path.read_bytes():
        raise RuntimeError("monitor changed the snapshot manifest")
    print("Snapshot unchanged")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("create", "verify"))
    parser.add_argument("destination", type=Path)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    if args.operation == "create":
        snapshot(args.destination, args.manifest)
    else:
        verify(args.destination, args.manifest)


if __name__ == "__main__":
    main()
