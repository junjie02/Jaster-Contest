from __future__ import annotations

from pathlib import Path

from jaster.domain import (
    BuilderInput,
    BuilderOutput,
    ReconInput,
    ReconOutput,
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


class ReconAgent(JsonAgent[ReconInput, ReconOutput]):
    role = "recon"
    input_model = ReconInput
    output_model = ReconOutput


class StrategyAgent(JsonAgent[StrategyInput, StrategyOutput]):
    role = "strategy"
    input_model = StrategyInput
    output_model = StrategyOutput


class ReflectionAgent(JsonAgent[ReflectionInput, ReflectionOutput]):
    role = "reflection"
    input_model = ReflectionInput
    output_model = ReflectionOutput


class BuilderAgent(JsonAgent[BuilderInput, BuilderOutput]):
    role = "builder"
    input_model = BuilderInput
    output_model = BuilderOutput


class SubmissionAgent(JsonAgent[SubmissionInput, SubmissionOutput]):
    role = "submission"
    input_model = SubmissionInput
    output_model = SubmissionOutput


def build_agents(prompt_root: Path, llm: OpenAIChatClient) -> dict[str, JsonAgent]:
    prompts = PromptLibrary(prompt_root)
    return {
        "recon": ReconAgent(llm, prompts, prompt_root),
        "strategy": StrategyAgent(llm, prompts, prompt_root),
        "reflection": ReflectionAgent(llm, prompts, prompt_root),
        "builder": BuilderAgent(llm, prompts, prompt_root),
        "submission": SubmissionAgent(llm, prompts, prompt_root),
    }

