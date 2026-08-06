from __future__ import annotations

import json

from app.services.bug_report_service import create_bug_report


def test_create_bug_report_captures_context_logs_and_screenshot(tmp_path) -> None:
    log_dir = tmp_path / 'logs'
    log_dir.mkdir()
    (log_dir / 'stinger_20260806_130000.log').write_text('session details', encoding='utf-8')
    (log_dir / 'stinger.log').write_text('rotating details', encoding='utf-8')

    result = create_bug_report(
        log_dir=log_dir,
        description='Pressure display drifted',
        steps='Load the work order and pressurize.',
        context={'equipment_id': 'CA-SPS-02', 'sequence': '401'},
        screenshot_bytes=b'png-bytes',
    )

    assert result.report_dir.is_dir()
    assert (result.report_dir / 'screenshot.png').read_bytes() == b'png-bytes'
    assert (result.report_dir / 'stinger.log').read_text(encoding='utf-8') == 'rotating details'
    assert (result.report_dir / 'stinger_20260806_130000.log').is_file()
    context = json.loads(result.context_path.read_text(encoding='utf-8'))
    assert context['description'] == 'Pressure display drifted'
    assert context['equipment_id'] == 'CA-SPS-02'
