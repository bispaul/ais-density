from datetime import date
from pathlib import Path
from typing import Annotated, Self

import yaml
from pydantic import BaseModel, Field, model_validator

H3Resolution = Annotated[int, Field(ge=0, le=15)]
Longitude = Annotated[float, Field(ge=-180.0, le=180.0)]
Latitude = Annotated[float, Field(ge=-90.0, le=90.0)]


class Window(BaseModel):
    model_config = {"extra": "forbid"}

    start: date
    end: date

    @model_validator(mode="after")
    def _check_order(self) -> Self:
        if self.end < self.start:
            raise ValueError(f"end {self.end} precedes start {self.start}")
        return self


class H3Config(BaseModel):
    model_config = {"extra": "forbid"}

    resolution: H3Resolution


class ClassifyConfig(BaseModel):
    model_config = {"extra": "forbid"}

    # Quantile *level*; the vessel-count gate is computed at runtime from the
    # input's own distribution.
    activity_quantile: Annotated[float, Field(gt=0.0, lt=1.0)]
    anchor_sog_max: float
    lane_sog_min: float


class Region(BaseModel):
    model_config = {"extra": "forbid"}

    bbox: tuple[Longitude, Latitude, Longitude, Latitude]
    h3: H3Config | None = None

    @model_validator(mode="after")
    def _check_bbox(self) -> Self:
        min_lon, min_lat, max_lon, max_lat = self.bbox
        if min_lon >= max_lon:
            raise ValueError(f"bbox min_lon {min_lon} >= max_lon {max_lon}")
        if min_lat >= max_lat:
            raise ValueError(f"bbox min_lat {min_lat} >= max_lat {max_lat}")
        return self


class Config(BaseModel):
    model_config = {"extra": "forbid"}

    windows: dict[str, Window]
    h3: H3Config
    regions: dict[str, Region]
    classify: ClassifyConfig

    def resolution_for(self, region: str) -> int:
        """H3 resolution for a region, honoring a per-region override."""
        override = self.regions[region].h3
        return override.resolution if override else self.h3.resolution


def load_config(path: str | Path = "config.yaml") -> Config:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return Config.model_validate(raw)
