"""Central-server coding agent adapters, installer and registry."""

from forgemesh_control.agents.installer import OFFICIAL_SOURCES, AgentInstaller, detect_platform
from forgemesh_control.agents.registry import (
    AGENT_KINDS,
    AgentRegistryError,
    CentralAgentRegistry,
)

__all__ = [
    "AGENT_KINDS",
    "AgentInstaller",
    "AgentRegistryError",
    "CentralAgentRegistry",
    "OFFICIAL_SOURCES",
    "detect_platform",
]
