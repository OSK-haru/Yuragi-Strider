"""Prepare scenario assumptions, then freeze them when the world is established."""

from threading import RLock

from core.attack_simulation.scenario_compiler import (
    ResolvedScenarioManifest,
    ScenarioCompiler,
    ScenarioOverride,
)
from core.attack_simulation.scenario_policy import ScenarioSession


class ScenarioEnvironment:
    """Preparation boundary; runtime/UI settings belong elsewhere."""

    def __init__(self, session: ScenarioSession) -> None:
        self._session = session
        self._overrides: dict[str, ScenarioOverride] = {}
        self._manifest: ResolvedScenarioManifest | None = None
        self._establishing = False
        self._lock = RLock()

    @property
    def session(self) -> ScenarioSession:
        """Read-only authoring identity; create another environment to replace it."""
        return self._session

    @property
    def established(self) -> bool:
        with self._lock:
            return self._manifest is not None

    def set_assumption(self, override: ScenarioOverride) -> None:
        with self._lock:
            if self._manifest is not None or self._establishing:
                raise RuntimeError("scenario assumptions are frozen during or after environment establishment")
            if not self.session.is_assumption_editable(override.path):
                raise PermissionError(f"scenario assumption is not editable in the selected mode: {override.path}")
            self._overrides[override.path] = override

    def establish(self, compiler: ScenarioCompiler | None = None) -> ResolvedScenarioManifest:
        # Preparation-only synchronization: no runtime, physical state or host
        # clock is advanced here. Publish a complete manifest exactly once.
        with self._lock:
            if self._manifest is not None:
                return self._manifest
            if self._establishing:
                raise RuntimeError("recursive environment establishment is not allowed")
            self._establishing = True
            try:
                manifest = (compiler or ScenarioCompiler()).compile(
                    self.session, overrides=tuple(self._overrides.values())
                )
                if not isinstance(manifest, ResolvedScenarioManifest):
                    raise TypeError("scenario compiler must produce a resolved manifest")
                self._manifest = manifest
                return manifest
            finally:
                # Failure leaves preparation editable; success retains freeze.
                self._establishing = False
