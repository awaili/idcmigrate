"""Ingest adapters: ServiceNow CMDB, RVTools, Zabbix, Prometheus.

Importing this package registers all adapters with the base registry.
"""
from __future__ import annotations

from . import prometheus, rvtools, servicenow, zabbix  # noqa: F401  (register side-effect)
from .base import Adapter, IngestResult, all_sources, get_adapter
from ..models import SOURCE_PROMETHEUS, SOURCE_RVTOOLS, SOURCE_SERVICENOW, SOURCE_ZABBIX

__all__ = [
    "Adapter", "IngestResult", "all_sources", "get_adapter",
    "SOURCE_SERVICENOW", "SOURCE_RVTOOLS", "SOURCE_ZABBIX", "SOURCE_PROMETHEUS",
]