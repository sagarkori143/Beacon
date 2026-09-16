"""Architectural boundaries, enforced rather than documented.

Two rules from the design would decay silently without a test:

1. **Domain logic depends only on provider interfaces.** The moment a service
   imports a concrete provider, swapping that provider stops being a config
   change -- which is the entire premise of the architecture.

2. **No database session reaches the agent or a provider.** Passing an
   ``AsyncSession`` down there is how a transaction ends up held across an LLM
   call, which exhausts the connection pool under very ordinary load.

Both are checked by reading the source, so they hold for code nobody reviewed
carefully.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

#: Modules that may import concrete providers: the registry that builds them,
#: the providers themselves, and the composition root.
_WIRING_ALLOWED = (
    "app/providers/",
    "app/main.py",
    "app/workers/runner.py",
)

#: Interface-level modules within a provider family. `base` declares the
#: contract, `registry` maps a config type to a class, and `fusion` is the
#: score-combination strategy -- part of the search contract rather than any one
#: backend's implementation, since the retrieval service applies the same
#: strategy across query rewrites that the provider applies inside its SQL.
_INTERFACE_MODULES = ("base", "registry", "fusion")

_CONCRETE_PROVIDER = re.compile(
    r"^app\.providers\.(llm|embeddings|vector_store|search|ocr|storage|queue)\."
    r"(?!" + "|".join(f"{m}$" for m in _INTERFACE_MODULES) + r")"
)


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(APP_ROOT.parent).with_suffix("").parts)


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
        elif isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
    return names


def _source_files(*subdirs: str) -> list[Path]:
    files: list[Path] = []
    for subdir in subdirs:
        files.extend((APP_ROOT / subdir).rglob("*.py"))
    return [f for f in files if f.name != "__init__.py"]


class TestProviderIsolation:
    def test_services_and_tools_import_only_provider_interfaces(self) -> None:
        violations: list[str] = []

        for path in _source_files("services", "tools", "repositories", "api"):
            relative = path.relative_to(APP_ROOT.parent).as_posix()
            if any(relative.startswith(allowed) for allowed in _WIRING_ALLOWED):
                continue
            for imported in _imports(path):
                if _CONCRETE_PROVIDER.match(imported):
                    violations.append(f"{relative} imports {imported}")

        assert not violations, (
            "Domain code must depend on provider interfaces (base.py), not "
            "implementations:\n  " + "\n  ".join(violations)
        )

    def test_every_provider_family_exposes_a_base_module(self) -> None:
        for family in (
            "llm",
            "embeddings",
            "vector_store",
            "search",
            "ocr",
            "storage",
            "queue",
        ):
            assert (APP_ROOT / "providers" / family / "base.py").exists(), (
                f"provider family '{family}' has no interface module"
            )


class TestTransactionBoundaries:
    def test_no_session_reaches_the_agent_or_providers(self) -> None:
        """Only repositories, the pipeline and the search/vector providers --
        which are handed a session by their caller -- may name AsyncSession."""
        allowed_prefixes = (
            "app/repositories/",
            "app/providers/search/",
            "app/providers/vector_store/",
            "app/services/ingestion/",
            "app/services/documents/",
            "app/services/auth/",
            "app/core/db.py",
        )
        violations: list[str] = []

        for path in _source_files("services", "tools", "api"):
            relative = path.relative_to(APP_ROOT.parent).as_posix()
            if any(relative.startswith(p) for p in allowed_prefixes):
                continue
            if "AsyncSession" in path.read_text(encoding="utf-8"):
                violations.append(relative)

        assert not violations, (
            "These modules should take a UnitOfWork, not an AsyncSession -- "
            "holding a transaction across a provider call exhausts the pool:\n  "
            + "\n  ".join(violations)
        )


class TestSecretHygiene:
    def test_no_module_reads_the_environment_directly(self) -> None:
        """Configuration has exactly one entry point.

        Scattered ``os.environ`` reads are how a setting ends up documented in
        one place and honoured in another.
        """
        allowed = {"app/core/config.py", "app/core/logging.py"}
        violations: list[str] = []

        for path in APP_ROOT.rglob("*.py"):
            relative = path.relative_to(APP_ROOT.parent).as_posix()
            if relative in allowed:
                continue
            source = path.read_text(encoding="utf-8")
            if re.search(r"os\.environ|os\.getenv", source):
                violations.append(relative)

        assert not violations, (
            "Read configuration through app.core.config, not os.environ:\n  "
            + "\n  ".join(violations)
        )

    def test_tenant_context_is_only_ever_set_transaction_locally(self) -> None:
        """``set_config(..., false)`` sets the value for the whole connection.

        That connection then returns to the pool still carrying it, and the next
        request -- for a different tenant -- inherits it. ROLLBACK does not clear
        a session-level SET, so pool recycling does not save you. Every call site
        must pass ``true``.
        """
        pattern = re.compile(r"set_config\s*\([^)]*?,\s*(?:false|False)\s*\)", re.S)
        violations = [
            path.relative_to(APP_ROOT.parent).as_posix()
            for path in APP_ROOT.rglob("*.py")
            if pattern.search(path.read_text(encoding="utf-8"))
        ]
        assert not violations, (
            "set_config must be transaction-local (is_local=true):\n  " + "\n  ".join(violations)
        )

    def test_no_hardcoded_credentials_in_application_code(self) -> None:
        suspicious = re.compile(
            r"""(?:api_key|password|secret|token)\s*=\s*["'](?!.*\{)[A-Za-z0-9_\-]{16,}["']""",
            re.IGNORECASE,
        )
        violations = [
            f"{path.relative_to(APP_ROOT.parent).as_posix()}"
            for path in APP_ROOT.rglob("*.py")
            if suspicious.search(path.read_text(encoding="utf-8"))
        ]
        assert not violations, f"possible hardcoded credential: {violations}"


class TestCredentialBoundary:
    """The two credential kinds must not drift into each other.

    A platform operator provisions tenants; a tenant principal reads tenant
    data. The moment one endpoint accepts both, "no credential can see two
    organizations' data" stops being true -- and nothing would fail loudly,
    because both tokens are valid JWTs signed with the same key.
    """

    #: Not authentication boundaries: login and refresh exchange credentials,
    #: so requiring one is circular.
    _UNAUTHENTICATED = frozenset({"login", "refresh"})

    def test_every_platform_endpoint_requires_an_operator(self) -> None:
        from app.api.v1 import platform

        unguarded = [
            name
            for name, fn in vars(platform).items()
            if callable(fn)
            and getattr(fn, "__module__", None) == platform.__name__
            and name not in {"current_operator"}
            and name not in self._UNAUTHENTICATED
            and "operator" not in getattr(fn, "__annotations__", {})
        ]
        assert not unguarded, (
            "platform endpoints must take the operator dependency:\n  " + "\n  ".join(unguarded)
        )

    def test_only_the_public_router_builds_a_public_principal(self) -> None:
        """Anonymous scope has exactly one entry point.

        `public_principal` is the single place where tenant scope comes from a
        request parameter instead of a token. That is a deliberate, reviewed
        exception for the public site. If a second module starts constructing
        one, "scope comes from the token" has quietly stopped being true
        everywhere else, and nothing would fail to say so.
        """
        allowed = {"app/api/v1/public.py", "app/core/tenancy.py"}
        offenders = [
            path.relative_to(APP_ROOT.parent).as_posix()
            for path in APP_ROOT.rglob("*.py")
            if path.relative_to(APP_ROOT.parent).as_posix() not in allowed
            and re.search(r"public_principal|PUBLIC_USER_ID", path.read_text("utf-8"))
        ]
        assert not offenders, (
            "only the public router may construct an anonymous principal:\n  "
            + "\n  ".join(offenders)
        )

    def test_no_tenant_endpoint_accepts_a_platform_credential(self) -> None:
        """Only api/v1/platform.py may resolve a platform token."""
        offenders = [
            path.relative_to(APP_ROOT.parent).as_posix()
            for path in (APP_ROOT / "api").rglob("*.py")
            if path.name != "platform.py"
            and re.search(r"decode_platform_token|PlatformPrincipal", path.read_text("utf-8"))
        ]
        assert not offenders, (
            "platform credentials must stay inside api/v1/platform.py:\n  " + "\n  ".join(offenders)
        )
