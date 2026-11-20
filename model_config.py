"""Model assignments shared by the news council and personal briefing."""

from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, StringConstraints


ModelName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ModelConfig(BaseModel):
    main_agent: ModelName
    default_persona: ModelName
    council: dict[str, ModelName]
    voice_router: ModelName | None = None
    research_desk: ModelName | None = None

    def persona(self, name: str) -> str:
        return self.council.get(name, self.default_persona)


def load_models() -> ModelConfig:
    path = Path(__file__).with_name("models.yaml")
    try:
        return ModelConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (ValueError, yaml.YAMLError) as exc:
        raise ValueError(f"{path}: {exc}") from exc
