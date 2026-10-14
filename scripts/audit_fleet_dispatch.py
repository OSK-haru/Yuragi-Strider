"""Dispatch bounded audit roles from 12-hour slots and per-role file changes."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import subprocess
import time

from build_monitor_snapshot import ROOT, allowed, git_paths


STATE_PATH = ROOT / ".cache" / "design-monitor" / "fleet-state.json"
RUNNER = ROOT / "scripts" / "run_design_monitor.ps1"
PERIODIC = {"1", "3", "7"}
ORDER = ("1", "3", "7", "4", "5", "2", "6")
MAX_ROLES_PER_TICK = 1
MAX_CHANGED_FILES_PER_RUN = 2
MAX_ATTEMPTS = 1
# Completed audits use the user-approved quota. Failed starts have a separate
# circuit breaker because a failed stream may still incur usage charges.
MAX_COMPLETED_AUDITS_PER_DAY = 4
MAX_FAILED_MODEL_ATTEMPTS_PER_DAY = 2
MAX_USER_AUTHORIZED_EXTRA_STARTS = 2


def role_path(role: str, path: str) -> bool:
    attack_docs = path.startswith("docs_for_develop/development/attack-simulation/")
    attack_core = path.startswith(("core/attack_simulation/", "core/experimental_isolation_physics/"))
    if role == "2":
        return attack_docs or path.startswith(("core/", "api/", "frontend/src/")) and path.endswith((".py", ".ts", ".tsx", ".md"))
    if role == "4":
        return attack_core or attack_docs or path.startswith(("validation_", "docs/validation/")) or path.startswith("tests/test_reset") or path.startswith(("scripts/experiment_", "scripts/calibrate_"))
    if role == "5":
        return attack_docs or path.startswith(("core/", "api/", "frontend/src/", "tests/")) and path.endswith((".py", ".ts", ".tsx"))
    if role == "6":
        return path.startswith("frontend/") or path.startswith("api/") and "attack" in path or path == "AUDIT_FLEET.md"
    raise ValueError(role)


def file_hashes() -> dict[str, str]:
    candidates = git_paths("ls-files", "--cached", "--others", "--exclude-standard")
    result = {}
    for name in sorted(candidates):
        relative = Path(name)
        if not allowed(relative):
            continue
        path = ROOT / relative
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(ROOT):
            continue
        if path.stat().st_size > 1_000_000:
            continue
        result[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def current_slot(now: datetime) -> str:
    if now.hour >= 21:
        slot = now.replace(hour=21, minute=0, second=0, microsecond=0)
    elif now.hour >= 9:
        slot = now.replace(hour=9, minute=0, second=0, microsecond=0)
    else:
        slot = (now - timedelta(days=1)).replace(hour=21, minute=0, second=0, microsecond=0)
    return slot.isoformat()


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"version": 2, "roles": {}}
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    if not isinstance(state.get("roles"), dict):
        raise RuntimeError("invalid audit fleet state")
    if state.get("version") == 1:
        # Retain completed time slots, but rebuild diff queues with exact paths.
        return {"version": 2, "roles": {
            role: {"completed_slot": entry["completed_slot"]}
            for role, entry in state["roles"].items()
            if role in PERIODIC and "completed_slot" in entry
        }}
    if state.get("version") != 2:
        raise RuntimeError("unsupported audit fleet state")
    return state


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(STATE_PATH)


def selection_key(paths: list[str], hashes: dict[str, str]) -> str:
    payload = [(path, hashes.get(path, "[deleted]")) for path in paths]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode("utf-8")).hexdigest()


def daily_budget(state: dict, now: datetime) -> dict:
    budget = state.get("model_budget", {})
    if budget.get("date") != now.date().isoformat():
        return {"date": now.date().isoformat(), "completed": 0, "failed_attempts": 0, "attempts": 0,
                "manual_extra_limit": 0, "manual_extra_used": 0}
    return {
        "date": budget["date"],
        "completed": int(budget.get("completed", 0)),
        "failed_attempts": int(budget.get("failed_attempts", 0)),
        "attempts": int(budget.get("attempts", budget.get("launches", 0))),
        "manual_extra_limit": int(budget.get("manual_extra_limit", 0)),
        "manual_extra_used": int(budget.get("manual_extra_used", 0)),
    }


def model_budget_remaining(state: dict, now: datetime) -> int:
    return max(0, MAX_COMPLETED_AUDITS_PER_DAY - daily_budget(state, now)["completed"])


def can_start_model(state: dict, now: datetime, manual_refresh: bool = False) -> bool:
    budget = daily_budget(state, now)
    normal_capacity = (
        budget["completed"] < MAX_COMPLETED_AUDITS_PER_DAY
        and budget["failed_attempts"] < MAX_FAILED_MODEL_ATTEMPTS_PER_DAY
        and budget["attempts"] < MAX_COMPLETED_AUDITS_PER_DAY + MAX_FAILED_MODEL_ATTEMPTS_PER_DAY
    )
    if normal_capacity:
        return True
    return bool(
        manual_refresh
        and budget["completed"] < MAX_COMPLETED_AUDITS_PER_DAY
        and budget["manual_extra_used"] < budget["manual_extra_limit"]
        and budget["attempts"] < MAX_COMPLETED_AUDITS_PER_DAY + MAX_FAILED_MODEL_ATTEMPTS_PER_DAY
        + budget["manual_extra_limit"]
    )


def reserve_model_run(state: dict, now: datetime, manual_refresh: bool = False) -> None:
    if not can_start_model(state, now, manual_refresh):
        raise RuntimeError("daily model-attempt safety limit reached")
    budget = daily_budget(state, now)
    if manual_refresh and not can_start_model(state, now):
        budget["manual_extra_used"] += 1
    stored_budget = state.setdefault("model_budget", {})
    stored_budget.update(budget)
    stored_budget["attempts"] += 1
    save_state(state)  # Reserve before starting; failed starts can still incur usage.


def order_model_reviews(due: list[tuple[str, str, list[str]]], state: dict) -> list[tuple[str, str, list[str]]]:
    # Slots and dirty files may continually create new work. Rotate by the last
    # actual launch so a permanent backlog cannot starve a particular role.
    return sorted(
        (item for item in due if item[0] != "6"),
        key=lambda item: state["roles"][item[0]].get("last_model_run", ""),
    )


def due_roles(state: dict, hashes: dict[str, str], initial_changed: set[str], now: datetime) -> list[tuple[str, str, list[str]]]:
    due = []
    slot = current_slot(now)
    for role in ORDER:
        entry = state["roles"].setdefault(role, {})
        if role in PERIODIC:
            if not entry and now.hour < 9:
                continue
            if entry.get("completed_slot") == slot:
                continue
            key, selected = slot, []
        else:
            current = {name: digest for name, digest in hashes.items() if role_path(role, name)}
            if "last_seen" not in entry:
                pending = {path for path in initial_changed if role_path(role, path)}
                fresh = pending
            else:
                previous = entry["last_seen"]
                changed = {path for path in set(current) | set(previous) if current.get(path) != previous.get(path)}
                pending = set(entry.get("pending", [])) | changed
                fresh = changed
            entry["last_seen"] = current
            entry["pending"] = sorted(pending)
            if not pending:
                continue
            failed_hashes = entry.get("failed_hashes", {})
            eligible = {
                path for path in pending
                if failed_hashes.get(path) != current.get(path, "[deleted]")
            }
            if not eligible:
                continue
            selected = (sorted(fresh & eligible) + sorted(eligible - fresh))[:MAX_CHANGED_FILES_PER_RUN]
            key = selection_key(selected, current)
        if entry.get("deferred_key") == key:
            continue
        if entry.get("attempt_key") == key and entry.get("attempts", 0) >= MAX_ATTEMPTS:
            continue
        due.append((role, key, selected))
    return due


def run_role(role: str, selected: list[str]) -> str:
    selected_path = STATE_PATH.parent / f"role{role}" / "selected-paths.json"
    selected_path.parent.mkdir(parents=True, exist_ok=True)
    latest = selected_path.parent / "latest.md"
    if role == "6":
        latest.write_text(
            "AUDIT_STATUS: deferred\n\n"
            "攻撃UIとサービスの操作可能な縦切り、および無人実行で使えるブラウザ操作経路が未確認。"
            "画面を実際に操作していないため利用者体験を判定しない。\n"
            f"発動した変更: {', '.join(selected)}\n",
            encoding="utf-8",
        )
        return "deferred"
    selected_path.write_text(json.dumps(selected, ensure_ascii=False), encoding="utf-8")
    started = time.time()
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "RemoteSigned",
             "-File", str(RUNNER), "-Role", role, "-TimeoutMinutes", "45", "-BudgetReserved",
             "-ChangedPathsJson", str(selected_path)],
            cwd=ROOT, capture_output=True, text=True, timeout=46 * 60, check=False,
        )
        if result.returncode != 0 or not latest.exists() or latest.stat().st_mtime < started - 1:
            return "failed"
        first_line = latest.read_text(encoding="utf-8-sig").splitlines()[0]
        if first_line == "AUDIT_STATUS: complete":
            return "complete"
        if first_line == "AUDIT_STATUS: deferred":
            return "deferred"
        return "failed"
    except subprocess.TimeoutExpired:
        return "failed"
    finally:
        selected_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-roles", type=int, choices=range(1, MAX_ROLES_PER_TICK + 1), default=MAX_ROLES_PER_TICK)
    parser.add_argument("--refresh-role", choices=("4", "7"), help="one user-requested refresh; still subject to the hard daily start cap")
    args = parser.parse_args()
    now = datetime.now().astimezone()
    hashes = file_hashes()
    state = load_state()
    try:
        initial_changed = git_paths("diff", "--name-only", "HEAD") | git_paths(
            "ls-files", "--others", "--exclude-standard"
        )
    except subprocess.CalledProcessError:
        initial_changed = set(hashes)
    due = due_roles(state, hashes, initial_changed, now)
    if args.refresh_role:
        due = [item for item in due if item[0] == args.refresh_role]

    if args.dry_run:
        print(f"Slot: {current_slot(now)}; changed files from HEAD: {len(initial_changed)}")
        budget = daily_budget(state, now)
        print(f"Completed audits remaining today: {model_budget_remaining(state, now)}")
        print(f"Failed starts today: {budget['failed_attempts']}/{MAX_FAILED_MODEL_ATTEMPTS_PER_DAY}")
        print("Due: " + (", ".join(f"{role}({len(selected)})" for role, _, selected in due) or "none"))
        return

    save_state(state)
    failed_any = False
    free_reviews = [item for item in due if item[0] == "6"]
    planned = free_reviews + order_model_reviews(due, state)[:args.max_roles]
    for role, key, selected in planned:
        if role != "6":
            manual_refresh = args.refresh_role == role
            if not can_start_model(state, now, manual_refresh):
                print("Daily model safety limit reached; pending audits remain queued.")
                break
            state["roles"][role]["last_model_run"] = now.isoformat()
            reserve_model_run(state, now, manual_refresh)
        entry = state["roles"][role]
        attempts = entry.get("attempts", 0) + 1 if entry.get("attempt_key") == key else 1
        entry["attempt_key"] = key
        entry["attempts"] = attempts
        save_state(state)
        status = run_role(role, selected)
        print(f"Role {role}: {status}")
        failed_any = failed_any or status == "failed"
        if status == "complete":
            budget = state.setdefault("model_budget", {})
            budget.update(daily_budget(state, now))
            budget["completed"] += 1
            if role in PERIODIC:
                entry["completed_slot"] = key
            else:
                after = file_hashes()
                pending = set(entry["pending"])
                for path in selected:
                    if hashes.get(path) == after.get(path):
                        pending.discard(path)
                    entry.get("failed_hashes", {}).pop(path, None)
                entry["pending"] = sorted(pending)
            entry.pop("attempt_key", None)
            entry.pop("attempts", None)
            entry.pop("deferred_key", None)
        elif status == "deferred":
            entry["deferred_key"] = key
            entry.pop("attempt_key", None)
            entry.pop("attempts", None)
        elif status == "failed":
            budget = state.setdefault("model_budget", {})
            budget.update(daily_budget(state, now))
            budget["failed_attempts"] += 1
            if role not in PERIODIC:
                failed_hashes = entry.setdefault("failed_hashes", {})
                for path in selected:
                    failed_hashes[path] = hashes.get(path, "[deleted]")
        save_state(state)
    if failed_any:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
