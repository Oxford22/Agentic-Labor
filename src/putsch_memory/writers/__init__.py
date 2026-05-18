"""Per-crew typed episode writers.

Each crew has its own writer module. They share the base class in
`base.py` so the provenance + idempotency + redaction story is enforced
in one place and not repeated per crew.
"""

from putsch_memory.writers.ap_episode import APEpisode, APEpisodeWriter
from putsch_memory.writers.base import EpisodeWriter
from putsch_memory.writers.mahnverfahren_episode import (
    MahnverfahrenEpisode,
    MahnverfahrenEpisodeWriter,
)
from putsch_memory.writers.stammdaten_episode import (
    StammdatenEpisode,
    StammdatenEpisodeWriter,
)
from putsch_memory.writers.zoll_episode import ZollEpisode, ZollEpisodeWriter

__all__ = [
    "APEpisode",
    "APEpisodeWriter",
    "EpisodeWriter",
    "MahnverfahrenEpisode",
    "MahnverfahrenEpisodeWriter",
    "StammdatenEpisode",
    "StammdatenEpisodeWriter",
    "ZollEpisode",
    "ZollEpisodeWriter",
]
