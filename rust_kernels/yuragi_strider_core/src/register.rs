//! Qubit register operations on one Rust-owned density matrix.
//!
//! Operand order is explicit, and qubit zero is the most significant tensor factor.
//! Sampling consumes a caller-provided uniform draw so a host can preserve its
//! existing random stream while the runtime's own generator is migrated.

use crate::reset::{ResetOperation, ResetState};

#[derive(Clone, Debug)]
pub struct IdleSpec {
    pub t1_us: f64,
    pub tphi_us: f64,
    pub equilibrium_excited_population: f64,
}

#[derive(Clone, Debug)]
pub enum RegisterOperation {
    Unitary {
        qubits: Vec<usize>,
        matrix: Vec<f64>,
    },
    Reset(ResetOperation),
    Depolarize {
        qubits: Vec<usize>,
        probability: f64,
    },
    Dephase {
        qubits: Vec<usize>,
    },
    Idle {
        qubits: Vec<usize>,
        specs: Vec<IdleSpec>,
        elapsed_us: f64,
    },
    Measure {
        qubits: Vec<usize>,
        draw: f64,
    },
}

#[derive(Clone, Debug)]
pub struct DensityRegister {
    qubits: usize,
    dimension: usize,
    state: ResetState,
}

impl DensityRegister {
    pub fn new(values: Vec<f64>, qubits: usize) -> Result<Self, String> {
        let state = ResetState::new(values, qubits, 2)?;
        let dimension = 2usize
            .checked_pow(u32::try_from(qubits).map_err(|_| "too many qubits")?)
            .ok_or("register dimension is too large")?;
        Ok(Self {
            qubits,
            dimension,
            state,
        })
    }

    pub fn values(&self) -> &[f64] {
        self.state.values()
    }

    /// Apply a batch atomically. Returned bit arrays correspond only to measurements.
    pub fn execute(&mut self, operations: &[RegisterOperation]) -> Result<Vec<Vec<u8>>, String> {
        self.execute_with_checkpoints(operations, &[])
            .map(|(measurements, _)| measurements)
    }

    /// Capture specified post-operation states without publishing a partially failed batch.
    pub fn execute_with_checkpoints(
        &mut self,
        operations: &[RegisterOperation],
        checkpoints: &[usize],
    ) -> Result<(Vec<Vec<u8>>, Vec<Vec<f64>>), String> {
        if checkpoints.iter().any(|index| *index >= operations.len())
            || checkpoints.windows(2).any(|pair| pair[0] >= pair[1])
        {
            return Err("checkpoint indices must be strictly ordered and within the batch".into());
        }
        let mut candidate = self.clone();
        let mut measurements = Vec::new();
        let mut snapshots = Vec::with_capacity(checkpoints.len());
        for (index, operation) in operations.iter().enumerate() {
            match operation {
                RegisterOperation::Unitary { qubits, matrix } => {
                    candidate.apply_unitary(qubits, matrix)?
                }
                RegisterOperation::Reset(op) => {
                    candidate.state.execute(std::slice::from_ref(op))?
                }
                RegisterOperation::Depolarize {
                    qubits,
                    probability,
                } => candidate.depolarize(qubits, *probability)?,
                RegisterOperation::Dephase { qubits } => candidate.dephase(qubits)?,
                RegisterOperation::Idle {
                    qubits,
                    specs,
                    elapsed_us,
                } => candidate.idle(qubits, specs, *elapsed_us)?,
                RegisterOperation::Measure { qubits, draw } => {
                    measurements.push(candidate.measure(qubits, *draw)?)
                }
            }
            if checkpoints.get(snapshots.len()) == Some(&index) {
                snapshots.push(candidate.values().to_vec());
            }
        }
        *self = candidate;
        Ok((measurements, snapshots))
    }

    fn check_qubits(&self, qubits: &[usize]) -> Result<(), String> {
        if qubits.is_empty()
            || qubits.iter().any(|q| *q >= self.qubits)
            || qubits
                .iter()
                .collect::<std::collections::HashSet<_>>()
                .len()
                != qubits.len()
        {
            return Err("invalid register operands".into());
        }
        Ok(())
    }

    fn local_index(&self, index: usize, qubits: &[usize]) -> usize {
        qubits.iter().fold(0, |out, q| {
            (out << 1) | ((index >> (self.qubits - q - 1)) & 1)
        })
    }

    fn replace_index(&self, mut index: usize, qubits: &[usize], local: usize) -> usize {
        for (i, q) in qubits.iter().enumerate() {
            let bit = 1usize << (self.qubits - q - 1);
            if (local >> (qubits.len() - i - 1)) & 1 == 0 {
                index &= !bit;
            } else {
                index |= bit;
            }
        }
        index
    }

    fn apply_unitary(&mut self, qubits: &[usize], matrix: &[f64]) -> Result<(), String> {
        self.check_qubits(qubits)?;
        let size = 1usize
            .checked_shl(u32::try_from(qubits.len()).map_err(|_| "too many operands")?)
            .ok_or("operator dimension is too large")?;
        let length = size
            .checked_mul(size)
            .and_then(|v| v.checked_mul(2))
            .ok_or("operator dimension is too large")?;
        if matrix.len() != length || matrix.iter().any(|v| !v.is_finite()) {
            return Err("invalid operand operator".into());
        }
        let dim = self.dimension;
        let mut left = vec![0.0; self.state.values().len()];
        let mut out = vec![0.0; left.len()];
        for row in 0..dim {
            let local_row = self.local_index(row, qubits);
            for col in 0..dim {
                let pos = 2 * (row * dim + col);
                for k in 0..size {
                    let m = 2 * (local_row * size + k);
                    let source = 2 * (self.replace_index(row, qubits, k) * dim + col);
                    let (ar, ai, br, bi) = (
                        matrix[m],
                        matrix[m + 1],
                        self.state.values()[source],
                        self.state.values()[source + 1],
                    );
                    left[pos] += ar * br - ai * bi;
                    left[pos + 1] += ar * bi + ai * br;
                }
            }
        }
        for row in 0..dim {
            for col in 0..dim {
                let local_col = self.local_index(col, qubits);
                let pos = 2 * (row * dim + col);
                for k in 0..size {
                    let m = 2 * (local_col * size + k);
                    let source = 2 * (row * dim + self.replace_index(col, qubits, k));
                    let (ar, ai, br, bi) =
                        (left[source], left[source + 1], matrix[m], -matrix[m + 1]);
                    out[pos] += ar * br - ai * bi;
                    out[pos + 1] += ar * bi + ai * br;
                }
            }
        }
        self.state.replace_values(out)
    }

    fn depolarize(&mut self, qubits: &[usize], probability: f64) -> Result<(), String> {
        self.check_qubits(qubits)?;
        if !probability.is_finite() || !(0.0..=1.0).contains(&probability) {
            return Err("invalid depolarizing probability".into());
        }
        if probability == 0.0 {
            return Ok(());
        }
        let original = self.state.values().to_vec();
        let op = ResetOperation {
            targets: qubits.to_vec(),
            success_probabilities: vec![1.0; qubits.len()],
            target_populations: vec![0.5; 2 * qubits.len()],
            reset_count: 1,
        };
        self.state.execute(&[op])?;
        let result = original
            .iter()
            .zip(self.state.values())
            .map(|(a, b)| (1.0 - probability) * a + probability * b)
            .collect();
        self.state.replace_values(result)
    }

    fn dephase(&mut self, qubits: &[usize]) -> Result<(), String> {
        self.check_qubits(qubits)?;
        let mask = qubits
            .iter()
            .fold(0usize, |mask, q| mask | (1usize << (self.qubits - q - 1)));
        let mut out = self.state.values().to_vec();
        for row in 0..self.dimension {
            for col in 0..self.dimension {
                if row & mask != col & mask {
                    let pos = 2 * (row * self.dimension + col);
                    out[pos] = 0.0;
                    out[pos + 1] = 0.0;
                }
            }
        }
        self.state.replace_values(out)
    }

    fn idle(&mut self, qubits: &[usize], specs: &[IdleSpec], elapsed: f64) -> Result<(), String> {
        self.check_qubits(qubits)?;
        if qubits.len() != specs.len() || !elapsed.is_finite() || elapsed < 0.0 {
            return Err("invalid idle interval or specification count".into());
        }
        for spec in specs {
            if spec.t1_us.is_nan()
                || spec.t1_us <= 0.0
                || spec.tphi_us.is_nan()
                || spec.tphi_us <= 0.0
                || !spec.equilibrium_excited_population.is_finite()
                || !(0.0..=1.0).contains(&spec.equilibrium_excited_population)
            {
                return Err("invalid idle specification".into());
            }
        }
        let values = self.state.values();
        let mut trace_real = 0.0;
        let mut trace_imag = 0.0;
        for row in 0..self.dimension {
            let diagonal = 2 * (row * self.dimension + row);
            trace_real += values[diagonal];
            trace_imag += values[diagonal + 1];
            for col in 0..self.dimension {
                let a = 2 * (row * self.dimension + col);
                let b = 2 * (col * self.dimension + row);
                if (values[a] - values[b]).hypot(values[a + 1] + values[b + 1]) > 1e-10 {
                    return Err("idle state must be Hermitian with unit trace".into());
                }
            }
        }
        if (trace_real - 1.0).hypot(trace_imag) > 1e-10 {
            return Err("idle state must be Hermitian with unit trace".into());
        }
        if elapsed == 0.0 {
            return Ok(());
        }
        for (qubit, spec) in qubits.iter().zip(specs) {
            if spec.t1_us.is_infinite() && spec.tphi_us.is_infinite() {
                continue;
            }
            let relaxation = elapsed / spec.t1_us;
            let damping = -(-relaxation).exp_m1();
            let coherence = (-0.5 * relaxation - elapsed / spec.tphi_us).exp();
            let population = spec.equilibrium_excited_population;
            let bit = 1usize << (self.qubits - qubit - 1);
            let mut out = self.state.values().to_vec();
            let old = self.state.values();
            for row in 0..self.dimension {
                for col in 0..self.dimension {
                    let target = 2 * (row * self.dimension + col);
                    let row_one = row & bit != 0;
                    let col_one = col & bit != 0;
                    if row_one != col_one {
                        out[target] = old[target] * coherence;
                        out[target + 1] = old[target + 1] * coherence;
                    } else {
                        let zero = 2 * ((row & !bit) * self.dimension + (col & !bit));
                        let one = 2 * ((row | bit) * self.dimension + (col | bit));
                        let (weight_zero, weight_one) = if row_one {
                            (population * damping, 1.0 - (1.0 - population) * damping)
                        } else {
                            (1.0 - population * damping, (1.0 - population) * damping)
                        };
                        out[target] = weight_zero * old[zero] + weight_one * old[one];
                        out[target + 1] = weight_zero * old[zero + 1] + weight_one * old[one + 1];
                    }
                }
            }
            self.state.replace_values(out)?;
        }
        Ok(())
    }

    fn measure(&mut self, qubits: &[usize], draw: f64) -> Result<Vec<u8>, String> {
        self.check_qubits(qubits)?;
        if !draw.is_finite() || !(0.0..1.0).contains(&draw) {
            return Err("measurement draw must be in [0, 1)".into());
        }
        let outcomes = 1usize
            .checked_shl(u32::try_from(qubits.len()).map_err(|_| "too many operands")?)
            .ok_or("measurement size too large")?;
        let mut marginal = vec![0.0; outcomes];
        let mut total = 0.0;
        for i in 0..self.dimension {
            let probability = self.state.values()[2 * (i * self.dimension + i)];
            if !probability.is_finite() || probability < -1e-10 {
                return Err("invalid measurement probability distribution".into());
            }
            total += probability;
            marginal[self.local_index(i, qubits)] += probability.max(0.0);
        }
        if (total - 1.0).abs() > 1e-10 {
            return Err("invalid measurement probability distribution".into());
        }
        let point = draw * marginal.iter().sum::<f64>();
        let mut cumulative = 0.0;
        let mut outcome = outcomes - 1;
        for (index, probability) in marginal.iter().enumerate() {
            cumulative += probability;
            if point < cumulative {
                outcome = index;
                break;
            }
        }
        let probability = marginal[outcome];
        if probability <= 0.0 {
            return Err("sampled a zero-probability measurement branch".into());
        }
        let mut collapsed = vec![0.0; self.state.values().len()];
        for row in 0..self.dimension {
            if self.local_index(row, qubits) != outcome {
                continue;
            }
            for col in 0..self.dimension {
                if self.local_index(col, qubits) == outcome {
                    let pos = 2 * (row * self.dimension + col);
                    collapsed[pos] = self.state.values()[pos] / probability;
                    collapsed[pos + 1] = self.state.values()[pos + 1] / probability;
                }
            }
        }
        self.state.replace_values(collapsed)?;
        Ok((0..qubits.len())
            .map(|i| ((outcome >> (qubits.len() - i - 1)) & 1) as u8)
            .collect())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bell_measurement_and_failed_batch_are_atomic() {
        let mut initial = vec![0.0; 32];
        initial[0] = 1.0;
        let mut register = DensityRegister::new(initial, 2).unwrap();
        let h = 1.0 / 2.0_f64.sqrt();
        let h_gate = RegisterOperation::Unitary {
            qubits: vec![0],
            matrix: vec![h, 0.0, h, 0.0, h, 0.0, -h, 0.0],
        };
        let mut cx = vec![0.0; 32];
        for (row, col) in [(0, 0), (1, 1), (2, 3), (3, 2)] {
            cx[2 * (row * 4 + col)] = 1.0;
        }
        let cx_gate = RegisterOperation::Unitary {
            qubits: vec![0, 1],
            matrix: cx,
        };
        register.execute(&[h_gate.clone(), cx_gate]).unwrap();
        let before = register.values().to_vec();
        let failed = register.execute(&[
            h_gate,
            RegisterOperation::Measure {
                qubits: vec![0],
                draw: 1.0,
            },
        ]);
        assert!(failed.is_err());
        assert_eq!(register.values(), before);
        let bits = register
            .execute(&[RegisterOperation::Measure {
                qubits: vec![0, 1],
                draw: 0.75,
            }])
            .unwrap();
        assert_eq!(bits, vec![vec![1, 1]]);
        assert!((register.values()[2 * 15] - 1.0).abs() < 1e-12);
    }
}
