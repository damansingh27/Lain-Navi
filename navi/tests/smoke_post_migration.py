"""
Post-migration smoke checks: bootstrap JSON, Ollama tags, CUDA/device report, overlay
state transitions, calendar read, and local Outlook COM sanity.

Run: `python tests/smoke_post_migration.py` from repo root (script chdirs to root).
Exit code non-zero if any *critical* check fails; prints JSON + human summary.
"""

import json
import os
import sys
import traceback
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


@dataclass
class CheckResult:
    name: str
    status: str
    critical: bool
    message: str
    details: dict[str, Any]


def _result(name: str, status: str, critical: bool, message: str, details: dict[str, Any] | None = None):
    return CheckResult(
        name=name,
        status=status,
        critical=critical,
        message=message,
        details=details or {},
    )


def check_bootstrap_health() -> CheckResult:
    from runtime_bootstrap import run_startup_checks

    model = os.getenv("OLLAMA_MODEL", "dolphin3")
    base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    report = run_startup_checks(model_name=model, ollama_base_url=base_url, require_cuda=True)
    critical_failures = [
        d.name for d in report.diagnostics if d.critical and d.status == "fail"
    ]
    if report.ok:
        return _result(
            "bootstrap_health",
            "pass",
            True,
            "Bootstrap checks passed.",
            {"model": model, "base_url": base_url},
        )
    return _result(
        "bootstrap_health",
        "fail",
        True,
        "Bootstrap checks reported critical failures.",
        {"critical_failures": critical_failures, "report": report.to_dict()},
    )


def check_ollama_model_readiness() -> CheckResult:
    from runtime_bootstrap import run_startup_checks

    model = os.getenv("OLLAMA_MODEL", "dolphin3")
    base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    report = run_startup_checks(model_name=model, ollama_base_url=base_url, require_cuda=False)
    relevant = {
        d.name: d.status
        for d in report.diagnostics
        if d.name in {"ollama_api_reachability", "ollama_model_presence"}
    }
    if relevant.get("ollama_api_reachability") == "pass" and relevant.get("ollama_model_presence") == "pass":
        return _result(
            "ollama_model_readiness",
            "pass",
            True,
            f"Ollama API reachable and model '{model}' is present.",
            {"model": model, "base_url": base_url},
        )
    return _result(
        "ollama_model_readiness",
        "fail",
        True,
        "Ollama API/model readiness failed.",
        {"model": model, "base_url": base_url, "diagnostics": report.to_dict()},
    )


def check_cuda_report() -> CheckResult:
    from device_manager import DeviceManager, DeviceSelectionError

    dm = DeviceManager()
    try:
        dm.select_for_component(
            component="whisper",
            cuda_compute_type="float16",
            cpu_compute_type="int8",
            fallback_policy="allow_cpu_fallback",
        )
        dm.select_for_component(
            component="xtts",
            cuda_compute_type=None,
            cpu_compute_type=None,
            fallback_policy="require_cuda",
        )
    except DeviceSelectionError as exc:
        return _result(
            "cuda_report",
            "fail",
            True,
            f"Device selection failed: {exc}",
            {"report": dm.build_report().to_dict()},
        )

    report = dm.build_report().to_dict()
    if not report["cuda_available"]:
        return _result(
            "cuda_report",
            "fail",
            True,
            "CUDA unavailable while XTTS requires CUDA.",
            {"report": report},
        )
    return _result(
        "cuda_report",
        "pass",
        True,
        "CUDA report looks healthy.",
        {"report": report},
    )


def check_overlay_state_progression() -> CheckResult:
    from PyQt6.QtWidgets import QApplication
    from overlay import NAVIOverlay, OverlayState

    app = QApplication.instance() or QApplication([])
    overlay = NAVIOverlay()
    observed = [overlay.state.value]

    sequence = [
        OverlayState.LISTENING,
        OverlayState.PROCESSING,
        OverlayState.SPEAKING,
        OverlayState.IDLE,
    ]
    for state in sequence:
        overlay.set_state(state, "smoke_test")
        app.processEvents()
        observed.append(overlay.state.value)

    expected = ["IDLE", "LISTENING", "PROCESSING", "SPEAKING", "IDLE"]
    if observed == expected:
        return _result(
            "overlay_state_progression",
            "pass",
            True,
            "Overlay state progression succeeded.",
            {"observed": observed, "expected": expected},
        )
    return _result(
        "overlay_state_progression",
        "fail",
        True,
        "Overlay state progression mismatch.",
        {"observed": observed, "expected": expected},
    )


def check_calendar_tools() -> CheckResult:
    from calendar_tool import get_events_today, check_freebusy

    now = datetime.now()
    start = now.isoformat(timespec="minutes")
    end = (now + timedelta(hours=4)).isoformat(timespec="minutes")

    today_result = get_events_today()
    freebusy_result = check_freebusy(start, end)
    ok = isinstance(today_result, str) and isinstance(freebusy_result, dict) and freebusy_result.get("status") == "ok"
    if ok:
        return _result(
            "calendar_tools",
            "pass",
            True,
            "Calendar read + freebusy checks passed.",
            {
                "events_today_preview": today_result[:250],
                "freebusy_count": freebusy_result.get("busy_count"),
            },
        )
    return _result(
        "calendar_tools",
        "fail",
        True,
        "Calendar checks failed.",
        {"events_today_type": str(type(today_result)), "freebusy": freebusy_result},
    )


def check_outlook_local_tools() -> CheckResult:
    from outlook_tool import get_recent_emails

    recent = get_recent_emails(1)
    if isinstance(recent, str) and recent:
        if recent.startswith("Could not connect to Outlook desktop app"):
            return _result(
                "outlook_local_tools",
                "fail",
                True,
                "Local Outlook mail check failed.",
                {"response": recent[:300]},
            )
        return _result(
            "outlook_local_tools",
            "pass",
            True,
            "Local Outlook mail tool check passed.",
            {"recent_preview": recent[:300]},
        )
    return _result(
        "outlook_local_tools",
        "fail",
        True,
        "Local Outlook mail check returned unexpected output.",
        {"output_type": str(type(recent))},
    )


def _run_check(fn, name: str) -> CheckResult:
    try:
        return fn()
    except Exception as exc:
        return _result(
            name,
            "fail",
            True,
            f"Unhandled exception: {exc}",
            {"traceback": traceback.format_exc(limit=8)},
        )


def main() -> int:
    checks = [
        ("bootstrap_health", check_bootstrap_health),
        ("ollama_model_readiness", check_ollama_model_readiness),
        ("cuda_report", check_cuda_report),
        ("overlay_state_progression", check_overlay_state_progression),
        ("calendar_tools", check_calendar_tools),
        ("outlook_local_tools", check_outlook_local_tools),
    ]
    results = [_run_check(fn, name) for name, fn in checks]

    critical_failures = [r for r in results if r.critical and r.status == "fail"]
    passed = len([r for r in results if r.status == "pass"])
    failed = len([r for r in results if r.status == "fail"])
    warned = len([r for r in results if r.status == "warn"])

    payload = {
        "timestamp": datetime.now().isoformat(),
        "summary": {
            "total": len(results),
            "passed": passed,
            "failed": failed,
            "warned": warned,
            "critical_failures": len(critical_failures),
            "ready": len(critical_failures) == 0,
        },
        "results": [asdict(r) for r in results],
    }

    print("=== NAVI Post-Migration Smoke Summary ===")
    for r in results:
        icon = "PASS" if r.status == "pass" else "WARN" if r.status == "warn" else "FAIL"
        level = "CRIT" if r.critical else "INFO"
        print(f"[{icon}][{level}] {r.name}: {r.message}")
    print(
        f"Totals: passed={passed} failed={failed} warned={warned} "
        f"critical_failures={len(critical_failures)}"
    )
    print("=== JSON ===")
    print(json.dumps(payload, indent=2))

    return 1 if critical_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
