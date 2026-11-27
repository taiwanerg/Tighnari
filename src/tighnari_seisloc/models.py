"""Internal physical traces; public calibrated three-axis packets remain compatible."""

from dataclasses import dataclass, field
import numpy as np
from .legacy.models import Station, WavePacket

__all__ = ["Station", "WavePacket", "Record"]


@dataclass
class Record:
    id: str
    lat: float
    lon: float
    start: float
    rate: float
    velocity: np.ndarray  # m/s, columns Z,N,E; unavailable columns are NaN
    elevation_m: float = 0
    provider: str = ""
    family: str = "BH"
    clipped: bool = False
    metadata: dict = field(default_factory=dict)

    @property
    def end(self):
        return self.start + len(self.velocity) / self.rate

    def station(self):
        return dict(
            id=self.id,
            name=self.id.split(".")[1],
            lat=self.lat,
            lon=self.lon,
            elevation_m=self.elevation_m,
            source=self.provider,
            end=self.end,
            clipped=self.clipped,
            component="Z",
            unit="µm/s",
        )
