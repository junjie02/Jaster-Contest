from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

import httpx
from pydantic import BaseModel, Field

from jaster.domain import ChallengeSpec, Observation, RunState, SubmissionResult
from jaster.runtime.orchestrator import JasterOrchestrator, detect_target_type
from jaster.runtime.platform import (
    ChallengeListData,
    PlatformAPIError,
    PlatformChallenge,
    PlatformClient,
)


DIFFICULTY_ORDER = {"low": 0, "medium": 1, "hard": 2}


def zone_for_level(level: int) -> str:
    return {
        1: "zone1",
        2: "zone2",
        3: "zone3",
        4: "zone4",
    }.get(level, "zone1")


class ContestChallengeState(BaseModel):
    code: str
    title: str = ""
    difficulty: str = ""
    level: int = 0
    solved: bool = False
    attempts_in_cycle: int = 0
    hint_used: bool = False
    hint_content: str = ""
    used_rounds: int = 0
    last_run_id: str = ""
    last_flag_progress: int = 0
    incorrect_flags: list[str] = Field(default_factory=list)
    last_status: str = ""
    last_error: str = ""


class ContestSessionState(BaseModel):
    session_id: str
    platform_host: str
    started_at: float
    current_level: int = 0
    status: str = "running"
    last_synced_at: float = 0.0


class ContestSessionStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def new_session_id(self) -> str:
        return uuid.uuid4().hex[:12]

    def session_dir(self, session_id: str) -> Path:
        return self.root / session_id

    def create(self, session: ContestSessionState, challenges: dict[str, ContestChallengeState]) -> None:
        session_dir = self.session_dir(session.session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        self.save(session, challenges)

    def save(self, session: ContestSessionState, challenges: dict[str, ContestChallengeState]) -> None:
        session_dir = self.session_dir(session.session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "session.json").write_text(
            json.dumps(session.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (session_dir / "challenges.json").write_text(
            json.dumps({key: value.model_dump() for key, value in challenges.items()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def append_event(self, session_id: str, event: dict[str, object]) -> None:
        path = self.session_dir(session_id) / "events.jsonl"
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        line = json.dumps(event, ensure_ascii=False)
        path.write_text(f"{existing}{line}\n", encoding="utf-8")


class ContestScheduler:
    def __init__(
        self,
        *,
        client: PlatformClient,
        orchestrator: JasterOrchestrator,
        session_store: ContestSessionStore,
        data_dir: Path,
        max_rounds_per_attempt: int = 200,
        ready_timeout_seconds: int = 90,
        ready_poll_interval: float = 2.0,
    ) -> None:
        self.client = client
        self.orchestrator = orchestrator
        self.session_store = session_store
        self.max_rounds_per_attempt = max_rounds_per_attempt
        self.ready_timeout_seconds = ready_timeout_seconds
        self.ready_poll_interval = ready_poll_interval
        self.data_dir = data_dir
        self.session = ContestSessionState(
            session_id=session_store.new_session_id(),
            platform_host=client.base_url,
            started_at=time.time(),
        )
        self.challenge_states: dict[str, ContestChallengeState] = {}
        self.session_store.create(self.session, self.challenge_states)

    def run(self, *, max_attempts: int | None = None) -> ContestSessionState:
        attempts = 0
        while True:
            listing = self.sync()
            visible_unsolved = [item for item in listing.challenges if item.flag_got_count < item.flag_count]
            if not visible_unsolved:
                self.session.status = "completed"
                self.session_store.save(self.session, self.challenge_states)
                return self.session
            challenge = self._pick_next_challenge(visible_unsolved)
            if challenge is None:
                self._reset_cycle(visible_unsolved)
                challenge = self._pick_next_challenge(visible_unsolved)
                if challenge is None:
                    self.session.status = "completed"
                    self.session_store.save(self.session, self.challenge_states)
                    return self.session
            self._attempt_challenge(challenge)
            attempts += 1
            if max_attempts is not None and attempts >= max_attempts:
                self.session.status = "stopped"
                self.session_store.save(self.session, self.challenge_states)
                return self.session

    def sync(self) -> ChallengeListData:
        listing = self.client.list_challenges()
        self.session.current_level = listing.current_level
        self.session.last_synced_at = time.time()
        visible_codes = {item.code for item in listing.challenges}
        for item in listing.challenges:
            state = self.challenge_states.setdefault(
                item.code,
                ContestChallengeState(
                    code=item.code,
                    title=item.title,
                    difficulty=item.difficulty,
                    level=item.level,
                ),
            )
            state.title = item.title
            state.difficulty = item.difficulty
            state.level = item.level
            state.hint_used = state.hint_used or item.hint_viewed
            state.last_flag_progress = item.flag_got_count
            state.solved = item.flag_count > 0 and item.flag_got_count >= item.flag_count
            state.last_status = item.instance_status
            if state.solved:
                state.attempts_in_cycle = 0
        for code, state in self.challenge_states.items():
            if code not in visible_codes and not state.solved:
                state.last_status = "hidden"
        self.session_store.save(self.session, self.challenge_states)
        return listing

    def _attempt_challenge(self, challenge: PlatformChallenge) -> None:
        state = self.challenge_states[challenge.code]
        state.last_error = ""
        self._stop_other_running_instances(challenge.code)
        try:
            active = self._ensure_instance_ready(challenge)
            if state.solved:
                return
            if challenge.difficulty.lower() == "hard" and not active.hint_viewed and not state.hint_used:
                hint = self.client.view_hint(challenge.code)
                state.hint_used = True
                state.hint_content = hint.hint_content or ""
                active.hint_viewed = True
                self.session_store.append_event(
                    self.session.session_id,
                    {"type": "hint", "code": challenge.code, "message": state.hint_content},
                )
            spec = self._challenge_spec_from_platform(active)
            spec.hint_content = state.hint_content
            baseline_progress = active.flag_got_count
            hint_injected = state.hint_used

            def submission_handler(current: ChallengeSpec, flag: str, run_state: RunState) -> SubmissionResult:
                if flag in state.incorrect_flags:
                    return SubmissionResult(
                        correct=False,
                        message="该错误 Flag 已提交过，跳过重复提交",
                        flag_count=current.flag_count,
                        flag_got_count=current.flag_got_count,
                    )
                result = self.client.submit_flag(challenge.code, flag)
                self.session_store.append_event(
                    self.session.session_id,
                    {"type": "submit", "code": challenge.code, "flag": flag, "correct": result.correct},
                )
                if result.correct:
                    state.last_flag_progress = result.flag_got_count
                    current.flag_count = result.flag_count
                    current.flag_got_count = result.flag_got_count
                else:
                    state.incorrect_flags.append(flag)
                return SubmissionResult(
                    correct=result.correct,
                    message=result.message,
                    flag_count=result.flag_count,
                    flag_got_count=result.flag_got_count,
                )

            def round_hook(run_state: RunState, phase: str, latest_execution: object) -> bool:
                nonlocal hint_injected
                refreshed = self.sync()
                refreshed_item = self._get_challenge(refreshed, challenge.code)
                if refreshed_item is None:
                    return False
                run_state.challenge.flag_count = refreshed_item.flag_count
                run_state.challenge.flag_got_count = refreshed_item.flag_got_count
                run_state.challenge.entrypoints = list(refreshed_item.entrypoint or [])
                if refreshed_item.flag_count > 0 and refreshed_item.flag_got_count >= refreshed_item.flag_count:
                    state.solved = True
                    state.last_flag_progress = refreshed_item.flag_got_count
                    return True
                should_hint = (
                    challenge.difficulty.lower() in {"low", "medium"}
                    and not hint_injected
                    and not refreshed_item.hint_viewed
                    and run_state.rounds_completed >= 100
                    and refreshed_item.flag_got_count <= baseline_progress
                )
                if should_hint:
                    hint = self.client.view_hint(challenge.code)
                    hint_injected = True
                    state.hint_used = True
                    state.hint_content = hint.hint_content or ""
                    run_state.challenge.hint_content = state.hint_content
                    self.session_store.append_event(
                        self.session.session_id,
                        {"type": "hint", "code": challenge.code, "message": state.hint_content},
                    )
                return False

            run_state = self.orchestrator.run(
                spec,
                max_rounds=self.max_rounds_per_attempt,
                submission_handler=submission_handler,
                round_hook=round_hook,
            )
            state.used_rounds += run_state.rounds_completed
            state.last_run_id = run_state.run_id
            refreshed = self.sync()
            refreshed_item = self._get_challenge(refreshed, challenge.code)
            if refreshed_item is not None:
                state.solved = refreshed_item.flag_count > 0 and refreshed_item.flag_got_count >= refreshed_item.flag_count
                state.last_flag_progress = refreshed_item.flag_got_count
                state.hint_used = state.hint_used or refreshed_item.hint_viewed
            if state.solved:
                state.attempts_in_cycle = 0
            else:
                state.attempts_in_cycle += 1
        except PlatformAPIError as exc:
            state.last_error = str(exc)
            state.attempts_in_cycle += 1
            self.session_store.append_event(
                self.session.session_id,
                {"type": "error", "code": challenge.code, "message": str(exc)},
            )
        finally:
            try:
                self.client.stop_challenge(challenge.code)
            except PlatformAPIError:
                pass
            self.session_store.append_event(
                self.session.session_id,
                {"type": "stop", "code": challenge.code},
            )
            self.session_store.save(self.session, self.challenge_states)

    def _pick_next_challenge(self, challenges: list[PlatformChallenge]) -> PlatformChallenge | None:
        ordered = sorted(
            enumerate(challenges),
            key=lambda item: (
                DIFFICULTY_ORDER.get(item[1].difficulty.lower(), 99),
                -item[1].level,
                item[0],
            ),
        )
        for _, challenge in ordered:
            state = self.challenge_states.setdefault(
                challenge.code,
                ContestChallengeState(
                    code=challenge.code,
                    title=challenge.title,
                    difficulty=challenge.difficulty,
                    level=challenge.level,
                ),
            )
            if not state.solved and state.attempts_in_cycle < 2:
                return challenge
        return None

    def _reset_cycle(self, challenges: list[PlatformChallenge]) -> None:
        for item in challenges:
            state = self.challenge_states.get(item.code)
            if state and not state.solved:
                state.attempts_in_cycle = 0
        self.session_store.append_event(self.session.session_id, {"type": "cycle_reset"})
        self.session_store.save(self.session, self.challenge_states)

    def _ensure_instance_ready(self, challenge: PlatformChallenge) -> PlatformChallenge:
        if challenge.instance_status == "running" and challenge.entrypoint:
            self.session_store.append_event(
                self.session.session_id,
                {"type": "reuse", "code": challenge.code, "entrypoints": challenge.entrypoint},
            )
            return challenge
        started = self.client.start_challenge(challenge.code)
        if started.already_completed:
            state = self.challenge_states[challenge.code]
            state.solved = True
            state.last_status = "already_completed"
            return challenge
        self.session_store.append_event(
            self.session.session_id,
            {"type": "start", "code": challenge.code, "entrypoints": started.entrypoints},
        )
        deadline = time.monotonic() + self.ready_timeout_seconds
        while time.monotonic() < deadline:
            listing = self.sync()
            refreshed = self._get_challenge(listing, challenge.code)
            if refreshed and refreshed.instance_status == "running" and refreshed.entrypoint:
                return refreshed
            time.sleep(self.ready_poll_interval)
        raise PlatformAPIError(f"题目 {challenge.code} 启动超时")

    def _stop_other_running_instances(self, current_code: str) -> None:
        listing = self.sync()
        for item in listing.challenges:
            if item.code != current_code and item.instance_status == "running":
                try:
                    self.client.stop_challenge(item.code)
                except PlatformAPIError:
                    continue
                self.session_store.append_event(
                    self.session.session_id,
                    {"type": "stop_stale", "code": item.code},
                )

    def _challenge_spec_from_platform(self, challenge: PlatformChallenge) -> ChallengeSpec:
        target, target_type, entrypoints = resolve_entrypoints(challenge.entrypoint or [])
        return ChallengeSpec(
            target=target,
            target_type=target_type,
            description=build_platform_description(challenge, target, entrypoints),
            zone=zone_for_level(challenge.level),
            code=challenge.code,
            title=challenge.title,
            difficulty=challenge.difficulty,
            level=challenge.level,
            entrypoints=entrypoints,
            flag_count=challenge.flag_count,
            flag_got_count=challenge.flag_got_count,
        )

    @staticmethod
    def _get_challenge(listing: ChallengeListData, code: str) -> PlatformChallenge | None:
        for item in listing.challenges:
            if item.code == code:
                return item
        return None


def build_platform_description(challenge: PlatformChallenge, target: str, entrypoints: list[str]) -> str:
    lines = []
    if challenge.title:
        lines.append(f"标题: {challenge.title}")
    if challenge.description:
        lines.append(f"官方描述: {challenge.description}")
    if target:
        lines.append(f"主入口: {target}")
    others = [item for item in entrypoints if item != target]
    if others:
        lines.append("其他入口: " + ", ".join(others))
    if challenge.difficulty:
        lines.append(f"难度: {challenge.difficulty}")
    if challenge.level:
        lines.append(f"赛区等级: {challenge.level}")
    if challenge.flag_count:
        lines.append(f"Flag进度: {challenge.flag_got_count}/{challenge.flag_count}")
    return "\n".join(lines)


def resolve_entrypoints(entrypoints: list[str]) -> tuple[str, str, list[str]]:
    cleaned = [item.strip() for item in entrypoints if item and item.strip()]
    for entry in cleaned:
        if "://" in entry:
            return entry, detect_target_type(entry), cleaned
        if _looks_like_http(entry):
            return f"http://{entry}", "http", cleaned
    first = cleaned[0] if cleaned else ""
    return first, detect_target_type(first), cleaned


def _looks_like_http(entrypoint: str) -> bool:
    if ":" not in entrypoint:
        return False
    try:
        response = httpx.get(f"http://{entrypoint}", timeout=3.0, follow_redirects=True)
    except httpx.HTTPError:
        return False
    return response.status_code > 0


def create_contest_scheduler(
    *,
    base_url: str,
    agent_token: str,
    orchestrator: JasterOrchestrator,
    data_dir: Path,
) -> ContestScheduler:
    return ContestScheduler(
        client=PlatformClient(base_url=f"{base_url.rstrip('/')}/api", agent_token=agent_token),
        orchestrator=orchestrator,
        session_store=ContestSessionStore(data_dir / "contests"),
        data_dir=data_dir,
    )
