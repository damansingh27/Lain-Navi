"""
Central place to pick `cuda` vs `cpu` for heavy models (Whisper, XTTS).

NAVI constructs one `DeviceManager`, calls `select_for_component()` per subsystem,
then `build_report()` for console/bootstrap-style diagnostics.
"""

from dataclasses import dataclass, asdict
from typing import Any

import torch


@dataclass
class ComponentDeviceSelection:
    component: str
    device: str
    compute_type: str | None
    used_fallback: bool
    policy: str
    status: str
    message: str


@dataclass
class DeviceRuntimeReport:
    cuda_available: bool
    device_count: int
    torch_cuda_version: str | None
    selections: dict[str, ComponentDeviceSelection]

    def to_dict(self) -> dict[str, Any]:
        return {
            "cuda_available": self.cuda_available,
            "device_count": self.device_count,
            "torch_cuda_version": self.torch_cuda_version,
            "selections": {k: asdict(v) for k, v in self.selections.items()},
        }


class DeviceSelectionError(RuntimeError):
    pass


class DeviceManager:
    """
    Selects runtime devices per component using explicit fallback policy.

    Supported policies:
    - "require_cuda": fail if CUDA is unavailable.
    - "allow_cpu_fallback": use CPU when CUDA is unavailable.
    """

    def __init__(self) -> None:
        self.cuda_available = bool(torch.cuda.is_available())
        self.device_count = int(torch.cuda.device_count()) if self.cuda_available else 0
        self.torch_cuda_version = torch.version.cuda
        self._selections: dict[str, ComponentDeviceSelection] = {}

    def select_for_component(
        self,
        *,
        component: str,
        cuda_compute_type: str | None,
        cpu_compute_type: str | None,
        fallback_policy: str,
    ) -> ComponentDeviceSelection:
        if fallback_policy not in {"require_cuda", "allow_cpu_fallback"}:
            raise ValueError(f"Unsupported fallback policy: {fallback_policy}")

        if self.cuda_available:
            selection = ComponentDeviceSelection(
                component=component,
                device="cuda",
                compute_type=cuda_compute_type,
                used_fallback=False,
                policy=fallback_policy,
                status="pass",
                message=f"{component} configured for CUDA ({cuda_compute_type or 'default'}).",
            )
            self._selections[component] = selection
            return selection

        if fallback_policy == "allow_cpu_fallback":
            selection = ComponentDeviceSelection(
                component=component,
                device="cpu",
                compute_type=cpu_compute_type,
                used_fallback=True,
                policy=fallback_policy,
                status="warn",
                message=f"{component} running on CPU fallback ({cpu_compute_type or 'default'}).",
            )
            self._selections[component] = selection
            return selection

        selection = ComponentDeviceSelection(
            component=component,
            device="unassigned",
            compute_type=None,
            used_fallback=False,
            policy=fallback_policy,
            status="fail",
            message=f"{component} requires CUDA but CUDA is unavailable.",
        )
        self._selections[component] = selection
        raise DeviceSelectionError(selection.message)

    def build_report(self) -> DeviceRuntimeReport:
        return DeviceRuntimeReport(
            cuda_available=self.cuda_available,
            device_count=self.device_count,
            torch_cuda_version=self.torch_cuda_version,
            selections=dict(self._selections),
        )

