"""
Pre-startup validation for NAVI: Python imports, disk assets, CUDA, and Ollama.

`run_startup_checks()` returns a structured `BootstrapReport` used by `navi.py`
and by `launcher.py` (via subprocess) to gate startup before marking the app online.
"""

import importlib.util
import os
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import requests
import torch

PROJECT_ROOT = Path(__file__).resolve().parent


@dataclass
class Diagnostic:
    name: str
    status: str
    critical: bool
    message: str
    details: dict[str, Any]
    remediation: str | None = None


@dataclass
class BootstrapReport:
    ok: bool
    diagnostics: list[Diagnostic]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "diagnostics": [asdict(d) for d in self.diagnostics],
        }

    def critical_failures(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.critical and d.status == "fail"]

    def format_for_console(self) -> str:
        lines = []
        for d in self.diagnostics:
            icon = "OK" if d.status == "pass" else "WARN" if d.status == "warn" else "FAIL"
            lines.append(f"[{icon}] {d.name}: {d.message}")
            if d.remediation and d.status in {"warn", "fail"}:
                lines.append(f"      remediation: {d.remediation}")
        return "\n".join(lines)


def _check_python_deps(required: list[str], optional: list[str]) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []

    missing_required = [m for m in required if importlib.util.find_spec(m) is None]
    if missing_required:
        diagnostics.append(
            Diagnostic(
                name="python_dependencies_required",
                status="fail",
                critical=True,
                message=f"Missing required Python modules: {', '.join(missing_required)}",
                details={"missing_required": missing_required},
                remediation="Install missing packages in the active venv, then retry startup.",
            )
        )
    else:
        diagnostics.append(
            Diagnostic(
                name="python_dependencies_required",
                status="pass",
                critical=True,
                message="All required Python modules are available.",
                details={"required_count": len(required)},
            )
        )

    missing_optional = [m for m in optional if importlib.util.find_spec(m) is None]
    if missing_optional:
        diagnostics.append(
            Diagnostic(
                name="python_dependencies_optional",
                status="warn",
                critical=False,
                message=f"Missing optional Python modules: {', '.join(missing_optional)}",
                details={"missing_optional": missing_optional},
                remediation="Install optional packages if you want those integrations enabled.",
            )
        )
    else:
        diagnostics.append(
            Diagnostic(
                name="python_dependencies_optional",
                status="pass",
                critical=False,
                message="All optional Python modules are available.",
                details={"optional_count": len(optional)},
            )
        )

    return diagnostics


def _check_assets(required_assets: list[str], optional_assets: list[str]) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    def _asset_exists(path_str: str) -> bool:
        candidate = Path(path_str)
        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / candidate
        return candidate.exists()

    missing_required = [p for p in required_assets if not _asset_exists(p)]
    missing_optional = [p for p in optional_assets if not _asset_exists(p)]

    diagnostics.append(
        Diagnostic(
            name="asset_files_required",
            status="fail" if missing_required else "pass",
            critical=True,
            message=(
                f"Missing required assets: {', '.join(missing_required)}"
                if missing_required
                else "All required asset files are present."
            ),
            details={"missing_required_assets": missing_required},
            remediation=(
                "Restore missing files in assets/ or update configured paths before startup."
                if missing_required
                else None
            ),
        )
    )

    diagnostics.append(
        Diagnostic(
            name="asset_files_optional",
            status="warn" if missing_optional else "pass",
            critical=False,
            message=(
                f"Missing optional assets: {', '.join(missing_optional)}"
                if missing_optional
                else "All optional asset files are present."
            ),
            details={"missing_optional_assets": missing_optional},
            remediation=(
                "Optional features may be disabled until these files are added."
                if missing_optional
                else None
            ),
        )
    )
    return diagnostics


def _check_cuda(required: bool) -> Diagnostic:
    available = bool(torch.cuda.is_available())
    details = {
        "cuda_available": available,
        "device_count": int(torch.cuda.device_count()) if available else 0,
        "torch_cuda_version": torch.version.cuda,
    }
    if required and not available:
        return Diagnostic(
            name="cuda_runtime",
            status="fail",
            critical=True,
            message="CUDA is unavailable but GPU execution is required.",
            details=details,
            remediation="Install compatible NVIDIA drivers + CUDA runtime and use a CUDA-enabled Torch build.",
        )
    return Diagnostic(
        name="cuda_runtime",
        status="pass" if available else "warn",
        critical=required,
        message="CUDA is available." if available else "CUDA is not available; running in degraded mode.",
        details=details,
        remediation=None if available else "Enable CUDA to restore expected performance.",
    )


def _check_ollama(ollama_url: str, model_name: str, timeout_seconds: float) -> list[Diagnostic]:
    """GET /api/tags on Ollama; verify the configured model name appears in the tag list."""
    diagnostics: list[Diagnostic] = []
    tags_url = ollama_url.rstrip("/") + "/api/tags"

    try:
        response = requests.get(tags_url, timeout=timeout_seconds)
        response.raise_for_status()
    except Exception as exc:
        diagnostics.append(
            Diagnostic(
                name="ollama_api_reachability",
                status="fail",
                critical=True,
                message=f"Ollama API is unreachable at {tags_url}.",
                details={"error": str(exc), "url": tags_url},
                remediation="Start Ollama (`ollama serve`) and ensure localhost networking is available.",
            )
        )
        diagnostics.append(
            Diagnostic(
                name="ollama_model_presence",
                status="fail",
                critical=True,
                message=f"Cannot verify model '{model_name}' because Ollama API is unavailable.",
                details={"model_name": model_name},
                remediation="Fix API reachability first, then install/pull the model.",
            )
        )
        return diagnostics

    diagnostics.append(
        Diagnostic(
            name="ollama_api_reachability",
            status="pass",
            critical=True,
            message=f"Ollama API reachable at {tags_url}.",
            details={"status_code": response.status_code, "url": tags_url},
        )
    )

    payload = response.json()
    models = payload.get("models", [])
    installed_names = [m.get("name", "") for m in models if isinstance(m, dict)]
    model_found = any(name == model_name or name.startswith(f"{model_name}:") for name in installed_names)
    diagnostics.append(
        Diagnostic(
            name="ollama_model_presence",
            status="pass" if model_found else "fail",
            critical=True,
            message=(
                f"Configured model '{model_name}' is installed."
                if model_found
                else f"Configured model '{model_name}' is not installed in Ollama."
            ),
            details={"model_name": model_name, "installed_models": installed_names},
            remediation=(
                None
                if model_found
                else f"Run `ollama pull {model_name}` and verify the configured tag matches the pulled model."
            ),
        )
    )
    return diagnostics


def run_startup_checks(
    *,
    model_name: str,
    ollama_base_url: str = "http://localhost:11434",
    require_cuda: bool = True,
    timeout_seconds: float = 3.0,
    required_modules: list[str] | None = None,
    optional_modules: list[str] | None = None,
    required_assets: list[str] | None = None,
    optional_assets: list[str] | None = None,
) -> BootstrapReport:
    """Run all checks with sensible defaults; callers may override module/asset lists for tests."""
    required_modules = required_modules or [
        "PyQt6",
        "faster_whisper",
        "TTS",
        "keyboard",
        "sounddevice",
        "scipy",
        "numpy",
        "requests",
        "googleapiclient",
        "google_auth_oauthlib",
        "openwakeword",
        "resemblyzer",
        "torch",
    ]
    optional_modules = optional_modules or [
        "langchain",
        "msal",
        "playwright",
    ]
    required_assets = required_assets or [
        "system_prompt.txt",
        "assets/navivis.gif",
    ]
    optional_assets = optional_assets or [
        "assets/voice_profile.npy",
        "assets/google_credentials.json",
        "assets/google_token.json",
    ]

    diagnostics: list[Diagnostic] = []
    diagnostics.extend(_check_python_deps(required_modules, optional_modules))
    diagnostics.extend(_check_assets(required_assets, optional_assets))
    diagnostics.append(_check_cuda(require_cuda))
    diagnostics.extend(_check_ollama(ollama_base_url, model_name, timeout_seconds))

    ok = not any(d.critical and d.status == "fail" for d in diagnostics)
    return BootstrapReport(ok=ok, diagnostics=diagnostics)
