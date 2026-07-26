from pydantic import BaseModel, ConfigDict, Field

from app.world_geometry import MAP_HEIGHT_TILES, MAP_WIDTH_TILES


class ResidentListItem(BaseModel):
    id: str
    slug: str
    name: str
    district: str
    status: str
    # Realism P0-5a: the API `heat` is the *display* value max(heat, pinned_heat)
    # (reads the model's display_heat property). Off = pinned_heat 0 → unchanged.
    heat: int = Field(validation_alias="display_heat")
    sprite_key: str
    sprite_url: str | None = None
    sprite_content_hash: str | None = None
    sprite_generation_run_id: str | None = None
    portrait_url: str | None = None
    tile_x: int
    tile_y: int
    home_location_id: str | None
    star_rating: int
    token_cost_per_turn: int
    meta_json: dict | None
    mood_label: str = "calm"

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class ResidentDetail(ResidentListItem):
    ability_md: str
    persona_md: str
    soul_md: str
    total_conversations: int
    avg_rating: float
    creator_id: str


class ResidentEditRequest(BaseModel):
    ability_md: str | None = None
    persona_md: str | None = None
    soul_md: str | None = None


class VersionSnapshot(BaseModel):
    version_number: int
    ability_md: str
    persona_md: str
    soul_md: str
    created_at: str


class ResidentImportResponse(BaseModel):
    id: str
    slug: str
    name: str
    district: str
    star_rating: int
    ability_md: str
    persona_md: str
    soul_md: str
    meta_json: dict | None


class PlayerPositionUpdate(BaseModel):
    """Request to update the current user's player position."""
    tile_x: int = Field(ge=0, lt=MAP_WIDTH_TILES)
    tile_y: int = Field(ge=0, lt=MAP_HEIGHT_TILES)
