"""
Backup даних команди (tasks.json/ideas.json) — раз на добу комітить
їхню копію в окрему гілку `backup` того самого репозиторію (не в
main, щоб не змішувати дані з кодом).

Ці файли — у .gitignore і живуть ЛИШЕ на хості, де запущено bot.py
(ніколи не потрапляють у звичайні коміти/GitHub Actions), тому це не
може бути GitHub Action (раннер туди не має доступу) — лише локальний
скрипт на тому самому хості, за тим самим принципом, що watchdog.py.

Свідомо НЕ робить `git checkout backup` у робочій директорії бота —
bot.py живий процес, що постійно читає/пише файли з диска (tasks.json,
PRODUCT.md, design.html тощо); перемикання гілки просто під ним було б
ризиковано. Замість цього — окремий тимчасовий `git worktree`
(потребує Git 2.42+ для `--orphan` при першому запуску).

Запуск (рекомендовано — системний cron, раз на добу):
    0 3 * * * cd /path/to/repo && /path/to/venv/bin/python backup.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).parent
BACKUP_BRANCH = "backup"
FILES_TO_BACKUP = ["tasks.json", "ideas.json"]


def run(cmd: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=check)


def _branch_exists(name: str) -> bool:
    return run(["git", "rev-parse", "--verify", name], REPO_ROOT, check=False).returncode == 0


def main() -> int:
    existing = [f for f in FILES_TO_BACKUP if (REPO_ROOT / f).exists()]
    if not existing:
        print("backup: tasks.json/ideas.json відсутні, немає що бекапити")
        return 0

    with tempfile.TemporaryDirectory(prefix="backup-worktree-") as tmp:
        worktree = Path(tmp) / "wt"

        if _branch_exists(BACKUP_BRANCH):
            add = run(["git", "worktree", "add", str(worktree), BACKUP_BRANCH], REPO_ROOT, check=False)
        else:
            add = run(["git", "worktree", "add", "--orphan", "-b", BACKUP_BRANCH, str(worktree)], REPO_ROOT, check=False)

        if add.returncode != 0:
            print(f"backup: не вдалось створити worktree: {add.stderr.strip()}", file=sys.stderr)
            return 1

        try:
            for f in existing:
                shutil.copy2(REPO_ROOT / f, worktree / f)

            run(["git", "add", *existing], worktree)
            status = run(["git", "status", "--porcelain"], worktree)
            if not status.stdout.strip():
                print("backup: без змін від попереднього бекапу, коміт не потрібен")
                return 0

            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            run(["git", "commit", "-m", f"backup: {timestamp}"], worktree)

            push = run(["git", "push", "origin", BACKUP_BRANCH], worktree, check=False)
            if push.returncode != 0:
                print(f"backup: коміт створено локально, але push не вдався: {push.stderr.strip()}", file=sys.stderr)
                return 1

            print(f"backup: закомічено й запушено в {BACKUP_BRANCH} ({', '.join(existing)})")
            return 0
        finally:
            run(["git", "worktree", "remove", "--force", str(worktree)], REPO_ROOT, check=False)


if __name__ == "__main__":
    raise SystemExit(main())
