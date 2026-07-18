"""
Deterministic digests for the two-hash config determinism (Phase 4 / invariant I4).

TWO values fold into the orchestrator's active NeMo pin's ``nemo`` selector so
"same ``config_sha256`` ⇒ same guard behavior" survives the two-service split (a
bare NeMo-config digest would MISS NeMo/LangChain/langchain-aws resolution drift):

  (i)  ``config_dir_digest`` — a stable digest of THIS pod's NeMo ``config/``
       directory (``config.yml`` + ``prompts.yml`` + ``detectors.yml`` + any
       ``rails/*.co``). Computed by :func:`compute_config_dir_digest` HERE, in
       ``services/guardrail/``. ``detectors.yml`` (the deterministic secrets/PII
       pattern tables) is FOLDED INTO this SAME digest — one determinism
       mechanism, NO new pin hash — so a regex change becomes a pod
       ``config_version`` bump + a fresh orchestrator ``config_sha256``.
  (ii) ``uv_lock_sha256`` — the sha256 of THIS pod's resolved
       ``services/guardrail/uv.lock`` (the LOCKED dependency artifact whose
       transitive resolution pins NeMo + LangChain + langchain-aws). Computed by
       :func:`compute_lock_sha256` HERE, in ``services/guardrail/``.

Both are written as LITERAL pinned strings into the active ``legal-rag-default``
config YAML. Because they are bytes of that YAML they enter the orchestrator's
``config_sha256`` (truncated sha256 of the raw manifest-verified file bytes,
``services/orchestrator/app/config.py``) automatically — so changing EITHER the
NeMo config (config.yml / prompts.yml / detectors.yml / rails) OR the dependency
lock forces a DIFFERENT pin hash. A drifted resolution can no longer masquerade
under an unchanged config.

REGENERATE the pinned values (regen/audit mechanism) with either::

    cd services/guardrail && uv run python -m app.config_digest

The pod re-computes these live at startup and REFUSES to serve on mismatch (see
the ``app.main`` lifespan self-verification), so a drifted image cannot silently
answer under a pinned config.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Final

# The files that constitute the hashed NeMo config artifact. config.yml,
# prompts.yml and detectors.yml are MANDATORY; rails/*.co is globbed. detectors.yml
# (the deterministic secrets/PII pattern tables) is folded in HERE so a regex edit
# forces a re-pin through the SAME config_dir_digest — no separate hash.
_MANDATORY_CONFIG_FILES: Final[tuple[str, ...]] = (
    "config.yml",
    "prompts.yml",
    "detectors.yml",
)
_RAILS_GLOB: Final[str] = "rails/*.co"

# Default uv.lock location relative to the pod's config/ directory: the lock is
# the package root sibling of config/ (services/guardrail/uv.lock).
_LOCK_FILENAME: Final[str] = "uv.lock"


def _digest_member_paths(config_dir: Path) -> list[Path]:
    """
    Resolve the ordered, deterministic list of files that enter the digest.

    Order is stable: the mandatory files first (in declared order —
    ``config.yml``, ``prompts.yml``, ``detectors.yml``), then any ``rails/*.co``
    sorted by POSIX relative path — so the digest is independent of filesystem
    enumeration order.
    """
    members: list[Path] = []
    missing: list[str] = []
    for name in _MANDATORY_CONFIG_FILES:
        path = config_dir / name
        if not path.is_file():
            missing.append(name)
        members.append(path)
    if missing:
        raise FileNotFoundError(
            f"NeMo config digest cannot be computed: missing {missing} under {config_dir}"
        )
    rails = sorted(
        config_dir.glob(_RAILS_GLOB),
        key=lambda p: p.relative_to(config_dir).as_posix(),
    )
    members.extend(rails)
    return members


def compute_config_dir_digest(config_dir: str | Path) -> str:
    """
    Compute the deterministic sha256 digest of the NeMo ``config/`` directory.

    The digest hashes, for each member file (``config.yml``, ``prompts.yml``,
    ``detectors.yml``, then sorted ``rails/*.co``): the file's POSIX relative
    path, a NUL separator, the raw file bytes, and a trailing NUL. Path framing
    makes a rename tamper-evident; the fixed order makes the result independent of
    directory enumeration order.

    Args:
        config_dir: The pod's NeMo config directory.

    Returns:
        64-char lowercase hex sha256 digest.

    Raises:
        FileNotFoundError: If a mandatory config file is absent.
    """
    root = Path(config_dir)
    digest = hashlib.sha256()
    for path in _digest_member_paths(root):
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def compute_lock_sha256(lock_path: str | Path) -> str:
    """
    Compute the sha256 of the resolved ``uv.lock`` (the locked dependency set).

    Args:
        lock_path: Path to ``services/guardrail/uv.lock``.

    Returns:
        64-char lowercase hex sha256 digest.

    Raises:
        FileNotFoundError: If the lock file is absent.
    """
    path = Path(lock_path)
    if not path.is_file():
        raise FileNotFoundError(f"uv.lock not found for sha256: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


class DigestMismatchError(RuntimeError):
    """
    Live determinism digests do NOT match the pinned config values.

    Raised by :func:`verify_pinned_digests` in the pod lifespan; it aborts
    startup so the pod REFUSES to serve — a drifted ``config/`` or ``uv.lock``
    cannot silently answer under a pinned ``legal-rag-default`` selector.
    """


def verify_pinned_digests(
    *,
    config_dir: str | Path,
    lock_path: str | Path,
    expected_config_dir_digest: str | None,
    expected_uv_lock_sha256: str | None,
) -> None:
    """
    Assert the pod's LIVE digests match the pinned expectations.

    A ``None`` expectation is skipped (dev runs with no pinned expectation still
    serve). Any set expectation that disagrees with the freshly-computed live
    value raises :class:`DigestMismatchError`, aborting startup.

    Args:
        config_dir: The pod's NeMo ``config/`` directory.
        lock_path: Path to the resolved ``uv.lock``.
        expected_config_dir_digest: The active pin's ``config_dir_digest``
            (from env, CI/deploy-populated), or ``None`` to skip.
        expected_uv_lock_sha256: The active pin's ``uv_lock_sha256``
            (from env, CI/deploy-populated), or ``None`` to skip.

    Raises:
        DigestMismatchError: If any set expectation disagrees with the live value.
    """
    mismatches: list[str] = []
    if expected_config_dir_digest is not None:
        live = compute_config_dir_digest(config_dir)
        if live != expected_config_dir_digest:
            mismatches.append(
                f"config_dir_digest: pinned={expected_config_dir_digest} live={live}"
            )
    if expected_uv_lock_sha256 is not None:
        live = compute_lock_sha256(lock_path)
        if live != expected_uv_lock_sha256:
            mismatches.append(
                f"uv_lock_sha256: pinned={expected_uv_lock_sha256} live={live}"
            )
    if mismatches:
        raise DigestMismatchError(
            "guardrail pod REFUSES to serve — live determinism digest(s) do not "
            "match the pinned values (" + "; ".join(mismatches) + "). A drifted "
            "config/ or uv.lock cannot answer under a pinned selector."
        )


def default_config_dir() -> Path:
    """The shipping NeMo ``config/`` directory (package-root sibling)."""
    return Path(__file__).resolve().parent.parent / "config"


def default_lock_path() -> Path:
    """The pod's resolved ``uv.lock`` (package root)."""
    return Path(__file__).resolve().parent.parent / _LOCK_FILENAME


def main() -> None:
    """Print both determinism values for pinning into the active config (regen)."""
    config_dir = default_config_dir()
    lock_path = default_lock_path()
    print(f"config_dir_digest: {compute_config_dir_digest(config_dir)}")
    print(f"uv_lock_sha256:    {compute_lock_sha256(lock_path)}")
    print(f"# config_dir:      {config_dir}")
    print(f"# uv.lock:         {lock_path}")


if __name__ == "__main__":
    main()
