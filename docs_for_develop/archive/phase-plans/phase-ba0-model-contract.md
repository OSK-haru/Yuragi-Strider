# BA-0: Model Contract and Regression Guard

## Implementation Status

Status: complete on 2026-07-23.

> BA-0 intentionally froze a `501 Not Implemented` boundary before the solver
> existed. BA-6 later replaced that boundary with the validated functional
> endpoint. The item below is historical phase evidence, not current API
> behavior.

Implemented:

- Fixed two-level basis, $\sigma_y$, detuning, phase, and unit constants
- Added `driven_two_level_rwa_experimental_v1` as a separate pulse capability
- Added strict `physical` and `direct_rates` pulse request schemas
- Added Gaussian-derived and square explicit-duration validation
- Declared `/api/pulse/simulate` with an explicit `501 Not Implemented` boundary
- Preserved the existing gate model set and `/api/simulate` behavior

Verified:

```text
BA-0 and nearby API tests: 27 passed
V1-V7 validations: 36 passed
Full Python suite: 341 passed
Frontend production build: passed
git diff --check: passed
```

## 1. Goal

Fix the two-level pulse model's physical conventions, units, model identity,
and request validation before adding a time-dependent solver.

## 2. Prerequisites

- Existing gate-level model and V1-V7 validations pass.
- Current `/api/simulate` behavior is recorded as the regression baseline.

## 3. In Scope

- Two-level basis and Pauli matrices, including $\sigma_y$
- Rotating-frame and RWA labels
- Detuning and phase sign conventions
- Internal unit conversions
- Model ID registration
- Pulse request schema
- Mutually exclusive environment input modes
- Regression tests protecting the existing constant path and API

## 4. Out of Scope

- Time-dependent integration
- Pulse envelope evaluation
- Dissipative pulse execution
- Pulse Lab UI
- Qutrit, leakage, and DRAG

## 5. Fixed Contract

Use:

$$
|0\rangle=
\begin{pmatrix}1\\0\end{pmatrix},
\qquad
|1\rangle=
\begin{pmatrix}0\\1\end{pmatrix},
$$

$$
\sigma_y=
\begin{pmatrix}
0&-i\\
i&0
\end{pmatrix},
\qquad
\Delta=\omega_d-\omega_q.
$$

The internal units are:

| Quantity | Unit |
|---|---|
| Time | $\mu\mathrm{s}$ |
| $\Omega$, $\Delta$, $\omega_d$, $\omega_q$ | $\mathrm{rad}/\mu\mathrm{s}$ |
| Lindblad rates | $1/\mu\mathrm{s}$ |
| Phase | radians |

Frequency input in MHz is converted by:

$$
f\ [\mathrm{MHz}]
\longrightarrow
2\pi f\ [\mathrm{rad}/\mu\mathrm{s}].
$$

## 6. API Contract Work

Define the `/api/pulse/simulate` request contract without changing
`/api/simulate`.

The environment must use a discriminated mode:

```text
input_mode: physical
```

or:

```text
input_mode: direct_rates
```

Fields from the inactive mode must be rejected rather than silently ignored.
Pulse duration and total observation duration must remain distinct fields.

If the numerical endpoint is not activated in this phase, keep the schema
testable at the model level and activate the route only after the solver is
available. Do not expose a placeholder endpoint that returns a simulated
success result.

## 7. Implementation Checklist

- Add `driven_two_level_rwa_experimental_v1` to model capabilities.
- Define reusable pulse request and internal parameter types.
- Centralize MHz-to-rad/us conversion.
- Document the positive phase direction as $+x$ toward $+y$.
- Document the detuning sign with at least one positive and one negative case.
- Add API validation for incompatible or missing mode fields.
- Record the existing `/api/simulate` response contract before pulse changes.

## 8. Tests

- Exact $\sigma_y$ entries and Hermiticity
- $\phi=0,\pi/2,\pi,-\pi/2$ axis conventions
- Positive and negative detuning conversion
- MHz, GHz, rad/us, and microsecond conversion checks
- `physical` request acceptance
- `direct_rates` request acceptance
- Mixed-mode field rejection
- Missing-mode field rejection
- Existing `/api/simulate` contract regression
- V1-V7 regression

## 9. Completion Criteria

- Physics and unit conventions are represented by tests, not comments alone.
- The model ID and experimental label are fixed.
- Pulse input modes are unambiguous.
- No production pulse result is returned before a numerical path exists.
- Existing gate-level API responses and V1-V7 results are unchanged.
