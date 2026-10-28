use gemm::{Parallelism, c64, gemm};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::collections::HashMap;

const RESET_SEQUENCE_SCHEMA_VERSION: &str = "local_reset_sequence_v1";
const BINARY_CONTRAST_SCHEMA_VERSION: &str = "security_binary_contrast_v1";
const BINARY_CONTRAST_MAX_EXACT_COUNT: u64 = 1u64 << 53;
const CROSSTALK_EVALUATION_SCHEMA_VERSION: &str = "crosstalk_disruption_v1";

#[pyfunction]
fn backend_name() -> &'static str {
    "rust_dense_preview"
}

#[pyfunction]
fn add_f64(a: f64, b: f64) -> f64 {
    a + b
}

#[pyfunction]
fn matmul_complex_flat(a: Vec<f64>, b: Vec<f64>, d: usize) -> PyResult<Vec<f64>> {
    let element_count = matrix_element_count(d)?;
    validate_matrix_len("a", a.len(), element_count, d)?;
    validate_matrix_len("b", b.len(), element_count, d)?;
    Ok(matmul_flat(&a, &b, d))
}

#[pyfunction]
fn lindblad_rhs_flat(
    rho: Vec<f64>,
    h: Vec<f64>,
    collapse_ops: Vec<f64>,
    num_ops: usize,
    d: usize,
) -> PyResult<Vec<f64>> {
    let element_count = matrix_element_count(d)?;
    validate_matrix_len("rho", rho.len(), element_count, d)?;
    validate_matrix_len("h", h.len(), element_count, d)?;
    let collapse_len = num_ops
        .checked_mul(element_count)
        .ok_or_else(|| PyValueError::new_err("collapse operator count is too large"))?;
    if collapse_ops.len() != collapse_len {
        return Err(PyValueError::new_err(format!(
            "collapse_ops length must be num_ops * 2 * d * d; received len(collapse_ops)={} for num_ops={} and d={}",
            collapse_ops.len(),
            num_ops,
            d
        )));
    }

    let (collapse_adjoints, collapse_products) =
        prepare_collapse_operators_raw(&collapse_ops, num_ops, d);
    Ok(lindblad_rhs_raw(
        &rho,
        &h,
        &collapse_ops,
        &collapse_adjoints,
        &collapse_products,
        num_ops,
        d,
        is_zero_matrix(&h),
    ))
}

#[pyfunction]
fn gksl_exponential_superoperator_flat(
    h: Vec<f64>,
    collapse_ops: Vec<f64>,
    num_ops: usize,
    d: usize,
    duration_us: f64,
) -> PyResult<Vec<f64>> {
    let element_count = matrix_element_count(d)?;
    validate_matrix_len("h", h.len(), element_count, d)?;
    validate_collapse_ops_len(collapse_ops.len(), num_ops, element_count, d)?;
    validate_finite_matrix("h", &h)?;
    validate_finite_matrix("collapse_ops", &collapse_ops)?;
    validate_hermitian(&h, d)?;
    if !duration_us.is_finite() || duration_us < 0.0 {
        return Err(PyValueError::new_err(
            "duration_us must be finite and non-negative",
        ));
    }

    let (collapse_adjoints, collapse_products) =
        prepare_collapse_operators_raw(&collapse_ops, num_ops, d);
    let generator = gksl_liouvillian_superoperator_raw(
        &h,
        &collapse_ops,
        &collapse_adjoints,
        &collapse_products,
        num_ops,
        d,
    );
    matrix_exponential_pade13(&scale_flat(&generator, duration_us), d * d)
        .map_err(PyValueError::new_err)
}

#[pyfunction]
fn gksl_piecewise_superoperator_flat(
    hamiltonians: Vec<f64>,
    interval_durations_us: Vec<f64>,
    num_intervals: usize,
    collapse_ops: Vec<f64>,
    num_ops: usize,
    d: usize,
) -> PyResult<Vec<f64>> {
    if num_intervals == 0 {
        return Err(PyValueError::new_err(
            "num_intervals must be greater than 0",
        ));
    }
    if interval_durations_us.len() != num_intervals {
        return Err(PyValueError::new_err(
            "interval_durations_us length must equal num_intervals",
        ));
    }
    let element_count = matrix_element_count(d)?;
    let expected_hamiltonian_len = num_intervals
        .checked_mul(element_count)
        .ok_or_else(|| PyValueError::new_err("interval count is too large"))?;
    if hamiltonians.len() != expected_hamiltonian_len {
        return Err(PyValueError::new_err(
            "hamiltonians length must equal num_intervals * 2 * d * d",
        ));
    }
    validate_collapse_ops_len(collapse_ops.len(), num_ops, element_count, d)?;
    validate_finite_matrix("hamiltonians", &hamiltonians)?;
    validate_finite_matrix("collapse_ops", &collapse_ops)?;

    let superoperator_dimension = d * d;
    let mut composed = identity_flat(superoperator_dimension);
    let (collapse_adjoints, collapse_products) =
        prepare_collapse_operators_raw(&collapse_ops, num_ops, d);
    for (interval_index, duration) in interval_durations_us.iter().enumerate() {
        if !duration.is_finite() || *duration <= 0.0 {
            return Err(PyValueError::new_err(
                "interval durations must be finite and positive",
            ));
        }
        let start = interval_index * element_count;
        let end = start + element_count;
        let hamiltonian = &hamiltonians[start..end];
        validate_hermitian(hamiltonian, d)?;
        let generator = gksl_liouvillian_superoperator_raw(
            hamiltonian,
            &collapse_ops,
            &collapse_adjoints,
            &collapse_products,
            num_ops,
            d,
        );
        let interval_map =
            matrix_exponential_pade13(&scale_flat(&generator, *duration), superoperator_dimension)
                .map_err(PyValueError::new_err)?;
        composed = matmul_flat(&interval_map, &composed, superoperator_dimension);
    }
    Ok(composed)
}

#[pyfunction]
fn rk4_evolve_flat(
    rho: Vec<f64>,
    h: Vec<f64>,
    collapse_ops: Vec<f64>,
    num_ops: usize,
    d: usize,
    dt: f64,
    substeps: usize,
) -> PyResult<Vec<f64>> {
    let element_count = matrix_element_count(d)?;
    validate_matrix_len("rho", rho.len(), element_count, d)?;
    validate_matrix_len("h", h.len(), element_count, d)?;
    validate_collapse_ops_len(collapse_ops.len(), num_ops, element_count, d)?;
    if substeps == 0 {
        return Err(PyValueError::new_err("substeps must be greater than 0"));
    }
    if !dt.is_finite() {
        return Err(PyValueError::new_err("dt must be finite"));
    }

    let (collapse_adjoints, collapse_products) =
        prepare_collapse_operators_raw(&collapse_ops, num_ops, d);
    let hamiltonian_is_zero = is_zero_matrix(&h);
    let mut evolved = rho;
    for _ in 0..substeps {
        evolved = rk4_step_raw(
            &evolved,
            &h,
            &collapse_ops,
            &collapse_adjoints,
            &collapse_products,
            num_ops,
            d,
            dt,
            hamiltonian_is_zero,
        );
    }

    Ok(evolved)
}

#[pyfunction]
fn apply_local_reset_sequence_flat(
    schema_version: &str,
    rho: Vec<f64>,
    subsystem_count: usize,
    local_dimension: usize,
    targets: Vec<usize>,
    success_probabilities: Vec<f64>,
    target_populations: Vec<f64>,
    reset_count: usize,
) -> PyResult<Vec<f64>> {
    if schema_version != RESET_SEQUENCE_SCHEMA_VERSION {
        return Err(PyValueError::new_err(format!(
            "unsupported reset sequence schema version: {schema_version}"
        )));
    }
    let mut state =
        yuragi_strider_core::reset::ResetState::new(rho, subsystem_count, local_dimension)
            .map_err(PyValueError::new_err)?;
    let operation = yuragi_strider_core::reset::ResetOperation {
        targets,
        success_probabilities,
        target_populations,
        reset_count,
    };
    state.execute(&[operation]).map_err(PyValueError::new_err)?;
    Ok(state.values().to_vec())
}

/// A Rust-owned density matrix; a whole reset schedule crosses PyO3 once.
#[pyclass(name = "ResetSession")]
struct PyResetSession {
    state: yuragi_strider_core::reset::ResetState,
}

#[pymethods]
impl PyResetSession {
    #[new]
    fn new(
        schema_version: &str,
        rho: Vec<f64>,
        subsystem_count: usize,
        local_dimension: usize,
    ) -> PyResult<Self> {
        if schema_version != "local_reset_state_v1" {
            return Err(PyValueError::new_err(
                "unsupported reset state schema version",
            ));
        }
        Ok(Self {
            state: yuragi_strider_core::reset::ResetState::new(
                rho,
                subsystem_count,
                local_dimension,
            )
            .map_err(PyValueError::new_err)?,
        })
    }

    fn execute(
        &mut self,
        schema_version: &str,
        operations: Vec<(Vec<usize>, Vec<f64>, Vec<f64>, usize)>,
    ) -> PyResult<()> {
        if schema_version != "local_reset_batch_v1" {
            return Err(PyValueError::new_err(
                "unsupported reset batch schema version",
            ));
        }
        let operations = operations
            .into_iter()
            .map(
                |(targets, success_probabilities, target_populations, reset_count)| {
                    yuragi_strider_core::reset::ResetOperation {
                        targets,
                        success_probabilities,
                        target_populations,
                        reset_count,
                    }
                },
            )
            .collect::<Vec<_>>();
        self.state
            .execute(&operations)
            .map_err(PyValueError::new_err)
    }

    fn state_flat(&self) -> Vec<f64> {
        self.state.values().to_vec()
    }
}

/// Batched qubit register operations, with density state kept in the Rust core.
#[pyclass(name = "RegisterSession")]
struct PyRegisterSession {
    state: yuragi_strider_core::register::DensityRegister,
}

#[pymethods]
impl PyRegisterSession {
    #[new]
    fn new(schema_version: &str, rho: Vec<f64>, qubits: usize) -> PyResult<Self> {
        if schema_version != "register_state_v1" {
            return Err(PyValueError::new_err(
                "unsupported register state schema version",
            ));
        }
        Ok(Self {
            state: yuragi_strider_core::register::DensityRegister::new(rho, qubits)
                .map_err(PyValueError::new_err)?,
        })
    }

    /// Each tuple is (kind, operands, data, scalar); all records are one atomic batch.
    fn execute(
        &mut self,
        schema_version: &str,
        operations: Vec<(String, Vec<usize>, Vec<f64>, f64)>,
    ) -> PyResult<Vec<Vec<usize>>> {
        let converted = parse_register_batch(schema_version, operations)?;
        let measurements = self
            .state
            .execute(&converted)
            .map_err(PyValueError::new_err)?;
        Ok(measurements
            .into_iter()
            .map(|bits| bits.into_iter().map(usize::from).collect())
            .collect())
    }

    fn execute_with_checkpoints(
        &mut self,
        schema_version: &str,
        operations: Vec<(String, Vec<usize>, Vec<f64>, f64)>,
        checkpoints: Vec<usize>,
    ) -> PyResult<(Vec<Vec<usize>>, Vec<Vec<f64>>)> {
        let converted = parse_register_batch(schema_version, operations)?;
        let (measurements, snapshots) = self
            .state
            .execute_with_checkpoints(&converted, &checkpoints)
            .map_err(PyValueError::new_err)?;
        Ok((
            measurements
                .into_iter()
                .map(|bits| bits.into_iter().map(usize::from).collect())
                .collect(),
            snapshots,
        ))
    }

    fn state_flat(&self) -> Vec<f64> {
        self.state.values().to_vec()
    }
}

fn parse_register_batch(
    schema_version: &str,
    operations: Vec<(String, Vec<usize>, Vec<f64>, f64)>,
) -> PyResult<Vec<yuragi_strider_core::register::RegisterOperation>> {
    use yuragi_strider_core::register::{IdleSpec, RegisterOperation};
    use yuragi_strider_core::reset::ResetOperation;
    if schema_version != "register_batch_v1" {
        return Err(PyValueError::new_err(
            "unsupported register batch schema version",
        ));
    }
    let mut converted = Vec::with_capacity(operations.len());
    for (kind, qubits, data, scalar) in operations {
        let operation = match kind.as_str() {
            "unitary" if scalar == 0.0 => RegisterOperation::Unitary {
                qubits,
                matrix: data,
            },
            "reset" if scalar == 0.0 && data.len() == qubits.len().saturating_mul(3) => {
                let mut probabilities = Vec::with_capacity(qubits.len());
                let mut populations = Vec::with_capacity(qubits.len() * 2);
                for values in data.chunks_exact(3) {
                    probabilities.push(values[0]);
                    populations.extend_from_slice(&values[1..3]);
                }
                RegisterOperation::Reset(ResetOperation {
                    targets: qubits,
                    success_probabilities: probabilities,
                    target_populations: populations,
                    reset_count: 1,
                })
            }
            "depolarize" if data.is_empty() => RegisterOperation::Depolarize {
                qubits,
                probability: scalar,
            },
            "dephase" if data.is_empty() && scalar == 0.0 => RegisterOperation::Dephase { qubits },
            "idle" if data.len() == qubits.len().saturating_mul(3) => {
                let specs = data
                    .chunks_exact(3)
                    .map(|values| IdleSpec {
                        t1_us: values[0],
                        tphi_us: values[1],
                        equilibrium_excited_population: values[2],
                    })
                    .collect();
                RegisterOperation::Idle {
                    qubits,
                    specs,
                    elapsed_us: scalar,
                }
            }
            "measure" if data.is_empty() => RegisterOperation::Measure {
                qubits,
                draw: scalar,
            },
            _ => {
                return Err(PyValueError::new_err(
                    "unknown or malformed register operation",
                ));
            }
        };
        converted.push(operation);
    }
    Ok(converted)
}

#[pyfunction]
fn binary_contrast_statistics_v1(
    schema_version: &str,
    observation_source: &str,
    trials_0: u64,
    outcome_one_0: u64,
    trials_1: u64,
    outcome_one_1: u64,
) -> PyResult<(f64, f64, f64, f64, Option<f64>, f64, f64)> {
    if schema_version != BINARY_CONTRAST_SCHEMA_VERSION {
        return Err(PyValueError::new_err(format!(
            "unsupported binary contrast schema version: {schema_version}"
        )));
    }
    if !matches!(observation_source, "actor_visible" | "service_published") {
        return Err(PyValueError::new_err(
            "binary analysis requires an approved observation source",
        ));
    }
    if trials_0 == 0 || trials_1 == 0 {
        return Err(PyValueError::new_err("class trial counts must be positive"));
    }
    if trials_0 > BINARY_CONTRAST_MAX_EXACT_COUNT || trials_1 > BINARY_CONTRAST_MAX_EXACT_COUNT {
        return Err(PyValueError::new_err(
            "class trial counts must be exactly representable as f64",
        ));
    }
    if outcome_one_0 > trials_0 || outcome_one_1 > trials_1 {
        return Err(PyValueError::new_err(
            "outcome-one counts cannot exceed class trial counts",
        ));
    }

    let (p0, p1, standard_error) =
        binary_group_statistics(outcome_one_0, trials_0, outcome_one_1, trials_1);
    let difference = p1 - p0;
    let z_score = if standard_error > 0.0 {
        Some(difference / standard_error)
    } else {
        None
    };
    let mean_probability = (p0 + p1) / 2.0;
    let mutual_information =
        binary_entropy(mean_probability) - 0.5 * binary_entropy(p0) - 0.5 * binary_entropy(p1);
    let bayes_accuracy = 0.5 * (p0.max(p1) + (1.0 - p0).max(1.0 - p1));

    Ok((
        p0,
        p1,
        difference,
        standard_error,
        z_score,
        mutual_information,
        bayes_accuracy,
    ))
}

/// Pool public labelled reset-training observations and compute the shared
/// contrast plus threshold fit in one coarse PyO3 call.
#[pyfunction]
fn reset_training_analysis_v1<'py>(
    py: Python<'py>,
    schema_version: &str,
    observations: Vec<(u8, u64, u64)>,
) -> PyResult<Bound<'py, PyDict>> {
    if schema_version != BINARY_CONTRAST_SCHEMA_VERSION {
        return Err(PyValueError::new_err(
            "unsupported reset analysis schema version",
        ));
    }
    let mut sample_count = [0u64; 2];
    let mut shots = [0u64; 2];
    let mut ones = [0u64; 2];
    for (label, trial_count, outcome_one_count) in observations {
        if label > 1
            || trial_count == 0
            || outcome_one_count > trial_count
            || trial_count > BINARY_CONTRAST_MAX_EXACT_COUNT
        {
            return Err(PyValueError::new_err(
                "invalid labelled reset training observation",
            ));
        }
        let label = label as usize;
        sample_count[label] = sample_count[label]
            .checked_add(1)
            .ok_or_else(|| PyValueError::new_err("observation count overflow"))?;
        shots[label] = shots[label]
            .checked_add(trial_count)
            .ok_or_else(|| PyValueError::new_err("shot count overflow"))?;
        ones[label] = ones[label]
            .checked_add(outcome_one_count)
            .ok_or_else(|| PyValueError::new_err("outcome count overflow"))?;
    }
    if sample_count.contains(&0) || shots.contains(&0) {
        return Err(PyValueError::new_err(
            "training requires observations for both labels",
        ));
    }
    if shots
        .iter()
        .any(|count| *count > BINARY_CONTRAST_MAX_EXACT_COUNT)
    {
        return Err(PyValueError::new_err(
            "training shot counts exceed the exact count range",
        ));
    }
    let (p0, p1, standard_error) = binary_group_statistics(ones[0], shots[0], ones[1], shots[1]);
    let threshold = (p0 + p1) / 2.0;
    let difference = p1 - p0;
    let z_score = if standard_error > 0.0 {
        Some(difference / standard_error)
    } else {
        None
    };
    let mutual_information =
        binary_entropy((p0 + p1) / 2.0) - 0.5 * binary_entropy(p0) - 0.5 * binary_entropy(p1);
    let bayes_accuracy = 0.5 * (p0.max(p1) + (1.0 - p0).max(1.0 - p1));
    let result = PyDict::new(py);
    result.set_item("observation_count_by_label", sample_count.to_vec())?;
    result.set_item("shots_by_label", shots.to_vec())?;
    result.set_item("outcome_one_by_label", ones.to_vec())?;
    result.set_item("threshold", threshold)?;
    result.set_item("predict_one_when_at_or_above", p1 > p0)?;
    result.set_item("probability_one_by_class", vec![p0, p1])?;
    result.set_item("difference_class_1_minus_class_0", difference)?;
    result.set_item("standard_error", standard_error)?;
    result.set_item("z_score", z_score)?;
    result.set_item("balanced_prior_mutual_information_bits", mutual_information)?;
    result.set_item("balanced_prior_bayes_accuracy", bayes_accuracy)?;
    Ok(result)
}

fn binary_group_statistics(
    successes_0: u64,
    trials_0: u64,
    successes_1: u64,
    trials_1: u64,
) -> (f64, f64, f64) {
    let p0 = successes_0 as f64 / trials_0 as f64;
    let p1 = successes_1 as f64 / trials_1 as f64;
    let standard_error =
        (p0 * (1.0 - p0) / trials_0 as f64 + p1 * (1.0 - p1) / trials_1 as f64).sqrt();
    (p0, p1, standard_error)
}

fn binary_entropy(probability: f64) -> f64 {
    let mut entropy = 0.0;
    if probability > 0.0 {
        entropy -= probability * probability.log2();
    }
    let complement = 1.0 - probability;
    if complement > 0.0 {
        entropy -= complement * complement.log2();
    }
    entropy
}

/// Evaluate an entire private workload without returning its intermediate counts.
#[pyfunction]
fn evaluate_crosstalk_disruption_v1(
    schema_version: &str,
    runs: Vec<HashMap<String, u64>>,
    success_outcome: &str,
    baseline_successes: u64,
    baseline_shots: u64,
    significance_z: f64,
    minimum_drop: f64,
) -> PyResult<bool> {
    if schema_version != CROSSTALK_EVALUATION_SCHEMA_VERSION {
        return Err(PyValueError::new_err(
            "unsupported crosstalk evaluation schema",
        ));
    }
    if runs.is_empty()
        || success_outcome.is_empty()
        || baseline_shots == 0
        || baseline_shots > BINARY_CONTRAST_MAX_EXACT_COUNT
        || baseline_successes > baseline_shots
        || !significance_z.is_finite()
        || !minimum_drop.is_finite()
    {
        return Err(PyValueError::new_err("invalid crosstalk evaluation input"));
    }
    let mut attacked_successes = 0u64;
    let mut attacked_shots = 0u64;
    for run in &runs {
        for (outcome, count) in run {
            attacked_shots = attacked_shots
                .checked_add(*count)
                .ok_or_else(|| PyValueError::new_err("shot count overflow"))?;
            if outcome == success_outcome {
                attacked_successes = attacked_successes
                    .checked_add(*count)
                    .ok_or_else(|| PyValueError::new_err("success count overflow"))?;
            }
        }
    }
    if attacked_shots == 0 || attacked_shots > BINARY_CONTRAST_MAX_EXACT_COUNT {
        return Err(PyValueError::new_err("invalid attacked shot count"));
    }
    let (p_attack, p_base, standard_error) = binary_group_statistics(
        attacked_successes,
        attacked_shots,
        baseline_successes,
        baseline_shots,
    );
    let drop = p_base - p_attack;
    if standard_error == 0.0 {
        return Ok(drop >= minimum_drop);
    }
    Ok(drop >= minimum_drop && drop / standard_error >= significance_z)
}

/// Owns private baseline and submission accounting for one challenge instance.
#[pyclass]
struct CrosstalkEvaluationRuntime {
    success_outcome: String,
    required_runs: usize,
    max_attempts: u32,
    attempts: u32,
    baseline: Option<(u64, u64)>,
    significance_z: f64,
    minimum_drop: f64,
}

#[pymethods]
impl CrosstalkEvaluationRuntime {
    #[new]
    fn new(
        schema_version: &str,
        success_outcome: String,
        required_runs: usize,
        max_attempts: u32,
        significance_z: f64,
        minimum_drop: f64,
    ) -> PyResult<Self> {
        if schema_version != CROSSTALK_EVALUATION_SCHEMA_VERSION
            || success_outcome.is_empty()
            || required_runs == 0
            || max_attempts == 0
            || !significance_z.is_finite()
            || !minimum_drop.is_finite()
        {
            return Err(PyValueError::new_err(
                "invalid crosstalk runtime configuration",
            ));
        }
        Ok(Self {
            success_outcome,
            required_runs,
            max_attempts,
            attempts: 0,
            baseline: None,
            significance_z,
            minimum_drop,
        })
    }

    fn attempts_remaining(&self) -> u32 {
        self.max_attempts - self.attempts
    }

    fn requires_baseline(&self, run_count: usize) -> bool {
        run_count >= self.required_runs && self.baseline.is_none()
    }

    fn submit(
        &mut self,
        runs: Vec<HashMap<String, u64>>,
        baseline: Option<(u64, u64)>,
    ) -> PyResult<(u32, u32, bool, bool)> {
        if self.attempts == self.max_attempts {
            return Err(PyValueError::new_err("no submissions remain"));
        }
        if runs.len() < self.required_runs {
            return Ok((self.attempts, self.attempts_remaining(), false, false));
        }
        if self.baseline.is_none() {
            let (successes, shots) =
                baseline.ok_or_else(|| PyValueError::new_err("baseline required"))?;
            if shots == 0 || shots > BINARY_CONTRAST_MAX_EXACT_COUNT || successes > shots {
                return Err(PyValueError::new_err("invalid baseline counts"));
            }
            self.baseline = Some((successes, shots));
        }
        let (successes, shots) = self.baseline.expect("validated baseline");
        let passed = evaluate_crosstalk_disruption_v1(
            CROSSTALK_EVALUATION_SCHEMA_VERSION,
            runs,
            &self.success_outcome,
            successes,
            shots,
            self.significance_z,
            self.minimum_drop,
        )?;
        self.attempts += 1;
        Ok((self.attempts, self.attempts_remaining(), true, passed))
    }
}

#[pyfunction]
fn rk4_time_dependent_stages_flat(
    rho: Vec<f64>,
    h1: Vec<f64>,
    h2: Vec<f64>,
    h3: Vec<f64>,
    h4: Vec<f64>,
    collapse_ops: Vec<f64>,
    num_ops: usize,
    d: usize,
    dt: f64,
) -> PyResult<Vec<f64>> {
    let element_count = validate_time_dependent_rk4_inputs(
        &rho,
        &h1,
        &h2,
        &h3,
        &h4,
        &collapse_ops,
        num_ops,
        d,
        dt,
    )?;
    let (collapse_adjoints, collapse_products) =
        prepare_collapse_operators_raw(&collapse_ops, num_ops, d);
    let (k1, k2, k3, k4) = rk4_time_dependent_stages_raw(
        &rho,
        &h1,
        &h2,
        &h3,
        &h4,
        &collapse_ops,
        &collapse_adjoints,
        &collapse_products,
        num_ops,
        d,
        dt,
    );
    let mut stages = Vec::with_capacity(4 * element_count);
    stages.extend_from_slice(&k1);
    stages.extend_from_slice(&k2);
    stages.extend_from_slice(&k3);
    stages.extend_from_slice(&k4);
    Ok(stages)
}

#[pyfunction]
fn rk4_time_dependent_step_flat(
    rho: Vec<f64>,
    h1: Vec<f64>,
    h2: Vec<f64>,
    h3: Vec<f64>,
    h4: Vec<f64>,
    collapse_ops: Vec<f64>,
    num_ops: usize,
    d: usize,
    dt: f64,
) -> PyResult<Vec<f64>> {
    validate_time_dependent_rk4_inputs(&rho, &h1, &h2, &h3, &h4, &collapse_ops, num_ops, d, dt)?;
    let (collapse_adjoints, collapse_products) =
        prepare_collapse_operators_raw(&collapse_ops, num_ops, d);
    let (k1, k2, k3, k4) = rk4_time_dependent_stages_raw(
        &rho,
        &h1,
        &h2,
        &h3,
        &h4,
        &collapse_ops,
        &collapse_adjoints,
        &collapse_products,
        num_ops,
        d,
        dt,
    );
    let mut evolved = rho;
    for index in 0..evolved.len() {
        evolved[index] += dt / 6.0 * (k1[index] + 2.0 * k2[index] + 2.0 * k3[index] + k4[index]);
    }
    Ok(evolved)
}

#[pyfunction]
fn rk4_evolve_cleaned_flat(
    rho: Vec<f64>,
    h: Vec<f64>,
    collapse_ops: Vec<f64>,
    num_ops: usize,
    d: usize,
    dt: f64,
    substeps: usize,
) -> PyResult<Vec<f64>> {
    let element_count = matrix_element_count(d)?;
    validate_matrix_len("rho", rho.len(), element_count, d)?;
    validate_matrix_len("h", h.len(), element_count, d)?;
    validate_collapse_ops_len(collapse_ops.len(), num_ops, element_count, d)?;
    if substeps == 0 {
        return Err(PyValueError::new_err("substeps must be greater than 0"));
    }
    if !dt.is_finite() {
        return Err(PyValueError::new_err("dt must be finite"));
    }

    let prepared = prepare_structured_collapse_operators(&collapse_ops, num_ops, d);
    let hamiltonian_is_zero = is_zero_matrix(&h);
    let mut evolved = rho;
    let mut next = vec![0.0; element_count];
    let mut workspace = Rk4Workspace::new(d);
    for _ in 0..substeps {
        rk4_step_prepared_into(
            &evolved,
            &h,
            None,
            &prepared,
            d,
            dt,
            hamiltonian_is_zero,
            &mut next,
            &mut workspace,
        );
        clean_density_flat(&mut next, d)?;
        std::mem::swap(&mut evolved, &mut next);
    }

    Ok(evolved)
}

#[pyfunction]
fn rk4_evolve_cleaned_samples_flat(
    rho: Vec<f64>,
    h: Vec<f64>,
    collapse_ops: Vec<f64>,
    num_ops: usize,
    d: usize,
    dt: f64,
    sample_substeps: Vec<usize>,
) -> PyResult<Vec<f64>> {
    let element_count = matrix_element_count(d)?;
    validate_matrix_len("rho", rho.len(), element_count, d)?;
    validate_matrix_len("h", h.len(), element_count, d)?;
    validate_collapse_ops_len(collapse_ops.len(), num_ops, element_count, d)?;
    if !dt.is_finite() {
        return Err(PyValueError::new_err("dt must be finite"));
    }
    if sample_substeps.is_empty() {
        return Err(PyValueError::new_err("sample_substeps must be non-empty"));
    }
    for substeps in sample_substeps.iter() {
        if *substeps == 0 {
            return Err(PyValueError::new_err(
                "sample_substeps entries must be greater than 0",
            ));
        }
    }

    let total_output_len = sample_substeps
        .len()
        .checked_mul(element_count)
        .ok_or_else(|| PyValueError::new_err("sample output length is too large"))?;
    let prepared = prepare_structured_collapse_operators(&collapse_ops, num_ops, d);
    let hamiltonian_is_zero = is_zero_matrix(&h);
    let mut evolved = rho;
    let mut next = vec![0.0; element_count];
    let mut workspace = Rk4Workspace::new(d);
    let mut samples = Vec::with_capacity(total_output_len);
    for substeps in sample_substeps {
        for _ in 0..substeps {
            rk4_step_prepared_into(
                &evolved,
                &h,
                None,
                &prepared,
                d,
                dt,
                hamiltonian_is_zero,
                &mut next,
                &mut workspace,
            );
            clean_density_flat(&mut next, d)?;
            std::mem::swap(&mut evolved, &mut next);
        }
        samples.extend_from_slice(&evolved);
    }

    Ok(samples)
}

#[pyclass]
struct DenseRk4Session {
    d: usize,
    state: Vec<f64>,
    prepared: Vec<PreparedCollapseOperator>,
    workspace: Rk4Workspace,
    next: Vec<f64>,
    ideal_state: Vec<f64>,
    ideal_workspace: Rk4Workspace,
    ideal_next: Vec<f64>,
    hamiltonians: Vec<PreparedHamiltonian>,
}

#[pymethods]
impl DenseRk4Session {
    #[new]
    fn new(rho: Vec<f64>, collapse_ops: Vec<f64>, num_ops: usize, d: usize) -> PyResult<Self> {
        let element_count = matrix_element_count(d)?;
        validate_matrix_len("rho", rho.len(), element_count, d)?;
        validate_collapse_ops_len(collapse_ops.len(), num_ops, element_count, d)?;
        validate_finite_matrix("rho", &rho)?;
        validate_finite_matrix("collapse_ops", &collapse_ops)?;
        Ok(Self {
            d,
            ideal_state: rho.clone(),
            state: rho,
            prepared: prepare_structured_collapse_operators(&collapse_ops, num_ops, d),
            workspace: Rk4Workspace::new(d),
            next: vec![0.0; element_count],
            ideal_workspace: Rk4Workspace::new(d),
            ideal_next: vec![0.0; element_count],
            hamiltonians: Vec::new(),
        })
    }

    #[staticmethod]
    fn from_local_rates(
        rho: Vec<f64>,
        n_qubits: usize,
        gamma_down: f64,
        gamma_up: f64,
        gamma_phi: f64,
    ) -> PyResult<Self> {
        if n_qubits >= usize::BITS as usize {
            return Err(PyValueError::new_err("n_qubits is too large"));
        }
        let d = 1usize << n_qubits;
        let element_count = matrix_element_count(d)?;
        validate_matrix_len("rho", rho.len(), element_count, d)?;
        validate_finite_matrix("rho", &rho)?;
        for (name, rate) in [
            ("gamma_down", gamma_down),
            ("gamma_up", gamma_up),
            ("gamma_phi", gamma_phi),
        ] {
            if !rate.is_finite() || rate < 0.0 {
                return Err(PyValueError::new_err(format!(
                    "{name} must be finite and non-negative"
                )));
            }
        }
        Ok(Self {
            d,
            ideal_state: rho.clone(),
            state: rho,
            prepared: prepare_local_collapse_operators(n_qubits, gamma_down, gamma_up, gamma_phi),
            workspace: Rk4Workspace::new(d),
            next: vec![0.0; element_count],
            ideal_workspace: Rk4Workspace::new(d),
            ideal_next: vec![0.0; element_count],
            hamiltonians: Vec::new(),
        })
    }

    fn register_hamiltonian(&mut self, h: Vec<f64>) -> PyResult<usize> {
        let element_count = matrix_element_count(self.d)?;
        validate_matrix_len("h", h.len(), element_count, self.d)?;
        validate_finite_matrix("h", &h)?;
        if let Some(index) = self.hamiltonians.iter().position(|known| known.dense == h) {
            return Ok(index);
        }
        self.hamiltonians.push(PreparedHamiltonian::new(h, self.d));
        Ok(self.hamiltonians.len() - 1)
    }

    fn set_state(&mut self, rho: Vec<f64>) -> PyResult<()> {
        let element_count = matrix_element_count(self.d)?;
        validate_matrix_len("rho", rho.len(), element_count, self.d)?;
        validate_finite_matrix("rho", &rho)?;
        self.state = rho;
        Ok(())
    }

    fn state(&self) -> Vec<f64> {
        self.state.clone()
    }

    fn set_ideal_state(&mut self, rho: Vec<f64>) -> PyResult<()> {
        let element_count = matrix_element_count(self.d)?;
        validate_matrix_len("ideal_state", rho.len(), element_count, self.d)?;
        validate_finite_matrix("ideal_state", &rho)?;
        self.ideal_state = rho;
        Ok(())
    }

    fn evolve_cleaned(&mut self, h: Vec<f64>, dt: f64, substeps: usize) -> PyResult<Vec<f64>> {
        let element_count = matrix_element_count(self.d)?;
        validate_matrix_len("h", h.len(), element_count, self.d)?;
        if substeps == 0 || !dt.is_finite() {
            return Err(PyValueError::new_err(
                "substeps must be positive and dt must be finite",
            ));
        }
        let hamiltonian_is_zero = is_zero_matrix(&h);
        for _ in 0..substeps {
            rk4_step_prepared_into(
                &self.state,
                &h,
                None,
                &self.prepared,
                self.d,
                dt,
                hamiltonian_is_zero,
                &mut self.next,
                &mut self.workspace,
            );
            clean_density_flat(&mut self.next, self.d)?;
            std::mem::swap(&mut self.state, &mut self.next);
        }
        Ok(self.state.clone())
    }

    fn evolve_piecewise_cleaned_samples(
        &mut self,
        hamiltonians: Vec<f64>,
        dts: Vec<f64>,
        substeps: Vec<usize>,
    ) -> PyResult<Vec<f64>> {
        let segment_count = dts.len();
        if segment_count == 0 || substeps.len() != segment_count {
            return Err(PyValueError::new_err(
                "dts and substeps must have the same non-zero length",
            ));
        }
        let element_count = matrix_element_count(self.d)?;
        if hamiltonians.len() != segment_count * element_count {
            return Err(PyValueError::new_err(
                "hamiltonians length must equal segment_count * 2 * d * d",
            ));
        }
        let mut samples = Vec::with_capacity(segment_count * element_count);
        for segment in 0..segment_count {
            let dt = dts[segment];
            let count = substeps[segment];
            if count == 0 || !dt.is_finite() {
                return Err(PyValueError::new_err(
                    "substeps must be positive and dts must be finite",
                ));
            }
            let start = segment * element_count;
            let h = &hamiltonians[start..start + element_count];
            let hamiltonian_is_zero = is_zero_matrix(h);
            for _ in 0..count {
                rk4_step_prepared_into(
                    &self.state,
                    h,
                    None,
                    &self.prepared,
                    self.d,
                    dt,
                    hamiltonian_is_zero,
                    &mut self.next,
                    &mut self.workspace,
                );
                clean_density_flat(&mut self.next, self.d)?;
                std::mem::swap(&mut self.state, &mut self.next);
            }
            samples.extend_from_slice(&self.state);
        }
        Ok(samples)
    }

    fn evolve_registered_cleaned_samples(
        &mut self,
        hamiltonian_ids: Vec<usize>,
        dts: Vec<f64>,
        substeps: Vec<usize>,
    ) -> PyResult<Vec<f64>> {
        let segment_count = dts.len();
        if segment_count == 0
            || hamiltonian_ids.len() != segment_count
            || substeps.len() != segment_count
        {
            return Err(PyValueError::new_err(
                "hamiltonian_ids, dts, and substeps must have the same non-zero length",
            ));
        }
        let element_count = matrix_element_count(self.d)?;
        let mut samples = Vec::with_capacity(segment_count * element_count);
        for segment in 0..segment_count {
            let dt = dts[segment];
            let count = substeps[segment];
            if count == 0 || !dt.is_finite() {
                return Err(PyValueError::new_err(
                    "substeps must be positive and dts must be finite",
                ));
            }
            let h = self
                .hamiltonians
                .get(hamiltonian_ids[segment])
                .ok_or_else(|| {
                    PyValueError::new_err("hamiltonian_ids contains an unregistered id")
                })?;
            for _ in 0..count {
                rk4_step_prepared_into(
                    &self.state,
                    &h.dense,
                    h.sparse_if_beneficial(),
                    &self.prepared,
                    self.d,
                    dt,
                    h.is_zero,
                    &mut self.next,
                    &mut self.workspace,
                );
                clean_density_flat(&mut self.next, self.d)?;
                std::mem::swap(&mut self.state, &mut self.next);
            }
            samples.extend_from_slice(&self.state);
        }
        Ok(samples)
    }

    fn evolve_paired_piecewise_metrics(
        &mut self,
        hamiltonians: Vec<f64>,
        dts: Vec<f64>,
        substeps: Vec<usize>,
    ) -> PyResult<(Vec<f64>, Vec<f64>, Vec<f64>)> {
        let segment_count = dts.len();
        if segment_count == 0 || substeps.len() != segment_count {
            return Err(PyValueError::new_err(
                "dts and substeps must have the same non-zero length",
            ));
        }
        let element_count = matrix_element_count(self.d)?;
        if hamiltonians.len() != segment_count * element_count {
            return Err(PyValueError::new_err(
                "hamiltonians length must equal segment_count * 2 * d * d",
            ));
        }
        let mut noisy_samples = Vec::with_capacity(segment_count * element_count);
        let mut ideal_samples = Vec::with_capacity(segment_count * element_count);
        let mut metrics = Vec::with_capacity(segment_count * 4);
        let no_collapse_ops: [PreparedCollapseOperator; 0] = [];
        for segment in 0..segment_count {
            let dt = dts[segment];
            let count = substeps[segment];
            if count == 0 || !dt.is_finite() {
                return Err(PyValueError::new_err(
                    "substeps must be positive and dts must be finite",
                ));
            }
            let start = segment * element_count;
            let h = &hamiltonians[start..start + element_count];
            let hamiltonian_is_zero = is_zero_matrix(h);
            for _ in 0..count {
                rk4_step_prepared_into(
                    &self.state,
                    h,
                    None,
                    &self.prepared,
                    self.d,
                    dt,
                    hamiltonian_is_zero,
                    &mut self.next,
                    &mut self.workspace,
                );
                clean_density_flat(&mut self.next, self.d)?;
                std::mem::swap(&mut self.state, &mut self.next);
                rk4_step_prepared_into(
                    &self.ideal_state,
                    h,
                    None,
                    &no_collapse_ops,
                    self.d,
                    dt,
                    hamiltonian_is_zero,
                    &mut self.ideal_next,
                    &mut self.ideal_workspace,
                );
                clean_density_flat(&mut self.ideal_next, self.d)?;
                std::mem::swap(&mut self.ideal_state, &mut self.ideal_next);
            }
            noisy_samples.extend_from_slice(&self.state);
            ideal_samples.extend_from_slice(&self.ideal_state);
            metrics.push(probability_roundoff_guard(trace_product_real(
                &self.state,
                &self.ideal_state,
                self.d,
            )));
            metrics.push(probability_roundoff_guard(trace_product_real(
                &self.state,
                &self.state,
                self.d,
            )));
            metrics.push(trace_error_flat(&self.state, self.d));
            metrics.push(probability_roundoff_guard(trace_product_real(
                &self.ideal_state,
                &self.ideal_state,
                self.d,
            )));
        }
        Ok((noisy_samples, ideal_samples, metrics))
    }

    fn evolve_paired_registered_compact(
        &mut self,
        hamiltonian_ids: Vec<usize>,
        dts: Vec<f64>,
        substeps: Vec<usize>,
        capture_flags: Vec<bool>,
    ) -> PyResult<(Vec<usize>, Vec<f64>, Vec<f64>, Vec<f64>)> {
        let segment_count = dts.len();
        if segment_count == 0
            || hamiltonian_ids.len() != segment_count
            || substeps.len() != segment_count
            || capture_flags.len() != segment_count
        {
            return Err(PyValueError::new_err(
                "hamiltonian_ids, dts, substeps, and capture_flags must have the same non-zero length",
            ));
        }
        let element_count = matrix_element_count(self.d)?;
        let mut captured_indices = Vec::new();
        let mut noisy_samples = Vec::new();
        let mut ideal_samples = Vec::new();
        let mut metrics = Vec::with_capacity(segment_count * 4);
        let no_collapse_ops: [PreparedCollapseOperator; 0] = [];
        for segment in 0..segment_count {
            let dt = dts[segment];
            let count = substeps[segment];
            if count == 0 || !dt.is_finite() {
                return Err(PyValueError::new_err(
                    "substeps must be positive and dts must be finite",
                ));
            }
            let h = self
                .hamiltonians
                .get(hamiltonian_ids[segment])
                .ok_or_else(|| {
                    PyValueError::new_err("hamiltonian_ids contains an unregistered id")
                })?;
            for _ in 0..count {
                rk4_step_prepared_into(
                    &self.state,
                    &h.dense,
                    h.sparse_if_beneficial(),
                    &self.prepared,
                    self.d,
                    dt,
                    h.is_zero,
                    &mut self.next,
                    &mut self.workspace,
                );
                clean_density_flat(&mut self.next, self.d)?;
                std::mem::swap(&mut self.state, &mut self.next);
                rk4_step_prepared_into(
                    &self.ideal_state,
                    &h.dense,
                    h.sparse_if_beneficial(),
                    &no_collapse_ops,
                    self.d,
                    dt,
                    h.is_zero,
                    &mut self.ideal_next,
                    &mut self.ideal_workspace,
                );
                clean_density_flat(&mut self.ideal_next, self.d)?;
                std::mem::swap(&mut self.ideal_state, &mut self.ideal_next);
            }
            let fidelity = probability_roundoff_guard(trace_product_real(
                &self.state,
                &self.ideal_state,
                self.d,
            ));
            let purity =
                probability_roundoff_guard(trace_product_real(&self.state, &self.state, self.d));
            let trace_error = trace_error_flat(&self.state, self.d);
            let ideal_purity = probability_roundoff_guard(trace_product_real(
                &self.ideal_state,
                &self.ideal_state,
                self.d,
            ));
            metrics.extend_from_slice(&[fidelity, purity, trace_error, ideal_purity]);
            let must_capture = capture_flags[segment]
                || segment + 1 == segment_count
                || (ideal_purity - 1.0).abs() > 1e-8;
            if must_capture {
                captured_indices.push(segment);
                noisy_samples.extend_from_slice(&self.state);
                ideal_samples.extend_from_slice(&self.ideal_state);
            }
        }
        debug_assert_eq!(noisy_samples.len(), captured_indices.len() * element_count);
        Ok((captured_indices, noisy_samples, ideal_samples, metrics))
    }
}

fn trace_product_real(left: &[f64], right: &[f64], d: usize) -> f64 {
    let mut result = 0.0;
    for row in 0..d {
        for column in 0..d {
            let left_index = 2 * (row * d + column);
            let right_index = 2 * (column * d + row);
            result += left[left_index] * right[right_index]
                - left[left_index + 1] * right[right_index + 1];
        }
    }
    result
}

fn trace_error_flat(state: &[f64], d: usize) -> f64 {
    let mut real = -1.0;
    let mut imag = 0.0;
    for index in 0..d {
        let diagonal = 2 * (index * d + index);
        real += state[diagonal];
        imag += state[diagonal + 1];
    }
    real.hypot(imag)
}

fn probability_roundoff_guard(value: f64) -> f64 {
    if value < 0.0 && value > -1e-7 {
        0.0
    } else if value > 1.0 && value < 1.0 + 1e-7 {
        1.0
    } else {
        value
    }
}

fn prepare_collapse_operators_raw(
    collapse_ops: &[f64],
    num_ops: usize,
    d: usize,
) -> (Vec<f64>, Vec<f64>) {
    let element_count = 2 * d * d;
    let mut adjoints = Vec::with_capacity(num_ops * element_count);
    let mut products = Vec::with_capacity(num_ops * element_count);
    for op_index in 0..num_ops {
        let start = op_index * element_count;
        let end = start + element_count;
        let collapse_op = &collapse_ops[start..end];
        let adjoint = conjugate_transpose_flat(collapse_op, d);
        let product = matmul_flat(&adjoint, collapse_op, d);
        adjoints.extend_from_slice(&adjoint);
        products.extend_from_slice(&product);
    }
    (adjoints, products)
}

fn is_zero_matrix(matrix: &[f64]) -> bool {
    matrix.iter().all(|value| *value == 0.0)
}

#[derive(Clone)]
struct SparseEntry {
    row: usize,
    column: usize,
    real: f64,
    imag: f64,
}

struct PreparedHamiltonian {
    dense: Vec<f64>,
    sparse: Vec<SparseEntry>,
    is_zero: bool,
    use_sparse: bool,
}

impl PreparedHamiltonian {
    fn new(dense: Vec<f64>, d: usize) -> Self {
        let sparse = sparse_entries(&dense, d);
        let is_zero = sparse.is_empty();
        let use_sparse = !is_zero && sparse.len() < d * d;
        Self {
            dense,
            sparse,
            is_zero,
            use_sparse,
        }
    }

    fn sparse_if_beneficial(&self) -> Option<&[SparseEntry]> {
        self.use_sparse.then_some(self.sparse.as_slice())
    }
}

struct PreparedCollapseOperator {
    operator: Vec<f64>,
    adjoint: Vec<f64>,
    product: Vec<f64>,
    operator_sparse: Vec<SparseEntry>,
    adjoint_sparse: Vec<SparseEntry>,
    product_sparse: Vec<SparseEntry>,
    use_sparse: bool,
}

fn sparse_entries(matrix: &[f64], d: usize) -> Vec<SparseEntry> {
    let mut entries = Vec::new();
    for row in 0..d {
        for column in 0..d {
            let index = 2 * (row * d + column);
            let real = matrix[index];
            let imag = matrix[index + 1];
            if real != 0.0 || imag != 0.0 {
                entries.push(SparseEntry {
                    row,
                    column,
                    real,
                    imag,
                });
            }
        }
    }
    entries
}

fn prepare_structured_collapse_operators(
    collapse_ops: &[f64],
    num_ops: usize,
    d: usize,
) -> Vec<PreparedCollapseOperator> {
    let element_count = 2 * d * d;
    (0..num_ops)
        .map(|op_index| {
            let start = op_index * element_count;
            let operator = collapse_ops[start..start + element_count].to_vec();
            let adjoint = conjugate_transpose_flat(&operator, d);
            let product = matmul_flat(&adjoint, &operator, d);
            let operator_sparse = sparse_entries(&operator, d);
            let adjoint_sparse = sparse_entries(&adjoint, d);
            let product_sparse = sparse_entries(&product, d);
            let sparse_work =
                operator_sparse.len() + adjoint_sparse.len() + 2 * product_sparse.len();
            let dense_work = 4 * d * d;
            PreparedCollapseOperator {
                operator,
                adjoint,
                product,
                operator_sparse,
                adjoint_sparse,
                product_sparse,
                use_sparse: sparse_work < dense_work,
            }
        })
        .collect()
}

fn prepared_from_sparse(
    d: usize,
    operator_sparse: Vec<SparseEntry>,
    adjoint_sparse: Vec<SparseEntry>,
    product_sparse: Vec<SparseEntry>,
) -> PreparedCollapseOperator {
    let mut operator = vec![0.0; 2 * d * d];
    let mut adjoint = vec![0.0; 2 * d * d];
    let mut product = vec![0.0; 2 * d * d];
    for (entries, dense) in [
        (&operator_sparse, &mut operator),
        (&adjoint_sparse, &mut adjoint),
        (&product_sparse, &mut product),
    ] {
        for entry in entries {
            let index = 2 * (entry.row * d + entry.column);
            dense[index] = entry.real;
            dense[index + 1] = entry.imag;
        }
    }
    PreparedCollapseOperator {
        operator,
        adjoint,
        product,
        operator_sparse,
        adjoint_sparse,
        product_sparse,
        use_sparse: true,
    }
}

fn prepare_local_collapse_operators(
    n_qubits: usize,
    gamma_down: f64,
    gamma_up: f64,
    gamma_phi: f64,
) -> Vec<PreparedCollapseOperator> {
    let d = 1usize << n_qubits;
    let mut prepared = Vec::with_capacity(3 * n_qubits);
    for (kind, rate) in [
        (0usize, gamma_down),
        (1usize, gamma_up),
        (2usize, gamma_phi),
    ] {
        if rate <= 0.0 {
            continue;
        }
        let amplitude = if kind == 2 {
            (rate / 2.0).sqrt()
        } else {
            rate.sqrt()
        };
        for qubit in 0..n_qubits {
            let mask = 1usize << (n_qubits - qubit - 1);
            let mut operator = Vec::with_capacity(d);
            let mut adjoint = Vec::with_capacity(d);
            let mut product = Vec::with_capacity(d);
            for basis in 0..d {
                let bit_set = basis & mask != 0;
                match kind {
                    0 if bit_set => {
                        operator.push(SparseEntry {
                            row: basis ^ mask,
                            column: basis,
                            real: amplitude,
                            imag: 0.0,
                        });
                        adjoint.push(SparseEntry {
                            row: basis,
                            column: basis ^ mask,
                            real: amplitude,
                            imag: 0.0,
                        });
                        product.push(SparseEntry {
                            row: basis,
                            column: basis,
                            real: rate,
                            imag: 0.0,
                        });
                    }
                    1 if !bit_set => {
                        operator.push(SparseEntry {
                            row: basis ^ mask,
                            column: basis,
                            real: amplitude,
                            imag: 0.0,
                        });
                        adjoint.push(SparseEntry {
                            row: basis,
                            column: basis ^ mask,
                            real: amplitude,
                            imag: 0.0,
                        });
                        product.push(SparseEntry {
                            row: basis,
                            column: basis,
                            real: rate,
                            imag: 0.0,
                        });
                    }
                    2 => {
                        let signed = if bit_set { -amplitude } else { amplitude };
                        operator.push(SparseEntry {
                            row: basis,
                            column: basis,
                            real: signed,
                            imag: 0.0,
                        });
                        adjoint.push(SparseEntry {
                            row: basis,
                            column: basis,
                            real: signed,
                            imag: 0.0,
                        });
                        product.push(SparseEntry {
                            row: basis,
                            column: basis,
                            real: rate / 2.0,
                            imag: 0.0,
                        });
                    }
                    _ => {}
                }
            }
            prepared.push(prepared_from_sparse(d, operator, adjoint, product));
        }
    }
    prepared
}

struct Rk4Workspace {
    k1: Vec<f64>,
    k2: Vec<f64>,
    k3: Vec<f64>,
    k4: Vec<f64>,
    stage: Vec<f64>,
    scratch1: Vec<f64>,
    scratch2: Vec<f64>,
}

impl Rk4Workspace {
    fn new(d: usize) -> Self {
        let len = 2 * d * d;
        Self {
            k1: vec![0.0; len],
            k2: vec![0.0; len],
            k3: vec![0.0; len],
            k4: vec![0.0; len],
            stage: vec![0.0; len],
            scratch1: vec![0.0; len],
            scratch2: vec![0.0; len],
        }
    }
}

fn sparse_left_mul_into(entries: &[SparseEntry], right: &[f64], out: &mut [f64], d: usize) {
    out.fill(0.0);
    for entry in entries {
        for column in 0..d {
            let source = 2 * (entry.column * d + column);
            let target = 2 * (entry.row * d + column);
            let right_real = right[source];
            let right_imag = right[source + 1];
            out[target] += entry.real * right_real - entry.imag * right_imag;
            out[target + 1] += entry.real * right_imag + entry.imag * right_real;
        }
    }
}

fn sparse_right_mul_into(left: &[f64], entries: &[SparseEntry], out: &mut [f64], d: usize) {
    out.fill(0.0);
    for entry in entries {
        for row in 0..d {
            let source = 2 * (row * d + entry.row);
            let target = 2 * (row * d + entry.column);
            let left_real = left[source];
            let left_imag = left[source + 1];
            out[target] += left_real * entry.real - left_imag * entry.imag;
            out[target + 1] += left_real * entry.imag + left_imag * entry.real;
        }
    }
}

fn matmul_into(a: &[f64], b: &[f64], out: &mut [f64], d: usize) {
    out.fill(0.0);
    let parallelism = if d >= 32 {
        Parallelism::Rayon(0)
    } else {
        Parallelism::None
    };
    unsafe {
        gemm(
            d,
            d,
            d,
            out.as_mut_ptr() as *mut c64,
            1,
            d as isize,
            false,
            a.as_ptr() as *const c64,
            1,
            d as isize,
            b.as_ptr() as *const c64,
            1,
            d as isize,
            c64::new(0.0, 0.0),
            c64::new(1.0, 0.0),
            false,
            false,
            false,
            parallelism,
        );
    }
}

fn lindblad_rhs_prepared_into(
    rho: &[f64],
    h: &[f64],
    h_sparse: Option<&[SparseEntry]>,
    collapse_ops: &[PreparedCollapseOperator],
    d: usize,
    hamiltonian_is_zero: bool,
    derivative: &mut [f64],
    scratch1: &mut [f64],
    scratch2: &mut [f64],
) {
    derivative.fill(0.0);
    if !hamiltonian_is_zero {
        if let Some(entries) = h_sparse {
            sparse_left_mul_into(entries, rho, scratch1, d);
            sparse_right_mul_into(rho, entries, scratch2, d);
        } else {
            matmul_into(h, rho, scratch1, d);
            matmul_into(rho, h, scratch2, d);
        }
        for index in (0..derivative.len()).step_by(2) {
            let commutator_real = scratch1[index] - scratch2[index];
            let commutator_imag = scratch1[index + 1] - scratch2[index + 1];
            derivative[index] += commutator_imag;
            derivative[index + 1] -= commutator_real;
        }
    }
    for collapse in collapse_ops {
        if collapse.use_sparse {
            sparse_left_mul_into(&collapse.operator_sparse, rho, scratch1, d);
            sparse_right_mul_into(scratch1, &collapse.adjoint_sparse, scratch2, d);
            for index in 0..derivative.len() {
                derivative[index] += scratch2[index];
            }
            sparse_left_mul_into(&collapse.product_sparse, rho, scratch1, d);
            for index in 0..derivative.len() {
                derivative[index] -= 0.5 * scratch1[index];
            }
            sparse_right_mul_into(rho, &collapse.product_sparse, scratch2, d);
            for index in 0..derivative.len() {
                derivative[index] -= 0.5 * scratch2[index];
            }
        } else {
            matmul_into(&collapse.operator, rho, scratch1, d);
            matmul_into(scratch1, &collapse.adjoint, scratch2, d);
            for index in 0..derivative.len() {
                derivative[index] += scratch2[index];
            }
            matmul_into(&collapse.product, rho, scratch1, d);
            for index in 0..derivative.len() {
                derivative[index] -= 0.5 * scratch1[index];
            }
            matmul_into(rho, &collapse.product, scratch2, d);
            for index in 0..derivative.len() {
                derivative[index] -= 0.5 * scratch2[index];
            }
        }
    }
}

fn rk4_step_prepared_into(
    rho: &[f64],
    h: &[f64],
    h_sparse: Option<&[SparseEntry]>,
    collapse_ops: &[PreparedCollapseOperator],
    d: usize,
    dt: f64,
    hamiltonian_is_zero: bool,
    evolved: &mut [f64],
    workspace: &mut Rk4Workspace,
) {
    let Rk4Workspace {
        k1,
        k2,
        k3,
        k4,
        stage,
        scratch1,
        scratch2,
    } = workspace;
    lindblad_rhs_prepared_into(
        rho,
        h,
        h_sparse,
        collapse_ops,
        d,
        hamiltonian_is_zero,
        k1,
        scratch1,
        scratch2,
    );
    for i in 0..rho.len() {
        stage[i] = rho[i] + 0.5 * dt * k1[i];
    }
    lindblad_rhs_prepared_into(
        stage,
        h,
        h_sparse,
        collapse_ops,
        d,
        hamiltonian_is_zero,
        k2,
        scratch1,
        scratch2,
    );
    for i in 0..rho.len() {
        stage[i] = rho[i] + 0.5 * dt * k2[i];
    }
    lindblad_rhs_prepared_into(
        stage,
        h,
        h_sparse,
        collapse_ops,
        d,
        hamiltonian_is_zero,
        k3,
        scratch1,
        scratch2,
    );
    for i in 0..rho.len() {
        stage[i] = rho[i] + dt * k3[i];
    }
    lindblad_rhs_prepared_into(
        stage,
        h,
        h_sparse,
        collapse_ops,
        d,
        hamiltonian_is_zero,
        k4,
        scratch1,
        scratch2,
    );
    for i in 0..rho.len() {
        evolved[i] = rho[i] + dt / 6.0 * (k1[i] + 2.0 * k2[i] + 2.0 * k3[i] + k4[i]);
    }
}

fn lindblad_rhs_raw(
    rho: &[f64],
    h: &[f64],
    collapse_ops: &[f64],
    collapse_adjoints: &[f64],
    collapse_products: &[f64],
    num_ops: usize,
    d: usize,
    hamiltonian_is_zero: bool,
) -> Vec<f64> {
    let element_count = 2 * d * d;
    let mut derivative = vec![0.0_f64; element_count];

    if !hamiltonian_is_zero {
        let h_rho = matmul_flat(h, rho, d);
        let rho_h = matmul_flat(rho, h, d);
        for index in (0..element_count).step_by(2) {
            let commutator_real = h_rho[index] - rho_h[index];
            let commutator_imag = h_rho[index + 1] - rho_h[index + 1];
            derivative[index] += commutator_imag;
            derivative[index + 1] += -commutator_real;
        }
    }

    for op_index in 0..num_ops {
        let start = op_index * element_count;
        let end = start + element_count;
        let collapse_op = &collapse_ops[start..end];
        let collapse_adjoint = &collapse_adjoints[start..end];
        let ldl = &collapse_products[start..end];
        let l_rho = matmul_flat(collapse_op, &rho, d);
        let term1 = matmul_flat(&l_rho, collapse_adjoint, d);
        let term2 = matmul_flat(ldl, &rho, d);
        let term3 = matmul_flat(&rho, ldl, d);

        for index in 0..element_count {
            derivative[index] += term1[index] - 0.5 * (term2[index] + term3[index]);
        }
    }

    derivative
}

fn gksl_liouvillian_superoperator_raw(
    h: &[f64],
    collapse_ops: &[f64],
    collapse_adjoints: &[f64],
    collapse_products: &[f64],
    num_ops: usize,
    d: usize,
) -> Vec<f64> {
    let superoperator_dimension = d * d;
    let mut generator = vec![0.0_f64; 2 * superoperator_dimension * superoperator_dimension];
    let hamiltonian_is_zero = is_zero_matrix(h);

    for input_column in 0..d {
        for input_row in 0..d {
            let vectorized_column = input_row + input_column * d;
            let mut basis = vec![0.0_f64; 2 * d * d];
            basis[2 * (input_row * d + input_column)] = 1.0;
            let derivative = lindblad_rhs_raw(
                &basis,
                h,
                collapse_ops,
                collapse_adjoints,
                collapse_products,
                num_ops,
                d,
                hamiltonian_is_zero,
            );

            for output_column in 0..d {
                for output_row in 0..d {
                    let vectorized_row = output_row + output_column * d;
                    let source = 2 * (output_row * d + output_column);
                    let target = 2 * (vectorized_row * superoperator_dimension + vectorized_column);
                    generator[target] = derivative[source];
                    generator[target + 1] = derivative[source + 1];
                }
            }
        }
    }
    generator
}

const PADE13_THETA: f64 = 5.371_920_351_148_152;
const PADE13_COEFFICIENTS: [f64; 14] = [
    64_764_752_532_480_000.0,
    32_382_376_266_240_000.0,
    7_771_770_303_897_600.0,
    1_187_353_796_428_800.0,
    129_060_195_264_000.0,
    10_559_470_521_600.0,
    670_442_572_800.0,
    33_522_128_640.0,
    1_323_241_920.0,
    40_840_800.0,
    960_960.0,
    16_380.0,
    182.0,
    1.0,
];

fn matrix_exponential_pade13(matrix: &[f64], d: usize) -> Result<Vec<f64>, String> {
    let one_norm = matrix_one_norm(matrix, d);
    if one_norm == 0.0 {
        return Ok(identity_flat(d));
    }
    let scaling_steps = if one_norm <= PADE13_THETA {
        0_u32
    } else {
        (one_norm / PADE13_THETA).log2().ceil() as u32
    };
    let scaled = scale_flat(matrix, 2.0_f64.powi(-(scaling_steps as i32)));
    let mut approximation = pade13(&scaled, d)?;
    for _ in 0..scaling_steps {
        approximation = matmul_flat(&approximation, &approximation, d);
    }
    Ok(approximation)
}

fn pade13(matrix: &[f64], d: usize) -> Result<Vec<f64>, String> {
    let identity = identity_flat(d);
    let matrix_2 = matmul_flat(matrix, matrix, d);
    let matrix_4 = matmul_flat(&matrix_2, &matrix_2, d);
    let matrix_6 = matmul_flat(&matrix_4, &matrix_2, d);
    let coefficients = PADE13_COEFFICIENTS;

    let numerator_inner = linear_combination_flat(
        &[
            (&matrix_6, coefficients[13]),
            (&matrix_4, coefficients[11]),
            (&matrix_2, coefficients[9]),
        ],
        d,
    );
    let numerator_outer = linear_combination_flat(
        &[
            (&matmul_flat(&matrix_6, &numerator_inner, d), 1.0),
            (&matrix_6, coefficients[7]),
            (&matrix_4, coefficients[5]),
            (&matrix_2, coefficients[3]),
            (&identity, coefficients[1]),
        ],
        d,
    );
    let numerator = matmul_flat(matrix, &numerator_outer, d);

    let denominator_inner = linear_combination_flat(
        &[
            (&matrix_6, coefficients[12]),
            (&matrix_4, coefficients[10]),
            (&matrix_2, coefficients[8]),
        ],
        d,
    );
    let denominator = linear_combination_flat(
        &[
            (&matmul_flat(&matrix_6, &denominator_inner, d), 1.0),
            (&matrix_6, coefficients[6]),
            (&matrix_4, coefficients[4]),
            (&matrix_2, coefficients[2]),
            (&identity, coefficients[0]),
        ],
        d,
    );
    solve_complex_matrix(
        &subtract_flat(&denominator, &numerator),
        &add_flat(&denominator, &numerator),
        d,
    )
}

fn solve_complex_matrix(a: &[f64], b: &[f64], d: usize) -> Result<Vec<f64>, String> {
    let mut left = a.to_vec();
    let mut right = b.to_vec();

    for pivot_column in 0..d {
        let mut pivot_row = pivot_column;
        let mut pivot_norm = 0.0_f64;
        for row in pivot_column..d {
            let index = 2 * (row * d + pivot_column);
            let norm = left[index] * left[index] + left[index + 1] * left[index + 1];
            if norm > pivot_norm {
                pivot_norm = norm;
                pivot_row = row;
            }
        }
        if pivot_norm <= f64::EPSILON {
            return Err("Pade linear solve encountered a singular matrix".to_string());
        }
        if pivot_row != pivot_column {
            swap_matrix_rows(&mut left, pivot_row, pivot_column, d);
            swap_matrix_rows(&mut right, pivot_row, pivot_column, d);
        }

        let pivot_index = 2 * (pivot_column * d + pivot_column);
        let pivot_real = left[pivot_index];
        let pivot_imag = left[pivot_index + 1];
        for column in 0..d {
            let left_index = 2 * (pivot_column * d + column);
            let (real, imag) = complex_divide(
                left[left_index],
                left[left_index + 1],
                pivot_real,
                pivot_imag,
            );
            left[left_index] = real;
            left[left_index + 1] = imag;

            let right_index = 2 * (pivot_column * d + column);
            let (real, imag) = complex_divide(
                right[right_index],
                right[right_index + 1],
                pivot_real,
                pivot_imag,
            );
            right[right_index] = real;
            right[right_index + 1] = imag;
        }

        for row in 0..d {
            if row == pivot_column {
                continue;
            }
            let factor_index = 2 * (row * d + pivot_column);
            let factor_real = left[factor_index];
            let factor_imag = left[factor_index + 1];
            for column in 0..d {
                let pivot_left = 2 * (pivot_column * d + column);
                let target_left = 2 * (row * d + column);
                let (product_real, product_imag) = complex_multiply(
                    factor_real,
                    factor_imag,
                    left[pivot_left],
                    left[pivot_left + 1],
                );
                left[target_left] -= product_real;
                left[target_left + 1] -= product_imag;

                let pivot_right = 2 * (pivot_column * d + column);
                let target_right = 2 * (row * d + column);
                let (product_real, product_imag) = complex_multiply(
                    factor_real,
                    factor_imag,
                    right[pivot_right],
                    right[pivot_right + 1],
                );
                right[target_right] -= product_real;
                right[target_right + 1] -= product_imag;
            }
        }
    }
    Ok(right)
}

fn identity_flat(d: usize) -> Vec<f64> {
    let mut identity = vec![0.0_f64; 2 * d * d];
    for index in 0..d {
        identity[2 * (index * d + index)] = 1.0;
    }
    identity
}

fn linear_combination_flat(terms: &[(&[f64], f64)], d: usize) -> Vec<f64> {
    let mut result = vec![0.0_f64; 2 * d * d];
    for (matrix, coefficient) in terms {
        for index in 0..result.len() {
            result[index] += coefficient * matrix[index];
        }
    }
    result
}

fn add_flat(left: &[f64], right: &[f64]) -> Vec<f64> {
    left.iter()
        .zip(right.iter())
        .map(|(left_value, right_value)| left_value + right_value)
        .collect()
}

fn subtract_flat(left: &[f64], right: &[f64]) -> Vec<f64> {
    left.iter()
        .zip(right.iter())
        .map(|(left_value, right_value)| left_value - right_value)
        .collect()
}

fn scale_flat(matrix: &[f64], scale: f64) -> Vec<f64> {
    matrix.iter().map(|value| scale * value).collect()
}

fn matrix_one_norm(matrix: &[f64], d: usize) -> f64 {
    let mut maximum = 0.0_f64;
    for column in 0..d {
        let mut column_sum = 0.0_f64;
        for row in 0..d {
            let index = 2 * (row * d + column);
            column_sum +=
                (matrix[index] * matrix[index] + matrix[index + 1] * matrix[index + 1]).sqrt();
        }
        maximum = maximum.max(column_sum);
    }
    maximum
}

fn swap_matrix_rows(matrix: &mut [f64], first: usize, second: usize, d: usize) {
    for column in 0..d {
        let first_index = 2 * (first * d + column);
        let second_index = 2 * (second * d + column);
        matrix.swap(first_index, second_index);
        matrix.swap(first_index + 1, second_index + 1);
    }
}

fn complex_multiply(
    left_real: f64,
    left_imag: f64,
    right_real: f64,
    right_imag: f64,
) -> (f64, f64) {
    (
        left_real * right_real - left_imag * right_imag,
        left_real * right_imag + left_imag * right_real,
    )
}

fn complex_divide(
    numerator_real: f64,
    numerator_imag: f64,
    denominator_real: f64,
    denominator_imag: f64,
) -> (f64, f64) {
    let denominator = denominator_real * denominator_real + denominator_imag * denominator_imag;
    (
        (numerator_real * denominator_real + numerator_imag * denominator_imag) / denominator,
        (numerator_imag * denominator_real - numerator_real * denominator_imag) / denominator,
    )
}

fn rk4_step_raw(
    rho: &[f64],
    h: &[f64],
    collapse_ops: &[f64],
    collapse_adjoints: &[f64],
    collapse_products: &[f64],
    num_ops: usize,
    d: usize,
    dt: f64,
    hamiltonian_is_zero: bool,
) -> Vec<f64> {
    let element_count = 2 * d * d;
    let k1 = lindblad_rhs_raw(
        rho,
        h,
        collapse_ops,
        collapse_adjoints,
        collapse_products,
        num_ops,
        d,
        hamiltonian_is_zero,
    );
    let k2_state = add_scaled_flat(rho, &k1, 0.5 * dt);
    let k2 = lindblad_rhs_raw(
        &k2_state,
        h,
        collapse_ops,
        collapse_adjoints,
        collapse_products,
        num_ops,
        d,
        hamiltonian_is_zero,
    );
    let k3_state = add_scaled_flat(rho, &k2, 0.5 * dt);
    let k3 = lindblad_rhs_raw(
        &k3_state,
        h,
        collapse_ops,
        collapse_adjoints,
        collapse_products,
        num_ops,
        d,
        hamiltonian_is_zero,
    );
    let k4_state = add_scaled_flat(rho, &k3, dt);
    let k4 = lindblad_rhs_raw(
        &k4_state,
        h,
        collapse_ops,
        collapse_adjoints,
        collapse_products,
        num_ops,
        d,
        hamiltonian_is_zero,
    );

    let mut evolved = rho.to_vec();
    for index in 0..element_count {
        evolved[index] += dt / 6.0 * (k1[index] + 2.0 * k2[index] + 2.0 * k3[index] + k4[index]);
    }
    evolved
}

fn rk4_time_dependent_stages_raw(
    rho: &[f64],
    h1: &[f64],
    h2: &[f64],
    h3: &[f64],
    h4: &[f64],
    collapse_ops: &[f64],
    collapse_adjoints: &[f64],
    collapse_products: &[f64],
    num_ops: usize,
    d: usize,
    dt: f64,
) -> (Vec<f64>, Vec<f64>, Vec<f64>, Vec<f64>) {
    let k1 = lindblad_rhs_raw(
        rho,
        h1,
        collapse_ops,
        collapse_adjoints,
        collapse_products,
        num_ops,
        d,
        is_zero_matrix(h1),
    );
    let k2_state = add_scaled_flat(rho, &k1, 0.5 * dt);
    let k2 = lindblad_rhs_raw(
        &k2_state,
        h2,
        collapse_ops,
        collapse_adjoints,
        collapse_products,
        num_ops,
        d,
        is_zero_matrix(h2),
    );
    let k3_state = add_scaled_flat(rho, &k2, 0.5 * dt);
    let k3 = lindblad_rhs_raw(
        &k3_state,
        h3,
        collapse_ops,
        collapse_adjoints,
        collapse_products,
        num_ops,
        d,
        is_zero_matrix(h3),
    );
    let k4_state = add_scaled_flat(rho, &k3, dt);
    let k4 = lindblad_rhs_raw(
        &k4_state,
        h4,
        collapse_ops,
        collapse_adjoints,
        collapse_products,
        num_ops,
        d,
        is_zero_matrix(h4),
    );
    (k1, k2, k3, k4)
}

fn clean_density_flat(rho: &mut Vec<f64>, d: usize) -> PyResult<()> {
    let adjoint = conjugate_transpose_flat(rho, d);
    for index in 0..rho.len() {
        rho[index] = 0.5 * (rho[index] + adjoint[index]);
    }

    let mut trace_real = 0.0_f64;
    let mut trace_imag = 0.0_f64;
    for index in 0..d {
        let diagonal = 2 * (index * d + index);
        trace_real += rho[diagonal];
        trace_imag += rho[diagonal + 1];
    }

    let trace_norm_squared = trace_real * trace_real + trace_imag * trace_imag;
    if trace_norm_squared == 0.0 {
        return Err(PyValueError::new_err(
            "density matrix trace vanished during evolution",
        ));
    }

    let inverse_real = trace_real / trace_norm_squared;
    let inverse_imag = -trace_imag / trace_norm_squared;
    for index in (0..rho.len()).step_by(2) {
        let value_real = rho[index];
        let value_imag = rho[index + 1];
        rho[index] = value_real * inverse_real - value_imag * inverse_imag;
        rho[index + 1] = value_real * inverse_imag + value_imag * inverse_real;
    }
    Ok(())
}

fn matrix_element_count(d: usize) -> PyResult<usize> {
    if d == 0 {
        return Err(PyValueError::new_err("d must be greater than 0"));
    }
    d.checked_mul(d)
        .and_then(|value| value.checked_mul(2))
        .ok_or_else(|| PyValueError::new_err("matrix dimension is too large"))
}

fn validate_matrix_len(name: &str, actual: usize, expected: usize, d: usize) -> PyResult<()> {
    if actual != expected {
        return Err(PyValueError::new_err(format!(
            "{} length must be 2 * d * d; received len({})={} for d={}",
            name, name, actual, d
        )));
    }
    Ok(())
}

fn validate_collapse_ops_len(
    actual: usize,
    num_ops: usize,
    element_count: usize,
    d: usize,
) -> PyResult<()> {
    let expected = num_ops
        .checked_mul(element_count)
        .ok_or_else(|| PyValueError::new_err("collapse operator count is too large"))?;
    if actual != expected {
        return Err(PyValueError::new_err(format!(
            "collapse_ops length must be num_ops * 2 * d * d; received len(collapse_ops)={} for num_ops={} and d={}",
            actual, num_ops, d
        )));
    }
    Ok(())
}

fn validate_finite_matrix(name: &str, matrix: &[f64]) -> PyResult<()> {
    if matrix.iter().any(|value| !value.is_finite()) {
        return Err(PyValueError::new_err(format!(
            "{} must contain finite values",
            name
        )));
    }
    Ok(())
}

fn validate_hermitian(matrix: &[f64], d: usize) -> PyResult<()> {
    let mut maximum_error = 0.0_f64;
    for row in 0..d {
        for column in 0..d {
            let value = 2 * (row * d + column);
            let transposed = 2 * (column * d + row);
            let real_error = matrix[value] - matrix[transposed];
            let imag_error = matrix[value + 1] + matrix[transposed + 1];
            maximum_error =
                maximum_error.max((real_error * real_error + imag_error * imag_error).sqrt());
        }
    }
    if maximum_error > 1e-12 {
        return Err(PyValueError::new_err("hamiltonian must be Hermitian"));
    }
    Ok(())
}

fn validate_time_dependent_rk4_inputs(
    rho: &[f64],
    h1: &[f64],
    h2: &[f64],
    h3: &[f64],
    h4: &[f64],
    collapse_ops: &[f64],
    num_ops: usize,
    d: usize,
    dt: f64,
) -> PyResult<usize> {
    let element_count = matrix_element_count(d)?;
    validate_matrix_len("rho", rho.len(), element_count, d)?;
    validate_matrix_len("h1", h1.len(), element_count, d)?;
    validate_matrix_len("h2", h2.len(), element_count, d)?;
    validate_matrix_len("h3", h3.len(), element_count, d)?;
    validate_matrix_len("h4", h4.len(), element_count, d)?;
    validate_collapse_ops_len(collapse_ops.len(), num_ops, element_count, d)?;
    if !dt.is_finite() {
        return Err(PyValueError::new_err("dt must be finite"));
    }
    Ok(element_count)
}

fn matmul_flat(a: &[f64], b: &[f64], d: usize) -> Vec<f64> {
    let mut c = vec![0.0_f64; 2 * d * d];
    let parallelism = if d >= 32 {
        Parallelism::Rayon(0)
    } else {
        Parallelism::None
    };

    // SAFETY: validation at every public entry point guarantees that a, b, and
    // c each contain d*d interleaved complex64 values. gemm::c64 has the same
    // repr(C) real/imag layout, and these strides describe square row-major
    // matrices without aliasing either immutable input with c.
    unsafe {
        gemm(
            d,
            d,
            d,
            c.as_mut_ptr() as *mut c64,
            1,
            d as isize,
            false,
            a.as_ptr() as *const c64,
            1,
            d as isize,
            b.as_ptr() as *const c64,
            1,
            d as isize,
            c64::new(0.0, 0.0),
            c64::new(1.0, 0.0),
            false,
            false,
            false,
            parallelism,
        );
    }
    c
}

fn add_scaled_flat(base: &[f64], delta: &[f64], scale: f64) -> Vec<f64> {
    base.iter()
        .zip(delta.iter())
        .map(|(base_value, delta_value)| base_value + scale * delta_value)
        .collect()
}

fn conjugate_transpose_flat(matrix: &[f64], d: usize) -> Vec<f64> {
    let mut adjoint = vec![0.0_f64; 2 * d * d];
    for i in 0..d {
        for j in 0..d {
            let source = 2 * (i * d + j);
            let target = 2 * (j * d + i);
            adjoint[target] = matrix[source];
            adjoint[target + 1] = -matrix[source + 1];
        }
    }
    adjoint
}

#[pymodule]
fn yuragi_strider_rust(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<DenseRk4Session>()?;
    m.add_function(wrap_pyfunction!(backend_name, m)?)?;
    m.add_function(wrap_pyfunction!(add_f64, m)?)?;
    m.add_function(wrap_pyfunction!(matmul_complex_flat, m)?)?;
    m.add_function(wrap_pyfunction!(lindblad_rhs_flat, m)?)?;
    m.add_function(wrap_pyfunction!(gksl_exponential_superoperator_flat, m)?)?;
    m.add_function(wrap_pyfunction!(gksl_piecewise_superoperator_flat, m)?)?;
    m.add_function(wrap_pyfunction!(rk4_evolve_flat, m)?)?;
    m.add_function(wrap_pyfunction!(apply_local_reset_sequence_flat, m)?)?;
    m.add_function(wrap_pyfunction!(binary_contrast_statistics_v1, m)?)?;
    m.add_function(wrap_pyfunction!(reset_training_analysis_v1, m)?)?;
    m.add_function(wrap_pyfunction!(evaluate_crosstalk_disruption_v1, m)?)?;
    m.add_class::<CrosstalkEvaluationRuntime>()?;
    m.add_class::<PyResetSession>()?;
    m.add_class::<PyRegisterSession>()?;
    m.add_function(wrap_pyfunction!(rk4_time_dependent_stages_flat, m)?)?;
    m.add_function(wrap_pyfunction!(rk4_time_dependent_step_flat, m)?)?;
    m.add_function(wrap_pyfunction!(rk4_evolve_cleaned_flat, m)?)?;
    m.add_function(wrap_pyfunction!(rk4_evolve_cleaned_samples_flat, m)?)?;
    Ok(())
}
