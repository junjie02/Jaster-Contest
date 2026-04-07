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


class Observation(BaseModel):
    round: int = 0
    source: str = ""
    command: str = ""
    result_type: str = ""
    summary: str = ""
    next_action_hint: str = ""


class ExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

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


class GlobalFacts(BaseModel):
    flags: list[str] = Field(default_factory=list)
    credentials: list[str] = Field(default_factory=list)
    services: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)


class TreeNodeSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    parent_key: str = ""
    title: str
    kind: NodeKind
    locator: str = ""
    status: NodeStatus = NodeStatus.unexplored
    priority: int = 0
    value: str = ""
    reason: str = ""
    how: str = ""
    evidence: list[str] = Field(default_factory=list)
    shared_refs: list[str] = Field(default_factory=list)
    key_findings: list[str] = Field(default_factory=list)


class NodeInfo(BaseModel):
    """Strategy输入中使用的节点信息，包含TreeNodeSnapshot和NodePatch的所有字段"""
    key: str
    parent_key: str = ""
    title: str
    kind: NodeKind
    locator: str = ""
    status: NodeStatus = NodeStatus.unexplored
    priority: int = 0
    value: str = ""
    reason: str = ""
    how: str = ""
    evidence: list[str] = Field(default_factory=list)
    shared_refs: list[str] = Field(default_factory=list)
    key_findings: list[str] = Field(default_factory=list)


class AttackTreeSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[TreeNodeSnapshot] = Field(default_factory=list)
    facts: GlobalFacts = Field(default_factory=GlobalFacts)


class NodePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_key: str
    title: str
    kind: NodeKind
    locator: str
    priority: int = 0
    value: str = ""
    reason: str = ""
    how: str = ""
    evidence: list[str] = Field(default_factory=list)
    status: NodeStatus = NodeStatus.unexplored
    shared_refs: list[str] = Field(default_factory=list)
    key_findings: list[str] = Field(default_factory=list)


class NodeUpdatePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    status: NodeStatus | None = None
    priority: int | None = None
    value: str | None = None
    reason: str | None = None
    how: str | None = None
    evidence: list[str] | None = None
    shared_refs: list[str] | None = None
    key_findings: list[str] | None = None


class TreePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    add_nodes: list[NodePatch] = Field(default_factory=list)
    update_nodes: list[NodeUpdatePatch] = Field(default_factory=list)


class ActionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["skill", "builder", "finish"]
    goal: str
    expected_result: str = ""
    skill_name: str | None = None
    skill_args: dict[str, Any] = Field(default_factory=dict)
    builder_task: str | None = None


class AvailableSkill(BaseModel):
    name: str
    summary: str
    use_when: str = ""
    params_summary: str = ""


class ReconInput(BaseModel):
    objective: str
    tree: AttackTreeSnapshot
    recent_observations: list[Observation] = Field(default_factory=list)
    latest_execution: ExecutionResult | None = None
    available_skills: list[AvailableSkill] = Field(default_factory=list)
    latest_summary: str = ""


class ReconOutput(BaseModel):
    summary: str
    discover_vulnerability: bool = False
    selected_node_key: str = ""
    action: ActionPlan
    tree_patch: TreePatch = Field(default_factory=TreePatch)
    key_findings: list[str] = Field(default_factory=list)
    result_type: str = ""
    next_action_hint: str = ""


class StrategyInput(BaseModel):
    objective: str
    target_node: NodeInfo
    path_to_root: list[NodeInfo] = Field(default_factory=list)
    related_nodes: list[NodeInfo] = Field(default_factory=list)
    latest_summary: str = ""
    recent_observations: list[Observation] = Field(default_factory=list)
    latest_execution: ExecutionResult | None = None
    available_skills: list[AvailableSkill] = Field(default_factory=list)


class StrategyOutput(BaseModel):
    summary: str
    selected_node_key: str = ""
    action: ActionPlan
    flag_candidates: list[str] = Field(default_factory=list)
    goal_reached: bool = False
    need_recon: bool = False
    tree_patch: TreePatch = Field(default_factory=TreePatch)
    key_findings: list[str] = Field(default_factory=list)
    next_action_hint: str = ""
    result_type: str = ""


class ReflectionInput(BaseModel):
    objective: str
    tree: AttackTreeSnapshot
    recent_observations: list[Observation] = Field(default_factory=list)
    latest_execution: ExecutionResult | None = None
    last_strategy: str = ""
    latest_summary: str = ""


class ReflectionOutput(BaseModel):
    summary: str
    next_focus_key: str = ""
    flag_candidates: list[str] = Field(default_factory=list)
    tree_patch: TreePatch = Field(default_factory=TreePatch)


class BuilderInput(BaseModel):
    task: str


class BuilderOutput(BaseModel):
    summary: str
    script: str


class SubmissionInput(BaseModel):
    candidates: list[str] = Field(default_factory=list)
    recent_observations: list[Observation] = Field(default_factory=list)
    submitted_flags: list[str] = Field(default_factory=list)


class SubmissionOutput(BaseModel):
    should_submit: bool
    flag: str | None = None
    reason: str


class ChallengeSpec(BaseModel):
    target: str
    target_type: Literal["http", "tcp"] = "http"
    description: str = ""
    zone: str = "zone1"


class RunState(BaseModel):
    run_id: str
    challenge: ChallengeSpec
    tree: AttackTreeSnapshot
    observations: list[Observation] = Field(default_factory=list)
    submitted_flags: list[str] = Field(default_factory=list)
    rounds_completed: int = 0
