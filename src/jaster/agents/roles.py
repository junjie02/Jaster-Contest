from __future__ import annotations

from pathlib import Path
from typing import Any

from jaster.domain import (
    PlanInput,
    PlanOutput,
    ReflectionInput,
    ReflectionOutput,
    StrategyInput,
    StrategyOutput,
    SubmissionInput,
    SubmissionOutput,
)
from jaster.runtime.llm import OpenAIChatClient
from jaster.runtime.prompts import PromptLibrary

from .base import JsonAgent


class PlanAgent(JsonAgent[PlanInput, PlanOutput]):
    role = "plan"
    input_model = PlanInput
    output_model = PlanOutput


class StrategyAgent(JsonAgent[StrategyInput, StrategyOutput]):
    role = "strategy"
    input_model = StrategyInput
    output_model = StrategyOutput


class ReflectionAgent(JsonAgent[ReflectionInput, ReflectionOutput]):
    role = "reflection"
    input_model = ReflectionInput
    output_model = ReflectionOutput


class SubmissionAgent(JsonAgent[SubmissionInput, SubmissionOutput]):
    role = "submission"
    input_model = SubmissionInput
    output_model = SubmissionOutput


def build_agents(prompt_root: Path, llm: OpenAIChatClient) -> dict[str, Any]:
    prompts = PromptLibrary(prompt_root)
    return {
        "plan": PlanAgent(llm, prompts),
        "strategy": StrategyAgent(llm, prompts),
        "reflection": ReflectionAgent(llm, prompts),
        "submission": SubmissionAgent(llm, prompts),
    }
