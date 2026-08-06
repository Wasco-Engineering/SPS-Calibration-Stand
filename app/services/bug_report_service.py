"""Capture operator bug reports (screenshot, logs, context) to disk."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BugReportResult:
    """Filesystem location of a saved bug report."""

    report_dir: Path
    screenshot_path: Optional[Path]
    context_path: Path


def bug_reports_dir(log_dir: Path) -> Path:
    """Return ``<log_dir>/bug_reports`` (created on demand by callers)."""
    return Path(log_dir) / 'bug_reports'


def _latest_session_log(log_dir: Path) -> Optional[Path]:
    candidates = sorted(
        log_dir.glob('stinger_????????_??????.log'),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _git_sha(repo_root: Optional[Path]) -> Optional[str]:
    if repo_root is None:
        return None
    try:
        completed = subprocess.run(
            ['git', 'rev-parse', '--short', 'HEAD'],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if completed.returncode == 0:
            return completed.stdout.strip() or None
    except Exception as exc:
        logger.debug('Unable to resolve git SHA: %s', exc)
    return None


def create_bug_report(
    *,
    log_dir: Path,
    description: str,
    steps: str = '',
    context: Optional[dict[str, Any]] = None,
    screenshot_bytes: Optional[bytes] = None,
    repo_root: Optional[Path] = None,
) -> BugReportResult:
    """Write a timestamped bug report folder under ``logs/bug_reports/``.

    Copies the current session log (if present) and ``stinger.log``, saves an
    optional PNG screenshot, and writes ``context.json``.
    """
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    report_dir = bug_reports_dir(log_dir) / stamp
    report_dir.mkdir(parents=True, exist_ok=True)

    payload: dict[str, Any] = {
        'timestamp': stamp,
        'description': (description or '').strip(),
        'steps': (steps or '').strip(),
        'git_sha': _git_sha(repo_root),
    }
    if context:
        payload.update(context)

    context_path = report_dir / 'context.json'
    context_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding='utf-8',
    )

    session_log = _latest_session_log(log_dir)
    if session_log and session_log.is_file():
        shutil.copy2(session_log, report_dir / session_log.name)
    rotating = log_dir / 'stinger.log'
    if rotating.is_file():
        shutil.copy2(rotating, report_dir / 'stinger.log')

    screenshot_path: Optional[Path] = None
    if screenshot_bytes:
        screenshot_path = report_dir / 'screenshot.png'
        screenshot_path.write_bytes(screenshot_bytes)

    logger.info('Bug report saved to %s', report_dir)
    return BugReportResult(
        report_dir=report_dir,
        screenshot_path=screenshot_path,
        context_path=context_path,
    )
