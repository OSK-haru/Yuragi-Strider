"""Assemble the opt-in attack-simulator API surface for local products."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter

from api.attack_live_websocket import (
    PausedWorldWebSocketRegistry,
    create_live_attack_websocket_router,
)
from api.attack_product_factory import ProductAssemblyLimits, ProductEnvironmentFactory
from api.attack_device_snapshot import load_ibm_kingston_snapshot
from api.attack_challenge import reset_leakage_challenge, reset_leakage_message_challenge
from api.attack_crosstalk_challenge import crosstalk_grover_challenge
from api.attack_product_profiles import product_free_scenarios, standard_snapshot_initial_values
from api.attack_sessions import SessionHostLimits
from api.attack_settings_resolver import ScenarioSettingsResolver, create_settings_catalog_router
from api.attack_web_ownership import (
    BrowserEnvironmentManager,
    WebOwnershipSettings,
    create_browser_environment_router,
)
from core.attack_simulation.qpu_topology import TopologyLimits
from core.attack_simulation.request_receipts import ReceiptLimits


@dataclass(frozen=True)
class AttackSimulatorRuntime:
    router: APIRouter
    settings: WebOwnershipSettings
    resolver: ScenarioSettingsResolver
    registry: PausedWorldWebSocketRegistry
    owner_manager: BrowserEnvironmentManager


def build_attack_simulator_runtime(settings: WebOwnershipSettings) -> AttackSimulatorRuntime:
    """Build all dependencies before returning one router for atomic mounting."""
    if type(settings) is not WebOwnershipSettings:
        raise TypeError("explicit web ownership settings are required")
    # About 10k jobs fit in one environment's hour at ~0.35 s each; their keys
    # stay for the whole lifetime. Records: 128 retained + 8 queued + the
    # running job + jobs named by up to 16 pending requests <= 160.
    limits = ProductAssemblyLimits(
        session=SessionHostLimits(16384, 4, ReceiptLimits(16, 64), max_queued_jobs=8),
        topology=TopologyLimits(256, 2048, 4096),
        max_runtime_jobs=160,
        max_events_per_job=1100,
        max_unresolved_events=32,
        max_workload_pending=16,
        max_workload_history=64,
        retained_jobs=128,
    )
    snapshot = load_ibm_kingston_snapshot()
    resolver = ScenarioSettingsResolver(
        free_scenarios=product_free_scenarios(snapshot),
        challenges=(reset_leakage_message_challenge(snapshot), reset_leakage_challenge(snapshot),
                    crosstalk_grover_challenge(snapshot)),
        standard_initial_values=standard_snapshot_initial_values(snapshot),
    )
    factory = ProductEnvironmentFactory(limits)
    registry = PausedWorldWebSocketRegistry(max_worlds=settings.max_environments_total)
    owner_manager = BrowserEnvironmentManager(
        registry,
        request_environment_factory=resolver.bind_product_factory(factory),
        settings=settings,
    )
    router = APIRouter()
    router.include_router(create_settings_catalog_router(resolver, settings))
    router.include_router(create_browser_environment_router(owner_manager))
    router.include_router(create_live_attack_websocket_router(
        registry,
        resolve_owner=owner_manager.resolve_owner,
        allowed_origins=settings.allowed_origins,
    ))
    return AttackSimulatorRuntime(router, settings, resolver, registry, owner_manager)
