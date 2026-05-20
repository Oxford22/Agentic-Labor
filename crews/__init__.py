"""Crews: units of agentic work with a uniform interface.

A Crew is a callable that accepts a task plus optional context and returns
a structured result. The NodeAdapter wraps a Crew so it can sit inside a
larger LangGraph (or a simple Pipeline) and enforces the trust boundary:
upstream crew outputs become <external_content> data before the next crew
sees them.

Public surface:
  - Crew, CrewOutput   : the uniform interface
  - NodeAdapter        : trust-aware wrapper used as a graph node
  - SwarmCrew          : the Magentic-One swarm exposed as a Crew
  - StammdatenCrew     : vendor/material master-data lookup (stub)
"""

from .base import Crew, CrewOutput, NodeAdapter
from .stammdaten import StammdatenCrew
from .swarm_crew import SwarmCrew

__all__ = [
    "Crew",
    "CrewOutput",
    "NodeAdapter",
    "SwarmCrew",
    "StammdatenCrew",
]
