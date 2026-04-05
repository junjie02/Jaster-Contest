from __future__ import annotations

import json
from pathlib import Path
from typing import Generic, TypeVar

from pydantic import BaseModel

from jaster.runtime.llm import OpenAIChatClient
from jaster.runtime.prompts import PromptLibrary

InputModel = TypeVar("InputModel", bound=BaseModel)
OutputModel = TypeVar("OutputModel", bound=BaseModel)

STRICT_JSON_SYSTEM = (
    "You are a strict JSON generator. Follow the role instructions and return exactly one JSON object."
)


class JsonAgent(Generic[InputModel, OutputModel]):
    role: str
    input_model: type[InputModel]
    output_model: type[OutputModel]

    def __init__(self, llm: OpenAIChatClient, prompts: PromptLibrary, prompt_root: Path) -> None:
        self.llm = llm
        self.prompts = prompts
        self.prompt_root = prompt_root

    def run(self, zone: str, payload: InputModel) -> OutputModel:
        prompt = self.prompts.render(
            self.role,
            zone=zone,
            payload_json=json.dumps(payload.model_dump(), ensure_ascii=False, indent=2),
        )
        response = self.llm.complete_json(system=STRICT_JSON_SYSTEM, prompt=prompt)
        return self.output_model.model_validate(response)

