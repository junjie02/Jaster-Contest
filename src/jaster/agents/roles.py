from __future__ import annotations

from pathlib import Path
from typing import Any

from jaster.domain import (
    ExecutorInput,
    BuilderInput,
    BuilderOutput,
    ReconInput,
    ReconOutput,
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
from jaster.runtime.llm import LLMError
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


class SkillRouterAgent(JsonAgent[SkillRouterInput, SkillRouterOutput]):
    role = "skill_router"
    input_model = SkillRouterInput
    output_model = SkillRouterOutput


class ExecutorAgent:
    role = "executor"

    def __init__(self, llm: OpenAIChatClient, prompts: PromptLibrary) -> None:
        self.llm = llm
        self.prompts = prompts
        self.last_trace: dict[str, object] | None = None

    def run(
        self,
        zone: str,
        payload: ExecutorInput,
        *,
        tool_name: str,
        tools: list[dict],
        retry_context: dict[str, object] | None = None,
    ) -> dict[str, object]:
        base_payload = payload.model_dump()
        attempts: list[dict[str, object]] = []
        current_retry_context = dict(retry_context or {})
        max_attempts = max(1, int(getattr(self.llm, "max_retries", 1) or 1))

        for attempt in range(1, max_attempts + 1):
            rendered_payload = dict(base_payload)
            if current_retry_context:
                rendered_payload["retry_context"] = current_retry_context
            payload_json = ExecutorInput.model_validate(
                {
                    key: rendered_payload[key]
                    for key in (
                        "target",
                        "function_name",
                        "function_summary",
                        "function_schema_text",
                        "function_definition_json",
                        "executor_brief",
                    )
                }
            ).model_dump_json(indent=2)
            prompt = "\n".join(
                [
                    f"当前区域：{zone}",
                    f"输入载荷：{payload_json}",
                    self.prompts._read("agents/executor.md"),
                    f"retry_context: {current_retry_context}" if current_retry_context else "",
                ]
            ).strip()
            attempt_trace: dict[str, object] = {
                "attempt": attempt,
                "payload": rendered_payload,
                "payload_json": payload_json,
                "prompt": prompt,
                "tool_name": tool_name,
                "tools": tools,
            }
            attempts.append(attempt_trace)
            try:
                response = self.llm.complete_tool_call(
                    system="你是一个严格的工具调用执行器。你只能调用指定函数一次，不要输出解释文本。",
                    prompt=prompt,
                    tools=tools,
                    tool_choice=tool_name,
                )
                attempt_trace["tool_call"] = response
                self.last_trace = {
                    "role": self.role,
                    "zone": zone,
                    "attempts": attempts,
                    "succeeded_attempt": attempt,
                }
                return response
            except Exception as exc:
                attempt_trace["error_type"] = type(exc).__name__
                attempt_trace["error_message"] = str(exc)
                current_retry_context = _build_executor_retry_context(
                    attempt=attempt,
                    max_attempts=max_attempts,
                    error=exc,
                    tool_name=tool_name,
                    attempt_trace=attempt_trace,
                )
                if attempt >= max_attempts:
                    self.last_trace = {
                        "role": self.role,
                        "zone": zone,
                        "attempts": attempts,
                        "succeeded_attempt": None,
                    }
                    raise
        raise RuntimeError("unreachable")


def _build_executor_retry_context(
    *,
    attempt: int,
    max_attempts: int,
    error: Exception,
    tool_name: str,
    attempt_trace: dict[str, object],
) -> dict[str, object]:
    previous_response_excerpt = ""
    failure_stage = "executor_tool_call"
    if isinstance(error, LLMError):
        failure_stage = f"executor_{error.stage}"
        previous_response_excerpt = error.raw_text.strip()
    tool_call = attempt_trace.get("tool_call")
    if not previous_response_excerpt and tool_call is not None:
        previous_response_excerpt = str(tool_call)
    return {
        "attempt": attempt,
        "max_attempts": max_attempts,
        "failure_stage": failure_stage,
        "error_type": type(error).__name__,
        "error_message": str(error),
        "tool_name": tool_name,
        "previous_response_excerpt": previous_response_excerpt[:600],
    }


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
        "recon": ReconAgent(llm, prompts),
        "strategy": StrategyAgent(llm, prompts),
        "reflection": ReflectionAgent(llm, prompts),
        "skill_router": SkillRouterAgent(llm, prompts),
        "executor": ExecutorAgent(llm, prompts),
        "builder": BuilderAgent(llm, prompts),
        "submission": SubmissionAgent(llm, prompts),
    }
