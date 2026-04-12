from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class TaskStatus(str, Enum):
    in_progress = "in_progress"
    completed = "completed"
    failed = "failed"


class ArtifactRef(BaseModel):
    kind: str
    path: str
    producer_phase: str = ""
    producer_task_key: str = ""
    producer_action_id: str = ""
    producer_tool_name: str = ""
    producer_success: bool | None = None


class Observation(BaseModel):
    cycle: int = 0
    strategy_round: int = 0
    task_key: str = ""
    task_title: str = ""
    action_task_id: str = ""
    tool_name: str = ""
    target: str = ""
    result: str = ""
    key_findings: str = ""


class RecentObservationAction(BaseModel):
    action_task_id: str = ""
    tool_name: str = ""
    target: str = ""
    result: str = ""
    key_findings: str = ""


class RecentObservationRound(BaseModel):
    round: int = 0
    actions: list[RecentObservationAction] = Field(default_factory=list)


class ExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool
    batch_status: str = ""
    summary: str = ""
    findings: list[str] = Field(default_factory=list)
    flag_candidates: list[str] = Field(default_factory=list)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    command: str = ""
    script_path: str = ""
    source: str = ""
    failure_stage: str = ""
    task_results: dict[str, "TaskExecutionResult"] = Field(default_factory=dict)


class LatestExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool
    batch_status: str = ""
    summary: str = ""
    findings: list[str] = Field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    command: str = ""
    source: str = ""
    failure_stage: str = ""
    task_results: dict[str, "TaskExecutionResult"] = Field(default_factory=dict)


class TaskExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    kind: Literal["tool", "finish"]
    tool_name: str | None = None
    success: bool
    summary: str = ""
    findings: list[str] = Field(default_factory=list)
    flag_candidates: list[str] = Field(default_factory=list)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    command: str = ""
    script_path: str = ""
    source: str = ""
    failure_stage: str = ""


class ObservedTaskResult(BaseModel):
    task_id: str
    target: str = ""
    result: str = ""
    key_findings: str = ""


class AvailableTool(BaseModel):
    name: str
    summary: str = ""
    server_name: str = ""
    tool_schema_text: str = ""
    tool_definition_json: str = ""


class TaskNodeSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    parent_key: str = ""
    title: str
    reason: str = ""
    completion_criteria: str = ""
    status: TaskStatus = TaskStatus.in_progress
    latest_summary: str = ""
    latest_findings: list[str] = Field(default_factory=list)
    attempt_count: int = 0


class TaskNodePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_key: str
    title: str
    reason: str = ""
    completion_criteria: str = ""
    status: TaskStatus = TaskStatus.in_progress
    latest_summary: str = ""
    latest_findings: list[str] = Field(default_factory=list)
    attempt_count: int = 0


class TaskNodeUpdatePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    title: str | None = None
    reason: str | None = None
    completion_criteria: str | None = None
    status: TaskStatus | None = None
    latest_summary: str | None = None
    latest_findings: list[str] | None = None
    attempt_count: int | None = None


class TaskTreePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    add_nodes: list[TaskNodePatch] = Field(default_factory=list)
    update_nodes: list[TaskNodeUpdatePatch] = Field(default_factory=list)


class TaskTreeSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[TaskNodeSnapshot] = Field(default_factory=list)


class TaskDiscovery(BaseModel):
    cycle: int = 0
    task_key: str = ""
    task_title: str = ""
    source: str = ""
    summary: str = ""
    findings: list[str] = Field(default_factory=list)
    flag_candidates: list[str] = Field(default_factory=list)
    credentials: list[str] = Field(default_factory=list)


class FailurePattern(BaseModel):
    pattern: str
    reason: str = ""
    affected_task_keys: list[str] = Field(default_factory=list)


class StrategicRejection(BaseModel):
    label: str
    reason: str = ""


class PlanningThought(BaseModel):
    analysis: str = ""
    failure_diagnosis: str = ""
    decomposition: str = ""
    dispatch_rationale: str = ""


class PlanningAttempt(BaseModel):
    cycle: int
    phase_summary: str = ""
    planner_notes: str = ""
    added_task_titles: list[str] = Field(default_factory=list)
    continued_task_keys: list[str] = Field(default_factory=list)


class PlannerContext(BaseModel):
    initial_objective: str = ""
    target: str = ""
    planning_attempts: list[PlanningAttempt] = Field(default_factory=list)
    rejected_strategies: dict[str, str] = Field(default_factory=dict)
    long_term_objectives: list[str] = Field(default_factory=list)
    latest_reflection_digest: str = ""
    compressed_history_summary: str = ""
    compression_count: int = 0


class PlannerHistoryEntry(BaseModel):
    cycle: int
    summary: str = ""
    planner_notes: str = ""
    dispatched_task_keys: list[str] = Field(default_factory=list)
    added_task_titles: list[str] = Field(default_factory=list)
    continued_task_keys: list[str] = Field(default_factory=list)
    planning_thought: PlanningThought | None = None


class ActionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = ""
    kind: Literal["tool", "finish"]
    goal: str
    expected_result: str = ""
    tool_name: str | None = None
    tool_args: dict[str, Any] = Field(default_factory=dict)


class StrategyTaskResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_key: str
    task_title: str
    completed: bool = False
    rounds_used: int = 0
    termination_reason: str = ""
    phase_summary: str = ""
    task_summary: str = ""
    task_findings: list[str] = Field(default_factory=list)
    flag_candidates: list[str] = Field(default_factory=list)
    credentials: list[str] = Field(default_factory=list)
    latest_execution: LatestExecutionResult | None = None
    observed_task_results: list[ObservedTaskResult] = Field(default_factory=list)
    artifacts: list[ArtifactRef] = Field(default_factory=list)


class ReflectionTaskUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    status: TaskStatus
    latest_summary: str = ""
    latest_findings: list[str] = Field(default_factory=list)
    reason: str = ""


class ReflectionHistoryEntry(BaseModel):
    cycle: int
    summary: str = ""
    planner_guidance: str = ""
    task_updates: list[ReflectionTaskUpdate] = Field(default_factory=list)
    failure_patterns: list[FailurePattern] = Field(default_factory=list)
    strategic_rejections: list[StrategicRejection] = Field(default_factory=list)
    critical_findings: list[str] = Field(default_factory=list)


class PlanInput(BaseModel):
    objective: str
    task_tree: TaskTreeSnapshot
    challenge_context: str = ""
    bootstrap_execution: LatestExecutionResult | None = None
    planner_context: PlannerContext | None = None
    task_status_summary: str = ""
    failure_patterns_summary: str = ""
    reflection_history: list[ReflectionHistoryEntry] = Field(default_factory=list)
    latest_discoveries: list[TaskDiscovery] = Field(default_factory=list)
    available_artifacts: list[ArtifactRef] = Field(default_factory=list)


class PlanOutput(BaseModel):
    phase_summary: str
    planner_notes: str = ""
    planning_thought: PlanningThought | None = None
    tree_patch: TaskTreePatch = Field(default_factory=TaskTreePatch)
    dispatch_task_keys: list[str] = Field(default_factory=list)


class StrategyInput(BaseModel):
    objective: str
    assigned_task: TaskNodeSnapshot
    task_tree: TaskTreeSnapshot
    challenge_context: str = ""
    recent_observations: list[RecentObservationRound] = Field(default_factory=list)
    latest_execution: LatestExecutionResult | None = None
    reflection_history: list[ReflectionHistoryEntry] = Field(default_factory=list)
    available_artifacts: list[ArtifactRef] = Field(default_factory=list)
    available_tools: list[AvailableTool] = Field(default_factory=list)


class StrategyOutput(BaseModel):
    phase_summary: str
    is_complete: bool = False
    task_summary: str = ""
    task_findings: list[str] = Field(default_factory=list)
    actions: list[ActionPlan] = Field(default_factory=list)
    flag_candidates: list[str] = Field(default_factory=list)
    observed_task_results: list[ObservedTaskResult] = Field(default_factory=list)
    credentials: list[str] = Field(default_factory=list)


class ReflectionInput(BaseModel):
    objective: str
    task_tree: TaskTreeSnapshot
    challenge_context: str = ""
    strategy_results: list[StrategyTaskResult] = Field(default_factory=list)
    reflection_history: list[ReflectionHistoryEntry] = Field(default_factory=list)
    latest_discoveries: list[TaskDiscovery] = Field(default_factory=list)
    available_artifacts: list[ArtifactRef] = Field(default_factory=list)


class ReflectionOutput(BaseModel):
    summary: str
    planner_guidance: str = ""
    task_updates: list[ReflectionTaskUpdate] = Field(default_factory=list)
    failure_patterns: list[FailurePattern] = Field(default_factory=list)
    strategic_rejections: list[StrategicRejection] = Field(default_factory=list)
    critical_findings: list[str] = Field(default_factory=list)
    flag_candidates: list[str] = Field(default_factory=list)
    credentials: list[str] = Field(default_factory=list)


class SubmissionInput(BaseModel):
    candidates: list[str] = Field(default_factory=list)
    latest_discoveries: list[TaskDiscovery] = Field(default_factory=list)
    submitted_flags: list[str] = Field(default_factory=list)


class SubmissionOutput(BaseModel):
    should_submit: bool
    flag: str | None = None
    reason: str


class SubmissionResult(BaseModel):
    correct: bool
    message: str = ""
    flag_count: int = 0
    flag_got_count: int = 0


class ChallengeSpec(BaseModel):
    target: str
    target_type: Literal["http", "tcp"] = "http"
    description: str = ""
    zone: str = "zone1"
    code: str = ""
    title: str = ""
    difficulty: str = ""
    level: int = 0
    entrypoints: list[str] = Field(default_factory=list)
    hint_content: str = ""
    flag_count: int = 0
    flag_got_count: int = 0


class RunState(BaseModel):
    run_id: str
    challenge: ChallengeSpec
    task_tree: TaskTreeSnapshot
    planner_context: PlannerContext | None = None
    planner_history: list[PlannerHistoryEntry] = Field(default_factory=list)
    reflection_history: list[ReflectionHistoryEntry] = Field(default_factory=list)
    latest_discoveries: list[TaskDiscovery] = Field(default_factory=list)
    available_artifacts: list[ArtifactRef] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list)
    submitted_flags: list[str] = Field(default_factory=list)
    rounds_completed: int = 0
