from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class NodeKind(str, Enum):
    target = "target"
    asset = "asset"
    entry = "entry"
    weakness = "weakness"
    technique = "technique"
    hypothesis = "hypothesis"


class NodeStatus(str, Enum):
    unexplored = "unexplored"
    exploring = "exploring"
    success = "success"
    failed = "failed"


class ArtifactRef(BaseModel):
    kind: str
    path: str
    producer_phase: str = ""
    producer_task_id: str = ""
    producer_function_name: str = ""
    producer_success: bool | None = None


class Observation(BaseModel):
    round: int = 0
    source: str = ""
    task_id: str = ""
    task: str = ""
    target: str = ""
    result: str = ""


class RecentObservationAction(BaseModel):
    task: str = ""
    target: str = ""
    result: str = ""


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
    task_results: dict[str, "TaskExecutionResult"] = Field(default_factory=dict)


class TaskExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    kind: Literal["function", "builder", "finish"]
    function_name: str | None = None
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


class GlobalFacts(BaseModel):
    flags: list[str] = Field(default_factory=list)
    credentials: list[str] = Field(default_factory=list)


class TreeNodeSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    parent_key: str = ""
    title: str
    kind: NodeKind
    status: NodeStatus = NodeStatus.unexplored
    priority: int = 0
    reason: str = ""
    how: str = ""
    shared_refs: list[str] = Field(default_factory=list)


class NodeInfo(BaseModel):
    """Strategy输入中使用的节点信息，包含TreeNodeSnapshot和NodePatch的所有字段"""
    key: str
    parent_key: str = ""
    title: str
    kind: NodeKind
    status: NodeStatus = NodeStatus.unexplored
    priority: int = 0
    reason: str = ""
    how: str = ""
    shared_refs: list[str] = Field(default_factory=list)


class AttackTreeSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[TreeNodeSnapshot] = Field(default_factory=list)
    facts: GlobalFacts = Field(default_factory=GlobalFacts)


class NodePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_key: str
    title: str
    kind: NodeKind
    priority: int = 0
    reason: str = ""
    how: str = ""
    status: NodeStatus = NodeStatus.unexplored
    shared_refs: list[str] = Field(default_factory=list)


class NodeUpdatePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    status: NodeStatus | None = None
    priority: int | None = None
    reason: str | None = None
    how: str | None = None
    shared_refs: list[str] | None = None


class TreePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    add_nodes: list[NodePatch] = Field(default_factory=list)
    update_nodes: list[NodeUpdatePatch] = Field(default_factory=list)


class ActionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = ""
    kind: Literal["function", "builder", "finish"]
    goal: str
    expected_result: str = ""
    function_name: str | None = None
    function_args: dict[str, Any] = Field(default_factory=dict)
    key_parameters: list[dict[str, str]] = Field(default_factory=list)
    executor_brief: str = ""


class AvailableFunction(BaseModel):
    name: str
    summary: str
    use_when: str = ""


class AvailableSkill(BaseModel):
    name: str
    summary: str
    use_when: str = ""


class ReconInput(BaseModel):
    objective: str
    tree: AttackTreeSnapshot
    challenge_context: str = ""
    recent_observations: list[RecentObservationRound] = Field(default_factory=list)
    latest_execution: LatestExecutionResult | None = None
    available_artifacts: list[ArtifactRef] = Field(default_factory=list)
    available_functions: list[AvailableFunction] = Field(default_factory=list)
    latest_summary: str = ""


class ReconOutput(BaseModel):
    phase_summary: str
    discover_vulnerability: bool = False
    selected_node_key: str = ""
    actions: list[ActionPlan] = Field(default_factory=list)
    tree_patch: TreePatch = Field(default_factory=TreePatch)
    observed_task_results: list[ObservedTaskResult] = Field(default_factory=list)
    credentials: list[str] = Field(default_factory=list)


class StrategyInput(BaseModel):
    objective: str
    target_node: NodeInfo
    path_to_root: list[NodeInfo] = Field(default_factory=list)
    related_nodes: list[NodeInfo] = Field(default_factory=list)
    challenge_context: str = ""
    latest_summary: str = ""
    recent_observations: list[RecentObservationRound] = Field(default_factory=list)
    latest_execution: LatestExecutionResult | None = None
    available_artifacts: list[ArtifactRef] = Field(default_factory=list)
    available_functions: list[AvailableFunction] = Field(default_factory=list)


class StrategyOutput(BaseModel):
    phase_summary: str
    selected_node_key: str = ""
    actions: list[ActionPlan] = Field(default_factory=list)
    flag_candidates: list[str] = Field(default_factory=list)
    goal_reached: bool = False
    need_recon: bool = False
    tree_patch: TreePatch = Field(default_factory=TreePatch)
    observed_task_results: list[ObservedTaskResult] = Field(default_factory=list)
    credentials: list[str] = Field(default_factory=list)


class ReflectionInput(BaseModel):
    objective: str
    tree: AttackTreeSnapshot
    challenge_context: str = ""
    recent_observations: list[RecentObservationRound] = Field(default_factory=list)
    latest_execution: LatestExecutionResult | None = None
    available_artifacts: list[ArtifactRef] = Field(default_factory=list)
    last_strategy: str = ""
    latest_summary: str = ""
    selected_skills: list[str] = Field(default_factory=list)
    inspiration: str = ""


class ReflectionOutput(BaseModel):
    summary: str
    next_focus_key: str = ""
    flag_candidates: list[str] = Field(default_factory=list)
    tree_patch: TreePatch = Field(default_factory=TreePatch)
    credentials: list[str] = Field(default_factory=list)


class SkillRouterInput(BaseModel):
    objective: str
    tree: AttackTreeSnapshot
    challenge_context: str = ""
    recent_observations: list[RecentObservationRound] = Field(default_factory=list)
    latest_execution: LatestExecutionResult | None = None
    last_strategy: str = ""
    latest_summary: str = ""
    available_skills: list[AvailableSkill] = Field(default_factory=list)


class SkillRouterOutput(BaseModel):
    selected_skills: list[str] = Field(default_factory=list, max_length=2)


class ExecutorInput(BaseModel):
    target: str = ""
    function_name: str
    function_summary: str = ""
    function_schema_text: str
    function_definition_json: str = ""
    executor_brief: str
    accessible_artifacts: list[ArtifactRef] = Field(default_factory=list)


class BuilderInput(BaseModel):
    task: str
    key_parameters: list[dict[str, str]] = Field(default_factory=list)
    accessible_artifacts: list[ArtifactRef] = Field(default_factory=list)


class BuilderOutput(BaseModel):
    summary: str
    script: str


class SubmissionInput(BaseModel):
    candidates: list[str] = Field(default_factory=list)
    recent_observations: list[RecentObservationRound] = Field(default_factory=list)
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
    tree: AttackTreeSnapshot
    available_artifacts: list[ArtifactRef] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list)
    submitted_flags: list[str] = Field(default_factory=list)
    rounds_completed: int = 0
