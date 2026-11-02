//! Bounded selection policy for semantic state snapshots.

const TIME_TOLERANCE_US: f64 = 1e-12;

#[derive(Clone, Debug)]
struct Candidate {
    original_index: usize,
    time_us: f64,
    kind: String,
    priority: u8,
}

/// Select source indices using the snapshot ordering and cap rules used by Circuit Studio.
pub fn select_snapshot_indices(
    candidates: &[(f64, String)],
    max_snapshots: usize,
) -> Result<(Vec<usize>, usize), String> {
    if max_snapshots == 0 {
        return Err("max_snapshots must be positive".into());
    }
    let mut ordered = candidates
        .iter()
        .enumerate()
        .map(|(original_index, (time_us, kind))| {
            let priority = priority(kind)?;
            if !time_us.is_finite() || *time_us < 0.0 {
                return Err("snapshot candidate times must be finite and non-negative".into());
            }
            Ok(Candidate {
                original_index,
                time_us: *time_us,
                kind: kind.clone(),
                priority,
            })
        })
        .collect::<Result<Vec<_>, String>>()?;
    ordered.sort_by(|left, right| left.time_us.total_cmp(&right.time_us));

    let mut deduped: Vec<Candidate> = Vec::new();
    for candidate in ordered {
        let existing = deduped.iter().position(|item| {
            (item.time_us - candidate.time_us).abs() <= TIME_TOLERANCE_US
        });
        if let Some(index) = existing {
            if candidate.priority > deduped[index].priority {
                deduped[index] = candidate;
            }
        } else {
            deduped.push(candidate);
        }
    }

    let deduplicated_count = candidates.len().saturating_sub(deduped.len());
    let selected = if deduped.len() <= max_snapshots {
        deduped
    } else {
        select_bounded(&deduped, max_snapshots)
    };
    let mut selected = selected;
    selected.sort_by(|left, right| left.time_us.total_cmp(&right.time_us));
    let selected_indices = selected
        .into_iter()
        .take(max_snapshots)
        .map(|candidate| candidate.original_index)
        .collect();
    Ok((selected_indices, deduplicated_count))
}

fn select_bounded(candidates: &[Candidate], max_snapshots: usize) -> Vec<Candidate> {
    let mut selected = candidates
        .iter()
        .filter(|candidate| matches!(candidate.kind.as_str(), "initial" | "final"))
        .cloned()
        .collect::<Vec<_>>();
    let mut remaining = max_snapshots.saturating_sub(selected.len());
    if remaining == 0 {
        return selected;
    }

    let custom = candidates
        .iter()
        .filter(|candidate| candidate.kind == "custom_time")
        .cloned()
        .collect::<Vec<_>>();
    append_prefix(&mut selected, &custom, remaining);
    remaining = max_snapshots.saturating_sub(selected.len());
    if remaining > 0 {
        let events = candidates
            .iter()
            .filter(|candidate| {
                matches!(candidate.kind.as_str(), "column_boundary" | "after_circuit")
                    && !contains_index(&selected, candidate.original_index)
            })
            .cloned()
            .collect::<Vec<_>>();
        append_prefix(&mut selected, &events, remaining);
        remaining = max_snapshots.saturating_sub(selected.len());
    }
    if remaining > 0 {
        let uniform = candidates
            .iter()
            .filter(|candidate| {
                matches!(candidate.kind.as_str(), "uniform_time" | "idle_sample")
                    && !contains_index(&selected, candidate.original_index)
            })
            .cloned()
            .collect::<Vec<_>>();
        selected.extend(even_subset(&uniform, remaining));
    }
    selected
}

fn append_prefix(selected: &mut Vec<Candidate>, candidates: &[Candidate], limit: usize) {
    for candidate in candidates.iter().take(limit) {
        if !contains_index(selected, candidate.original_index) {
            selected.push(candidate.clone());
        }
    }
}

fn even_subset(candidates: &[Candidate], limit: usize) -> Vec<Candidate> {
    if candidates.len() <= limit {
        return candidates.to_vec();
    }
    if limit == 0 {
        return Vec::new();
    }
    if limit == 1 {
        return vec![candidates[candidates.len() / 2].clone()];
    }
    let last = (candidates.len() - 1) as f64;
    (0..limit)
        .map(|index| {
            let candidate_index = (index as f64 * last / (limit - 1) as f64)
                .round_ties_even() as usize;
            candidates[candidate_index].clone()
        })
        .collect()
}

fn contains_index(candidates: &[Candidate], original_index: usize) -> bool {
    candidates
        .iter()
        .any(|candidate| candidate.original_index == original_index)
}

fn priority(kind: &str) -> Result<u8, String> {
    match kind {
        "idle_sample" => Ok(10),
        "uniform_time" => Ok(20),
        "column_boundary" => Ok(40),
        "measurement" => Ok(45),
        "after_circuit" => Ok(50),
        "initial" | "final" => Ok(110),
        "custom_time" => Ok(100),
        _ => Err(format!("unsupported snapshot kind: {kind}")),
    }
}

#[cfg(test)]
mod tests {
    use super::select_snapshot_indices;

    #[test]
    fn same_time_candidates_keep_the_highest_priority_kind() {
        let candidates = vec![
            (1.0, "uniform_time".into()),
            (1.0 + 5e-13, "column_boundary".into()),
            (1.0, "custom_time".into()),
        ];
        assert_eq!(select_snapshot_indices(&candidates, 10).unwrap(), (vec![2], 2));
    }

    #[test]
    fn bounded_policy_pins_endpoints_then_custom_then_events() {
        let candidates = vec![
            (0.0, "initial".into()),
            (0.5, "uniform_time".into()),
            (1.0, "custom_time".into()),
            (1.5, "column_boundary".into()),
            (2.0, "final".into()),
        ];
        assert_eq!(select_snapshot_indices(&candidates, 4).unwrap(), (vec![0, 2, 3, 4], 0));
    }

    #[test]
    fn even_subset_uses_ties_to_even_like_python_round() {
        let candidates = (0..6)
            .map(|index| (index as f64, "uniform_time".into()))
            .collect::<Vec<_>>();
        assert_eq!(select_snapshot_indices(&candidates, 3).unwrap(), (vec![0, 2, 5], 0));
    }
}
