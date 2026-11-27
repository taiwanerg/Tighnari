from typing import Literal
from pydantic import BaseModel, Field, model_validator
import math


class Station(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(max_length=200)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    source: str = Field(max_length=40)
    elevation_m: float = Field(default=0, ge=-500, le=5000)


class WavePacket(BaseModel):
    station: Station
    start: float = Field(gt=0, allow_inf_nan=False)
    rate: float = Field(ge=20, le=500, allow_inf_nan=False)
    # Calibrated, time-aligned geographic Z,N,E acceleration. Never raw counts.
    unit: Literal["cm/s2"] = "cm/s2"
    samples: list[list[float]] = Field(min_length=1, max_length=30000)

    @model_validator(mode="after")
    def validate_samples(self):
        if len(self.samples) > self.rate * 60:
            raise ValueError("每批最多 60 秒")
        if any(len(row) != 3 or any(not math.isfinite(v) or abs(v) > 20000 for v in row)
               for row in self.samples):
            raise ValueError("samples 必須是有限的三軸 Z,N,E 加速度")
        return self
