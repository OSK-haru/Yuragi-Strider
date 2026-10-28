//! Dense local replacement channels, with Rust-owned state and atomic batches.

#[derive(Clone, Debug)]
pub struct ResetOperation {
    pub targets: Vec<usize>,
    pub success_probabilities: Vec<f64>,
    pub target_populations: Vec<f64>,
    pub reset_count: usize,
}

#[derive(Clone, Debug)]
pub struct ResetState {
    subsystem_count: usize,
    local_dimension: usize,
    dimension: usize,
    values: Vec<f64>,
}

impl ResetState {
    /// Interleaved real/imaginary, row-major density matrix; order matches the existing backend.
    pub fn new(
        values: Vec<f64>,
        subsystem_count: usize,
        local_dimension: usize,
    ) -> Result<Self, String> {
        if subsystem_count == 0 {
            return Err("subsystem_count must be greater than 0".into());
        }
        if local_dimension != 2 && local_dimension != 3 {
            return Err("local_dimension must be 2 or 3".into());
        }
        let exponent = u32::try_from(subsystem_count)
            .map_err(|_| "subsystem_count exceeds the supported exponent range")?;
        let dimension = local_dimension
            .checked_pow(exponent)
            .ok_or("register dimension is too large")?;
        let length = dimension
            .checked_mul(dimension)
            .and_then(|n| n.checked_mul(2))
            .ok_or("register dimension is too large")?;
        if values.len() != length {
            return Err("rho length does not match register dimension".into());
        }
        if values.iter().any(|x| !x.is_finite()) {
            return Err("rho must contain finite values".into());
        }
        Ok(Self {
            subsystem_count,
            local_dimension,
            dimension,
            values,
        })
    }

    pub fn values(&self) -> &[f64] {
        &self.values
    }

    pub(crate) fn replace_values(&mut self, values: Vec<f64>) -> Result<(), String> {
        if values.len() != self.values.len() || values.iter().any(|x| !x.is_finite()) {
            return Err("invalid register state output".into());
        }
        self.values = values;
        Ok(())
    }

    /// Execute all operations or leave the original state untouched on error.
    pub fn execute(&mut self, operations: &[ResetOperation]) -> Result<(), String> {
        for op in operations {
            self.validate(op)?;
        }
        let mut current = self.values.clone();
        let mut next = vec![0.0; current.len()];
        let local = self.local_dimension;
        let dim = self.dimension;
        for op in operations {
            for _ in 0..op.reset_count {
                for (index, target) in op.targets.iter().enumerate() {
                    let stride = local
                        .checked_pow(
                            u32::try_from(self.subsystem_count - target - 1)
                                .map_err(|_| "reset target exponent is out of range")?,
                        )
                        .ok_or("reset target stride is too large")?;
                    let populations = &op.target_populations[index * local..(index + 1) * local];
                    let success = op.success_probabilities[index];
                    let survival = 1.0 - success;
                    for row in 0..dim {
                        let row_level = (row / stride) % local;
                        let row_without = row - row_level * stride;
                        for column in 0..dim {
                            let pos = 2 * (row * dim + column);
                            next[pos] = survival * current[pos];
                            next[pos + 1] = survival * current[pos + 1];
                            let column_level = (column / stride) % local;
                            if row_level == column_level {
                                let column_without = column - column_level * stride;
                                let mut real = 0.0;
                                let mut imag = 0.0;
                                for level in 0..local {
                                    let source = 2
                                        * ((row_without + level * stride) * dim
                                            + column_without
                                            + level * stride);
                                    real += current[source];
                                    imag += current[source + 1];
                                }
                                let weight = success * populations[row_level];
                                next[pos] += weight * real;
                                next[pos + 1] += weight * imag;
                            }
                        }
                    }
                    std::mem::swap(&mut current, &mut next);
                }
            }
        }
        if current.iter().any(|x| !x.is_finite()) {
            return Err("reset output must contain finite values".into());
        }
        self.values = current;
        Ok(())
    }

    fn validate(&self, op: &ResetOperation) -> Result<(), String> {
        if op.targets.is_empty() {
            return Err("targets must not be empty".into());
        }
        if op
            .targets
            .iter()
            .any(|target| *target >= self.subsystem_count)
        {
            return Err("reset target is outside the register".into());
        }
        if op
            .targets
            .iter()
            .collect::<std::collections::HashSet<_>>()
            .len()
            != op.targets.len()
        {
            return Err("reset targets must be unique".into());
        }
        if op.success_probabilities.len() != op.targets.len() {
            return Err("success_probabilities length must equal targets length".into());
        }
        if op.targets.len().checked_mul(self.local_dimension) != Some(op.target_populations.len()) {
            return Err(
                "target_populations length must equal targets length * local_dimension".into(),
            );
        }
        if op
            .success_probabilities
            .iter()
            .any(|p| !p.is_finite() || *p < 0.0 || *p > 1.0)
        {
            return Err("success probabilities must be finite and between zero and one".into());
        }
        if op
            .target_populations
            .iter()
            .any(|p| !p.is_finite() || *p < 0.0)
        {
            return Err("target populations must be finite and non-negative".into());
        }
        for values in op.target_populations.chunks_exact(self.local_dimension) {
            if (values.iter().sum::<f64>() - 1.0).abs() > 1e-12 {
                return Err("each target population vector must sum to one".into());
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn batch_is_atomic_and_matches_ground_state_reset() {
        let mut state = ResetState::new(vec![0., 0., 0., 0., 0., 0., 1., 0.], 1, 2).unwrap();
        let reset = ResetOperation {
            targets: vec![0],
            success_probabilities: vec![1.0],
            target_populations: vec![1.0, 0.0],
            reset_count: 1,
        };
        let mut invalid = reset.clone();
        invalid.target_populations = vec![0.1, 0.1];
        assert!(state.execute(&[reset.clone(), invalid]).is_err());
        assert_eq!(state.values(), &[0., 0., 0., 0., 0., 0., 1., 0.]);
        state.execute(&[reset]).unwrap();
        assert_eq!(state.values(), &[1., 0., 0., 0., 0., 0., 0., 0.]);
    }
}
