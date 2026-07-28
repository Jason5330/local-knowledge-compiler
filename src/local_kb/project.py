"""Resolve project-local knowledge Vault locations."""

from pathlib import Path


DEFAULT_VAULT_NAME = "KnowledgeBase"


def default_vault_path(cwd: Path | None = None) -> Path:
    base = Path.cwd() if cwd is None else Path(cwd)
    return (base / DEFAULT_VAULT_NAME).resolve()


def is_initialized_vault(path: Path) -> bool:
    candidate = Path(path)
    return (
        candidate.is_dir()
        and (candidate / "80_system" / "config.toml").is_file()
        and (
            candidate / "80_system" / "KNOWLEDGE_PROTOCOL.md"
        ).is_file()
    )


def resolve_vault_path(
    explicit: Path | None,
    *,
    cwd: Path | None = None,
) -> Path:
    base = (Path.cwd() if cwd is None else Path(cwd)).resolve()
    if explicit is not None:
        candidate = Path(explicit).resolve()
        if not is_initialized_vault(candidate):
            raise ValueError(
                f"not an initialized knowledge vault: {candidate}"
            )
        return candidate
    if is_initialized_vault(base):
        return base
    child = base / DEFAULT_VAULT_NAME
    if is_initialized_vault(child):
        return child.resolve()
    if child.exists():
        raise ValueError(
            f"not an initialized knowledge vault: {child.resolve()}"
        )
    raise ValueError(
        "找不到已初始化的知識庫；請先對 AI 說「初始化本專案知識庫」。"
    )
