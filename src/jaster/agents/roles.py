from __future__ import annotations

from pathlib import Path
from typing import Any

from jaster.domain import (
    BuilderInput,
    BuilderOutput,
    ReflectionInput,
    ReflectionOutput,
    SkillRouterInput,
    SkillRouterOutput,
    StrategyInput,
    StrategyOutput,
    SubmissionInput,
    SubmissionOutput,
)
from jaster.runtime.llm import OpenAIChatClient
from jaster.runtime.prompts import PromptLibrary

from .base import JsonAgent


class StrategyAgent(JsonAgent[StrategyInput, StrategyOutput]):
    role = "strategy"
    input_model = StrategyInput
    output_model = StrategyOutput


class ReflectionAgent(JsonAgent[ReflectionInput, ReflectionOutput]):
    role = "reflection"
    input_model = ReflectionInput
    output_model = ReflectionOutput


class SkillRouterAgent(JsonAgent[SkillRouterInput, SkillRouterOutput]):
    role = "skill_router"
    input_model = SkillRouterInput
    output_model = SkillRouterOutput


class BuilderAgent(JsonAgent[BuilderInput, BuilderOutput]):
    role = "builder"
    input_model = BuilderInput
    output_model = BuilderOutput


class SubmissionAgent(JsonAgent[SubmissionInput, SubmissionOutput]):
    role = "submission"
    input_model = SubmissionInput
    output_model = SubmissionOutput


def build_agents(prompt_root: Path, llm: OpenAIChatClient) -> dict[str, Any]:
    prompts = PromptLibrary(prompt_root)
    return {
        "strategy": StrategyAgent(llm, prompts),
        "reflection": ReflectionAgent(llm, prompts),
        "skill_router": SkillRouterAgent(llm, prompts),
        "builder": BuilderAgent(llm, prompts),
        "submission": SubmissionAgent(llm, prompts),
    }
