# Rust API migration contract

The API server is a separate migration phase from the numerical core. The web
client keeps the existing HTTP paths and JSON contract while route ownership
moves from FastAPI to Rust. FastAPI remains the production entry point until the
Rust service covers every mounted route and preserves authentication, budgets,
security headers, CORS, job lifecycle, and WebSocket behavior.

## First route slice

`GET /api/health` is implemented in `rust_kernels/yuragi_strider_api`. Its
response is frozen in `contracts/api-health-v1.json` and checked against both
the current FastAPI handler and the Axum route. The Rust route also carries the
API security headers and the configured `ALLOWED_ORIGINS` CORS policy. This is
a parallel contract-tested implementation; deployment still serves the route
from FastAPI.

The shared fixture is intentionally the wire contract, not a second source for
runtime capability declarations. If the pulse capability catalog changes, the
FastAPI and Rust route tests must both be updated with the reviewed contract.

## Circuit compile preview slice

The Axum server also implements `POST /api/circuit/compile` for both
`logical_direct` and `auto_decompose`, including the `bell`, `teleportation`,
and `bit_flip_repetition` presets. The direct bell response and a composite
auto-decomposition response covering all eight rewrite rules are frozen in
shared contract fixtures and checked by Python and Rust tests. The route
validates the bounded circuit shape before returning a preview.

FastAPI continues to serve the production route. Axum is still a parallel
implementation, not wired into production traffic. Exact Pydantic validation
and error parity and the remaining API routes still need migration.

## Cutover rule

Move routes by cohesive behavior groups, with a contract fixture and focused
parity tests for each group. Keep the current service deployed until the Rust
server can host the complete surface. Move authentication, origin/host checks,
resource limits, scheduling, job state, and WebSockets before replacing the
FastAPI process. Do not route only a subset of production traffic to two
independent job/state stores.

The final Rust service should use the interpreter-independent core directly.
Python may be loaded only for an explicitly selected extension, with the data
it needs collected into coarse-grained PyO3 calls. Python reference paths remain
available in tests and validation, not as an implicit production fallback.
