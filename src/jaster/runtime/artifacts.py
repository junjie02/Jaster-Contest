from __future__ import annotations

from pathlib import Path

from jaster.domain import ArtifactRef


def snapshot_work_dir(cwd: Path) -> set[str]:
    if not cwd.exists():
        return set()
    snapshots: set[str] = set()
    for path in cwd.rglob("*"):
        try:
            relative = path.relative_to(cwd)
        except ValueError:
            continue
        rendered = str(relative)
        if rendered:
            snapshots.add(rendered)
    return snapshots


def discover_artifacts(cwd: Path, *, before_paths: set[str], declared_outputs: list[Path] | None = None) -> list[ArtifactRef]:
    artifacts: list[ArtifactRef] = []
    declared_outputs = declared_outputs or []
    for path in declared_outputs:
        artifact = _artifact_for_path(path)
        if artifact is not None:
            artifacts.append(artifact)

    after_paths = snapshot_work_dir(cwd)
    new_paths = sorted(after_paths - before_paths, key=lambda item: (item.count("/"), item))
    selected_dirs: list[str] = []
    for relative in new_paths:
        if any(relative == parent or relative.startswith(parent + "/") for parent in selected_dirs):
            continue
        absolute = (cwd / relative).resolve(strict=False)
        artifact = _artifact_for_path(absolute)
        if artifact is None:
            continue
        artifacts.append(artifact)
        if absolute.is_dir():
            selected_dirs.append(relative)
    return _dedupe_artifacts(artifacts)


def filter_available_artifacts(artifacts: list[ArtifactRef]) -> list[ArtifactRef]:
    filtered: list[ArtifactRef] = []
    for artifact in artifacts:
        if artifact.kind not in {"file", "directory"}:
            continue
        path = Path(artifact.path)
        if artifact.kind == "directory":
            if not _is_non_empty_dir(path):
                continue
        elif not _is_non_empty_file(path):
            continue
        filtered.append(artifact)
    return _dedupe_artifacts(filtered)


def _artifact_for_path(path: Path) -> ArtifactRef | None:
    resolved = path.resolve(strict=False)
    if not resolved.exists():
        return None
    if resolved.is_dir():
        if not _is_non_empty_dir(resolved):
            return None
        return ArtifactRef(kind="directory", path=str(resolved))
    if resolved.is_file():
        if not _is_non_empty_file(resolved):
            return None
        return ArtifactRef(kind="file", path=str(resolved))
    return None


def _is_non_empty_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    try:
        next(path.iterdir())
    except (StopIteration, OSError):
        return False
    return True


def _is_non_empty_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _dedupe_artifacts(artifacts: list[ArtifactRef]) -> list[ArtifactRef]:
    seen: set[tuple[str, str]] = set()
    deduped: list[ArtifactRef] = []
    for artifact in artifacts:
        key = (artifact.kind, artifact.path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(artifact)
    return deduped
