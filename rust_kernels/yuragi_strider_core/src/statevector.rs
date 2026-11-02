//! Ideal circuit execution and binary readout assignment in native Rust.
//!
//! Qubit zero is the most-significant bit. This path stores only `2^n`
//! amplitudes and accepts a whole operation list so language bindings cross
//! the boundary once per circuit.

use std::f64::consts::{FRAC_1_SQRT_2, PI};
use crate::register::{DensityRegister, RegisterOperation};

type Complex = (f64, f64);

#[derive(Clone, Debug)]
pub struct StatevectorOperation {
    pub kind: String,
    pub targets: Vec<usize>,
    pub controls: Vec<usize>,
    pub theta_rad: f64,
    pub marked_index: usize,
    pub control_value: u8,
    pub control_state: Option<usize>,
    pub conditions: Vec<(usize, u8)>,
}

pub fn execute_ideal_circuit(
    logical_qubits: usize,
    classical_bits: usize,
    initial_states: &[String],
    operations: &[StatevectorOperation],
) -> Result<Vec<f64>, String> {
    if logical_qubits == 0 || logical_qubits > 18 || initial_states.len() != logical_qubits {
        return Err("initial_states must match logical_qubits in the supported range".into());
    }
    let dimension = 1usize
        .checked_shl(logical_qubits as u32)
        .ok_or("statevector dimension is too large")?;
    let mut state = initial_amplitudes(initial_states)?;
    if state.len() != dimension { return Err("statevector dimension is inconsistent".into()); }

    let mut classical = vec![0u8; classical_bits];
    for operation in operations {
        if operation.conditions.iter().any(|(_, value)| *value > 1) {
            return Err("classical condition values must be 0 or 1".into());
        }
        if operation.conditions.iter().any(|(bit, value)| {
            *bit >= classical.len() || classical[*bit] != *value
        }) {
            if operation.conditions.iter().any(|(bit, _)| *bit >= classical.len()) {
                return Err("classical condition is outside the register range".into());
            }
            continue;
        }
        apply_operation(&mut state, logical_qubits, operation)?;
    }
    let mut probabilities = state
        .iter()
        .map(|amplitude| amplitude.0 * amplitude.0 + amplitude.1 * amplitude.1)
        .collect::<Vec<_>>();
    normalize_probabilities(&mut probabilities)?;
    Ok(probabilities)
}

pub fn apply_binary_readout(
    probabilities: &[f64],
    logical_qubits: usize,
    assignment_errors: &[(f64, f64)],
) -> Result<Vec<f64>, String> {
    if logical_qubits == 0 || logical_qubits > 18 {
        return Err("logical_qubits is outside the supported range".into());
    }
    let dimension = 1usize << logical_qubits;
    if probabilities.len() != dimension || assignment_errors.len() != logical_qubits {
        return Err("probabilities and assignment_errors must match logical_qubits".into());
    }
    if probabilities.iter().any(|value| !value.is_finite() || *value < -1e-10) {
        return Err("invalid output probability distribution".into());
    }
    let mut observed = probabilities.iter().map(|value| value.max(0.0)).collect::<Vec<_>>();
    normalize_probabilities(&mut observed)?;
    for (qubit, (p10, p01)) in assignment_errors.iter().copied().enumerate() {
        if !p10.is_finite() || !p01.is_finite() || !(0.0..1.0).contains(&p10) || !(0.0..1.0).contains(&p01) || p10 + p01 >= 1.0 {
            return Err(format!("readout assignment span must be positive for qubit {qubit}"));
        }
        let stride = 1usize << (logical_qubits - qubit - 1);
        for block in (0..dimension).step_by(stride << 1) {
            for offset in block..block + stride {
                let zero = observed[offset];
                let one = observed[offset + stride];
                observed[offset] = (1.0 - p10) * zero + p01 * one;
                observed[offset + stride] = p10 * zero + (1.0 - p01) * one;
            }
        }
    }
    Ok(observed)
}

pub fn execute_ideal_density_circuit(
    logical_qubits: usize,
    initial_states: &[String],
    operations: &[(usize, StatevectorOperation)],
) -> Result<Vec<f64>, String> {
    if logical_qubits == 0 || logical_qubits > 8 || initial_states.len() != logical_qubits {
        return Err("initial_states must match logical_qubits in the supported density range".into());
    }
    let amplitudes = initial_amplitudes(initial_states)?;
    let dimension = 1usize << logical_qubits;
    let mut density = vec![0.0; 2 * dimension * dimension];
    for row in 0..dimension {
        for column in 0..dimension {
            let value = multiply(amplitudes[row], (amplitudes[column].0, -amplitudes[column].1));
            let pos = 2 * (row * dimension + column);
            density[pos] = value.0;
            density[pos + 1] = value.1;
        }
    }
    let mut register = DensityRegister::new(density, logical_qubits)?;
    let mut sorted = operations.iter().collect::<Vec<_>>();
    sorted.sort_by_key(|(step, _)| *step);
    let mut batch = Vec::with_capacity(sorted.len());
    let mut current_step = None;
    let mut measured_qubits = Vec::new();
    for (step, operation) in sorted {
        if current_step.is_some_and(|previous| previous != *step) {
            push_measurement_dephasing(&mut batch, &mut measured_qubits);
        }
        current_step = Some(*step);
        if operation.kind.eq_ignore_ascii_case("MEASURE") {
            validate_operands(&[], &operation.targets, logical_qubits)?;
            measured_qubits.extend(operation.targets.iter().copied());
            continue;
        }
        let (qubits, matrix) = local_unitary(operation)?;
        batch.push(RegisterOperation::Unitary { qubits, matrix });
    }
    push_measurement_dephasing(&mut batch, &mut measured_qubits);
    register.execute(&batch)?;
    Ok(register.values().to_vec())
}

pub fn local_unitary(operation: &StatevectorOperation) -> Result<(Vec<usize>, Vec<f64>), String> {
    let mut qubits = operation.controls.clone();
    qubits.extend(operation.targets.iter().copied());
    validate_operands(&[], &qubits, 18)?;
    if qubits.is_empty() {
        return Err("unitary operation requires at least one operand".into());
    }
    let local_operation = StatevectorOperation {
        kind: operation.kind.clone(),
        targets: operation.targets.iter().map(|q| qubits.iter().position(|item| item == q).unwrap()).collect(),
        controls: operation.controls.iter().map(|q| qubits.iter().position(|item| item == q).unwrap()).collect(),
        theta_rad: operation.theta_rad,
        marked_index: operation.marked_index,
        control_value: operation.control_value,
        control_state: operation.control_state,
        conditions: Vec::new(),
    };
    let dimension = 1usize
        .checked_shl(qubits.len() as u32)
        .ok_or("local unitary dimension is too large")?;
    let mut matrix = vec![0.0; dimension * dimension * 2];
    for column in 0..dimension {
        let mut state = vec![(0.0, 0.0); dimension];
        state[column] = (1.0, 0.0);
        apply_operation(&mut state, qubits.len(), &local_operation)?;
        for (row, value) in state.into_iter().enumerate() {
            let pos = 2 * (row * dimension + column);
            matrix[pos] = value.0;
            matrix[pos + 1] = value.1;
        }
    }
    Ok((qubits, matrix))
}

fn initial_amplitudes(initial_states: &[String]) -> Result<Vec<Complex>, String> {
    let mut state = vec![(1.0, 0.0)];
    for initial in initial_states {
        let local = match initial.as_str() {
            "0" => [(1.0, 0.0), (0.0, 0.0)],
            "1" => [(0.0, 0.0), (1.0, 0.0)],
            "+" => [(FRAC_1_SQRT_2, 0.0), (FRAC_1_SQRT_2, 0.0)],
            "-" => [(FRAC_1_SQRT_2, 0.0), (-FRAC_1_SQRT_2, 0.0)],
            "+i" => [(FRAC_1_SQRT_2, 0.0), (0.0, FRAC_1_SQRT_2)],
            "-i" => [(FRAC_1_SQRT_2, 0.0), (0.0, -FRAC_1_SQRT_2)],
            _ => return Err(format!("unsupported initial state: {initial:?}")),
        };
        let mut expanded = Vec::with_capacity(state.len() * 2);
        for amplitude in state {
            expanded.push(multiply(amplitude, local[0]));
            expanded.push(multiply(amplitude, local[1]));
        }
        state = expanded;
    }
    Ok(state)
}

fn push_measurement_dephasing(batch: &mut Vec<RegisterOperation>, measured: &mut Vec<usize>) {
    measured.sort_unstable();
    measured.dedup();
    if !measured.is_empty() {
        batch.push(RegisterOperation::Dephase { qubits: std::mem::take(measured) });
    }
}

fn apply_operation(state: &mut [Complex], n: usize, op: &StatevectorOperation) -> Result<(), String> {
    let kind = op.kind.to_ascii_uppercase();
    match kind.as_str() {
        "I" | "H" | "X" | "Y" | "Z" | "S" | "T" | "RX" | "RY" | "RZ" | "MESSAGE" => {
            require_shape(op, 1, 0, n)?;
            let matrix = if kind == "MESSAGE" {
                message_matrix(op.theta_rad)?
            } else {
                single_matrix(&kind, op.theta_rad)?
            };
            apply_single(state, n, op.targets[0], matrix);
        }
        "CNOT" => {
            if op.controls.is_empty() || op.targets.len() != 1 || op.control_value > 1 {
                return Err("CNOT requires controls and one target".into());
            }
            validate_operands(&op.controls, &op.targets, n)?;
            let default_state = if op.controls.len() == 1 { op.control_value as usize } else { (1usize << op.controls.len()) - 1 };
            let control_state = op.control_state.unwrap_or(default_state);
            if control_state >= (1usize << op.controls.len()) {
                return Err("CNOT control_state is outside the control-bit range".into());
            }
            let control_masks = op.controls.iter().map(|q| bit_mask(n, *q)).collect::<Vec<_>>();
            let target_mask = bit_mask(n, op.targets[0]);
            for index in 0..state.len() {
                let active = control_masks.iter().enumerate().all(|(position, mask)| {
                    ((index & mask != 0) as usize) == ((control_state >> (op.controls.len() - position - 1)) & 1)
                });
                if active && index & target_mask == 0 {
                    state.swap(index, index | target_mask);
                }
            }
        }
        "CZ" | "CP" => {
            require_shape(op, 1, 1, n)?;
            if op.controls[0] == op.targets[0] {
                return Err("controlled phase operands must be different".into());
            }
            let phase = if kind == "CZ" { (-1.0, 0.0) } else { (op.theta_rad.cos(), op.theta_rad.sin()) };
            if !op.theta_rad.is_finite() { return Err("theta_rad must be finite".into()); }
            let control = bit_mask(n, op.controls[0]);
            let target = bit_mask(n, op.targets[0]);
            for (index, amplitude) in state.iter_mut().enumerate() {
                if index & control != 0 && index & target != 0 { *amplitude = multiply(*amplitude, phase); }
            }
        }
        "SWAP" => {
            require_shape(op, 2, 0, n)?;
            if op.targets[0] == op.targets[1] { return Err("SWAP targets must be different".into()); }
            let first = bit_mask(n, op.targets[0]);
            let second = bit_mask(n, op.targets[1]);
            for index in 0..state.len() {
                if index & first == 0 && index & second != 0 { state.swap(index, index ^ first ^ second); }
            }
        }
        "CCX" => {
            require_shape(op, 1, 2, n)?;
            if op.controls[0] == op.controls[1] || op.controls.contains(&op.targets[0]) { return Err("CCX operands must be different".into()); }
            let a = bit_mask(n, op.controls[0]);
            let b = bit_mask(n, op.controls[1]);
            let target = bit_mask(n, op.targets[0]);
            for index in 0..state.len() { if index & a != 0 && index & b != 0 && index & target == 0 { state.swap(index, index | target); } }
        }
        "QFT" => {
            if op.targets.is_empty() { return Err("QFT requires at least one target".into()); }
            validate_operands(&[], &op.targets, n)?;
            for position in 0..op.targets.len() {
                apply_single(state, n, op.targets[position], single_matrix("H", 0.0)?);
                for offset in 1..op.targets.len() - position {
                    let angle = PI / 2f64.powi(offset as i32);
                    apply_controlled_phase(state, n, op.targets[position + offset], op.targets[position], (angle.cos(), angle.sin()));
                }
            }
            for i in 0..op.targets.len() / 2 { swap_qubits(state, n, op.targets[i], op.targets[op.targets.len() - i - 1]); }
        }
        "ORACLE" => {
            if op.targets.is_empty() { return Err("ORACLE requires at least one target".into()); }
            validate_operands(&[], &op.targets, n)?;
            if op.marked_index >= (1usize << op.targets.len()) { return Err("ORACLE marked_index is outside the register range".into()); }
            let mut mask = 0usize;
            let mut marked = 0usize;
            for (position, qubit) in op.targets.iter().copied().enumerate() {
                let bit = bit_mask(n, qubit);
                mask |= bit;
                if (op.marked_index >> (op.targets.len() - position - 1)) & 1 != 0 { marked |= bit; }
            }
            for (index, amplitude) in state.iter_mut().enumerate() { if index & mask == marked { *amplitude = (-amplitude.0, -amplitude.1); } }
        }
        _ => return Err(format!("unsupported statevector gate: {}", op.kind)),
    }
    Ok(())
}

fn single_matrix(kind: &str, theta: f64) -> Result<[[Complex; 2]; 2], String> {
    let (s, c) = (theta / 2.0).sin_cos();
    let h = FRAC_1_SQRT_2;
    let matrix = match kind {
        "I" => [[(1.0, 0.0), (0.0, 0.0)], [(0.0, 0.0), (1.0, 0.0)]],
        "H" => [[(h, 0.0), (h, 0.0)], [(h, 0.0), (-h, 0.0)]],
        "X" => [[(0.0, 0.0), (1.0, 0.0)], [(1.0, 0.0), (0.0, 0.0)]],
        "Y" => [[(0.0, 0.0), (0.0, -1.0)], [(0.0, 1.0), (0.0, 0.0)]],
        "Z" => [[(1.0, 0.0), (0.0, 0.0)], [(0.0, 0.0), (-1.0, 0.0)]],
        "S" => [[(1.0, 0.0), (0.0, 0.0)], [(0.0, 0.0), (0.0, 1.0)]],
        "T" => [[(1.0, 0.0), (0.0, 0.0)], [(0.0, 0.0), (FRAC_1_SQRT_2, FRAC_1_SQRT_2)]],
        "RX" => [[(c, 0.0), (0.0, -s)], [(0.0, -s), (c, 0.0)]],
        "RY" => [[(c, 0.0), (-s, 0.0)], [(s, 0.0), (c, 0.0)]],
        "RZ" => [[((-theta / 2.0).cos(), (-theta / 2.0).sin()), (0.0, 0.0)], [(0.0, 0.0), ((theta / 2.0).cos(), (theta / 2.0).sin())]],
        _ => return Err(format!("unsupported statevector gate: {kind}")),
    };
    if !theta.is_finite() { return Err("theta_rad must be finite".into()); }
    Ok(matrix)
}

fn message_matrix(parameter: f64) -> Result<[[Complex; 2]; 2], String> {
    if !parameter.is_finite() { return Err("animation_parameter_t must be finite".into()); }
    let parameter = parameter.rem_euclid(1.0);
    let rx = single_matrix("RX", PI * parameter)?;
    let ry = single_matrix("RY", 2.0 * parameter)?;
    let mut result = [[(0.0, 0.0); 2]; 2];
    for row in 0..2 {
        for column in 0..2 {
            for inner in 0..2 { result[row][column] = add(result[row][column], multiply(rx[row][inner], ry[inner][column])); }
        }
    }
    Ok(result)
}

fn apply_single(state: &mut [Complex], n: usize, target: usize, matrix: [[Complex; 2]; 2]) {
    let mask = bit_mask(n, target);
    for index in 0..state.len() {
        if index & mask == 0 {
            let partner = index | mask;
            let (a, b) = (state[index], state[partner]);
            state[index] = add(multiply(matrix[0][0], a), multiply(matrix[0][1], b));
            state[partner] = add(multiply(matrix[1][0], a), multiply(matrix[1][1], b));
        }
    }
}

fn apply_controlled_phase(state: &mut [Complex], n: usize, control: usize, target: usize, phase: Complex) {
    let control = bit_mask(n, control);
    let target = bit_mask(n, target);
    for (index, amplitude) in state.iter_mut().enumerate() { if index & control != 0 && index & target != 0 { *amplitude = multiply(*amplitude, phase); } }
}

fn swap_qubits(state: &mut [Complex], n: usize, first: usize, second: usize) {
    let a = bit_mask(n, first);
    let b = bit_mask(n, second);
    for index in 0..state.len() { if index & a == 0 && index & b != 0 { state.swap(index, index ^ a ^ b); } }
}

fn validate_operands(controls: &[usize], targets: &[usize], n: usize) -> Result<(), String> {
    let all = controls.iter().chain(targets).copied().collect::<Vec<_>>();
    if all.iter().any(|q| *q >= n) || all.iter().collect::<std::collections::HashSet<_>>().len() != all.len() {
        return Err("invalid register operands".into());
    }
    Ok(())
}

fn require_shape(op: &StatevectorOperation, targets: usize, controls: usize, n: usize) -> Result<(), String> {
    if op.targets.len() != targets || op.controls.len() != controls { return Err("gate operands do not match the gate type".into()); }
    validate_operands(&op.controls, &op.targets, n)
}

fn bit_mask(n: usize, q: usize) -> usize { 1usize << (n - q - 1) }
fn multiply(a: Complex, b: Complex) -> Complex { (a.0 * b.0 - a.1 * b.1, a.0 * b.1 + a.1 * b.0) }
fn add(a: Complex, b: Complex) -> Complex { (a.0 + b.0, a.1 + b.1) }

fn normalize_probabilities(values: &mut [f64]) -> Result<(), String> {
    let total = values.iter().sum::<f64>();
    if !total.is_finite() || total <= 0.0 { return Err("invalid output probability distribution".into()); }
    for value in values { *value /= total; }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn operation(kind: &str, targets: Vec<usize>, controls: Vec<usize>) -> StatevectorOperation {
        StatevectorOperation { kind: kind.into(), targets, controls, theta_rad: 0.0, marked_index: 0, control_value: 1, control_state: None, conditions: vec![] }
    }

    #[test]
    fn bell_state_matches_most_significant_qubit_order() {
        let gates = [operation("H", vec![0], vec![]), operation("CNOT", vec![1], vec![0])];
        let probabilities = execute_ideal_circuit(2, 0, &["0".into(), "0".into()], &gates).unwrap();
        assert!((probabilities[0] - 0.5).abs() < 1e-12);
        assert!((probabilities[3] - 0.5).abs() < 1e-12);
        assert!(probabilities[1].abs() < 1e-12 && probabilities[2].abs() < 1e-12);
    }

    #[test]
    fn classical_conditions_run_inside_the_batch() {
        let mut x = operation("X", vec![0], vec![]);
        x.conditions = vec![(0, 1)];
        assert_eq!(execute_ideal_circuit(1, 1, &["0".into()], &[x.clone()]).unwrap(), vec![1.0, 0.0]);
        x.conditions = vec![(0, 0)];
        assert_eq!(execute_ideal_circuit(1, 1, &["0".into()], &[x]).unwrap(), vec![0.0, 1.0]);
    }

    #[test]
    fn qft_oracle_and_controlled_phase_preserve_probability() {
        let mut cp = operation("CP", vec![1], vec![0]);
        cp.theta_rad = PI / 3.0;
        let gates = [operation("H", vec![0], vec![]), operation("H", vec![1], vec![]), cp, operation("ORACLE", vec![0, 1], vec![]), operation("QFT", vec![0, 1], vec![])];
        let probabilities = execute_ideal_circuit(2, 0, &["0".into(), "0".into()], &gates).unwrap();
        assert!((probabilities.iter().sum::<f64>() - 1.0).abs() < 1e-12);
    }

    #[test]
    fn readout_assignment_is_tensor_local_and_preserves_mass() {
        let observed = apply_binary_readout(&[1.0, 0.0, 0.0, 0.0], 2, &[(0.1, 0.2), (0.3, 0.4)]).unwrap();
        assert!((observed[0] - 0.9 * 0.7).abs() < 1e-12);
        assert!((observed[1] - 0.9 * 0.3).abs() < 1e-12);
        assert!((observed[2] - 0.1 * 0.7).abs() < 1e-12);
        assert!((observed[3] - 0.1 * 0.3).abs() < 1e-12);
        assert!((observed.iter().sum::<f64>() - 1.0).abs() < 1e-12);
    }

    #[test]
    fn density_circuit_batch_matches_bell_state_and_nonselective_measurement() {
        let gates = [
            (0, operation("H", vec![0], vec![])),
            (1, operation("CNOT", vec![1], vec![0])),
        ];
        let state = execute_ideal_density_circuit(
            2,
            &["0".into(), "0".into()],
            &gates,
        )
        .unwrap();
        assert!((state[0] - 0.5).abs() < 1e-12);
        assert!((state[2 * (0 * 4 + 3)] - 0.5).abs() < 1e-12);
        assert!((state[2 * (3 * 4 + 0)] - 0.5).abs() < 1e-12);
        let measured = [
            (0, operation("H", vec![0], vec![])),
            (1, operation("MEASURE", vec![0], vec![])),
        ];
        let state = execute_ideal_density_circuit(
            1,
            &["0".into()],
            &measured,
        )
        .unwrap();
        assert!((state[0] - 0.5).abs() < 1e-12);
        assert!((state[6] - 0.5).abs() < 1e-12);
        assert!(state[2].abs() < 1e-12 && state[4].abs() < 1e-12);
    }
}
