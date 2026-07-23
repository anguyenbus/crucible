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

REGENERATE the pinned values (regen/audit mechanism) with::

    cd services/guardrail && uv run python -m app.config_digest            # print
    cd services/guardrail && uv run python -m app.config_digest --write-env # publish

``--write-env`` rewrites :func:`default_pin_env_path` — ``deploy/guardrail-pins.env``,
a committed dotenv carrying ``GUARDRAIL_EXPECTED_CONFIG_DIR_DIGEST`` and
``GUARDRAIL_EXPECTED_UV_LOCK_SHA256``. That file is the SINGLE GENERATED SOURCE of
the two pin values for every deploy surface: Docker Compose consumes it directly
via ``env_file:``, and a K8s ConfigMap is generated from the same bytes
(``kubectl create configmap --from-env-file``, or kustomize's
``configMapGenerator.envs``). Pin values are NEVER hand-copied into a deploy file
— a hand-copied value dies at the pod boundary the moment compose stops being the
deploy surface. It carries NO credentials; AWS credentials come from the ambient
chain.

The pod re-computes these live at startup and REFUSES to serve on mismatch (see
the ``app.main`` lifespan self-verification), so a drifted image cannot silently
answer under a pinned config.
"""

from __future__ import annotations

import hashlib
import sys
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

# The single GENERATED source of the two determinism pins, consumed by every
# deploy surface (compose `env_file:`, K8s ConfigMap). Package-root relative, and
# deliberately OUTSIDE config/ so publishing the pins cannot change the digest
# they pin.
_PIN_ENV_RELPATH: Final[str] = "deploy/guardrail-pins.env"

# The env-var names the pod reads in ``app.settings.get_settings``.
_PIN_ENV_CONFIG_DIR_DIGEST: Final[str] = "GUARDRAIL_EXPECTED_CONFIG_DIR_DIGEST"
_PIN_ENV_UV_LOCK_SHA256: Final[str] = "GUARDRAIL_EXPECTED_UV_LOCK_SHA256"


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


def default_pin_env_path() -> Path:
    """The committed dotenv that publishes both pins to every deploy surface."""
    return Path(__file__).resolve().parent.parent / _PIN_ENV_RELPATH


def render_pin_env(config_dir: str | Path, lock_path: str | Path) -> str:
    """
    Render the pin dotenv from the LIVE digests (the single generated source).

    Plain ``KEY=value`` lines with no quoting or interpolation, so the same bytes
    are valid for Docker Compose ``env_file:`` and for a K8s ConfigMap generated
    with ``--from-env-file``. NO credentials appear here — AWS credentials come
    from the ambient chain.

    Args:
        config_dir: The pod's NeMo ``config/`` directory.
        lock_path: Path to the resolved ``uv.lock``.

    Returns:
        The dotenv file contents.

    """
    return "\n".join(
        (
            "# GENERATED — do not edit by hand.",
            "# Regenerate: cd services/guardrail && "
            "uv run python -m app.config_digest --write-env",
            "#",
            "# The SINGLE source of the guardrail pod's two determinism pins. Every",
            "# deploy surface reads THESE bytes (compose `env_file:`; a K8s ConfigMap",
            "# generated with `--from-env-file`) — pin values are never hand-copied.",
            "# The pod recomputes both at startup and REFUSES to serve on mismatch.",
            "# Contains NO credentials: AWS credentials come from the ambient chain.",
            f"{_PIN_ENV_CONFIG_DIR_DIGEST}={compute_config_dir_digest(config_dir)}",
            f"{_PIN_ENV_UV_LOCK_SHA256}={compute_lock_sha256(lock_path)}",
            "",
        )
    )


def read_pin_env(pin_env_path: str | Path) -> dict[str, str]:
    """
    Parse the generated pin dotenv into a ``{env var: value}`` mapping.

    Args:
        pin_env_path: Path to ``deploy/guardrail-pins.env``.

    Returns:
        The declared pins, comments and blank lines discarded.

    Raises:
        FileNotFoundError: If the generated pin source is absent.

    """
    path = Path(pin_env_path)
    if not path.is_file():
        raise FileNotFoundError(f"generated pin source not found: {path}")
    pins: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        pins[key.strip()] = value.strip()
    return pins


def write_pin_env(
    *,
    config_dir: str | Path,
    lock_path: str | Path,
    pin_env_path: str | Path,
) -> Path:
    """Write the generated pin dotenv and return the path written."""
    path = Path(pin_env_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_pin_env(config_dir, lock_path), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> None:
    """
    Print both determinism values, and with ``--write-env`` publish them (regen).

    Args:
        argv: Command-line arguments (defaults to ``sys.argv[1:]``).

    """
    args = sys.argv[1:] if argv is None else argv
    config_dir = default_config_dir()
    lock_path = default_lock_path()
    print(f"config_dir_digest: {compute_config_dir_digest(config_dir)}")
    print(f"uv_lock_sha256:    {compute_lock_sha256(lock_path)}")
    print(f"# config_dir:      {config_dir}")
    print(f"# uv.lock:         {lock_path}")
    if "--write-env" in args:
        written = write_pin_env(
            config_dir=config_dir,
            lock_path=lock_path,
            pin_env_path=default_pin_env_path(),
        )
        print(f"# wrote pins:      {written}")


if __name__ == "__main__":
    main()
