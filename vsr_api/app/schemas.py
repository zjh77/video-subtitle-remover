from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field, model_validator

Mode = Literal["sttn-auto", "sttn-det", "lama", "propainter", "opencv"]


class SubtitleArea(BaseModel):
    ymin: int = Field(ge=0); ymax: int = Field(ge=0); xmin: int = Field(ge=0); xmax: int = Field(ge=0)
    @model_validator(mode="after")
    def ordered(self):
        if self.ymin >= self.ymax or self.xmin >= self.xmax: raise ValueError("area bounds must have positive size")
        return self


class JobCreate(BaseModel):
    input_asset_id: str
    subtitle_areas: list[SubtitleArea] = Field(min_length=1)
    inpaint_mode: Mode = "sttn-auto"
