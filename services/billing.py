"""Resource Units: turn usage records into RU (PLATFORM.md, section 6).

Only Compute is metered today (wall time at ``invoke`` -> vCPU-hours, one
vCPU per service call); Memory, Storage and Network are structured but zero
until those meters land. Tariffs are internal and configurable, decoupled
from any public price -- change them without touching public rates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from services.models import UsageRecord

_MS_PER_HOUR = 1000.0 * 3600.0


@dataclass(frozen=True)
class Tariff:
    """Internal RU rates per resource unit (not a public price)."""

    c_cpu: float
    c_ram: float
    c_disk: float
    c_net: float


DEFAULT_TARIFF = Tariff(c_cpu=0.05, c_ram=0.01, c_disk=1.5e-6, c_net=0.02)
"""A sane starting point; override per deployment (RU_CPU, ...)."""


@dataclass(frozen=True)
class ResourceUnits:
    """An RU breakdown by resource, plus the total."""

    compute: float
    memory: float
    storage: float
    network: float
    total: float


def resource_units(
    records: Iterable[UsageRecord], tariff: Tariff
) -> ResourceUnits:
    """RU from usage: Compute from ms; other dimensions 0 until metered."""
    vcpu_hours = sum(record.ms for record in records) / _MS_PER_HOUR
    compute = vcpu_hours * tariff.c_cpu
    return ResourceUnits(
        compute=compute,
        memory=0.0,
        storage=0.0,
        network=0.0,
        total=compute,
    )
