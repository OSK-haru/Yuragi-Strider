//! First staged slice of `/api/circuit/compile`.
//!
//! `logical_direct` is implemented here. `auto_decompose` remains on FastAPI
//! until all gate rewrite rules and source maps have been ported and verified.

use serde_json::{Map, Value, json};

const NATIVE_GATE_SET_ID: &str = "gate_aware_hxyzst_rz_cnot_v3";
const GATE_NAMES: &[&str] = &[
    "H", "X", "Y", "Z", "S", "T", "RX", "RY", "RZ", "CNOT", "CZ", "CP", "CCX", "SWAP", "QFT",
    "ORACLE", "MEASURE", "MESSAGE", "RECEIVED",
];

#[derive(Debug)]
pub struct CompileError {
    pub status: axum::http::StatusCode,
    pub detail: Value,
}

pub fn compile_preview(request: &Value) -> Result<Value, CompileError> {
    let mode = request
        .get("compilation_mode")
        .and_then(Value::as_str)
        .unwrap_or("logical_direct");
    if mode == "auto_decompose" {
        return Err(CompileError {
            status: axum::http::StatusCode::NOT_IMPLEMENTED,
            detail: json!({
                "code": "RUST_COMPILER_MODE_PENDING",
                "message": "Rust auto_decompose is not available in this migration slice."
            }),
        });
    }
    if mode != "logical_direct" {
        return Err(invalid(
            "compilation mode must be logical_direct or auto_decompose.",
        ));
    }
    let durations = request
        .get("gate_duration_defaults")
        .cloned()
        .unwrap_or_else(|| json!({}));
    validate_durations(&durations)?;
    let circuit = match request
        .get("circuit_config")
        .filter(|value| !value.is_null())
    {
        Some(config) => build_custom(config, &durations)?,
        None => build_preset(
            request.get("circuit_preset").and_then(Value::as_str),
            &durations,
        )?,
    };
    validate_circuit(&circuit)?;
    compile_direct(circuit)
}

fn invalid(message: &str) -> CompileError {
    CompileError {
        status: axum::http::StatusCode::UNPROCESSABLE_ENTITY,
        detail: json!({"message": message}),
    }
}

fn defaults(gate: &str) -> f64 {
    match gate {
        "H" | "X" | "Y" | "RX" | "RY" | "RZ" => 0.02,
        "Z" | "S" | "T" | "MEASURE" => 0.0,
        "CNOT" | "CZ" | "SWAP" | "CP" => 0.2,
        "CCX" => 0.4,
        "QFT" | "ORACLE" => 0.2,
        "MESSAGE" => 0.04,
        _ => 0.0,
    }
}

fn duration(values: &Value, gate: &str) -> Result<f64, CompileError> {
    let value = values
        .get(gate)
        .and_then(Value::as_f64)
        .unwrap_or_else(|| defaults(gate));
    if !value.is_finite()
        || value < 0.0
        || (value == 0.0 && !matches!(gate, "Z" | "S" | "T" | "MEASURE"))
    {
        return Err(invalid("gate duration defaults contain an invalid value."));
    }
    Ok(value)
}

fn validate_durations(values: &Value) -> Result<(), CompileError> {
    for gate in [
        "H", "X", "Y", "Z", "S", "T", "RX", "RY", "RZ", "CNOT", "CZ", "SWAP", "CP", "CCX", "QFT",
        "ORACLE", "MEASURE", "MESSAGE",
    ] {
        if let Some(value) = values.get(gate) {
            let number = value
                .as_f64()
                .ok_or_else(|| invalid("gate duration defaults must be numbers."))?;
            if !number.is_finite()
                || number < 0.0
                || (number == 0.0 && !matches!(gate, "Z" | "S" | "T" | "MEASURE"))
            {
                return Err(invalid("gate duration defaults contain an invalid value."));
            }
        }
    }
    Ok(())
}

fn gate(kind: &str, targets: Vec<usize>, controls: Vec<usize>, duration: f64) -> Value {
    json!({"type":kind,"targets":targets,"controls":controls,"params":{"duration_us":duration},
        "classical_targets":[],"condition":null,"conditions":[]})
}

fn conditional_gate(kind: &str, target: usize, duration: f64, conditions: Value) -> Value {
    let mut value = gate(kind, vec![target], vec![], duration);
    value["condition"] = conditions
        .as_array()
        .and_then(|items| items.first())
        .cloned()
        .unwrap_or(Value::Null);
    value["conditions"] = conditions;
    value
}

fn build_preset(preset: Option<&str>, d: &Value) -> Result<Value, CompileError> {
    let preset = preset.ok_or_else(|| invalid("circuit_preset or circuit_config is required."))?;
    let h = duration(d, "H")?;
    let x = duration(d, "X")?;
    let z = duration(d, "Z")?;
    let cnot = duration(d, "CNOT")?;
    let measure = duration(d, "MEASURE")?;
    let (n, initial, classical, columns) = match preset {
        "bell" => {
            let columns = vec![
                json!({"step":0,"gates":[gate("H",vec![0],vec![],h)]}),
                json!({"step":1,"gates":[gate("CNOT",vec![1],vec![0],cnot)]}),
            ];
            (2, json!(["0", "0"]), 0, columns)
        }
        "teleportation" => {
            let m0 = json!([{"bit":0,"value":1}]);
            let m1 = json!([{"bit":1,"value":1}]);
            let mut measure0 = gate("MEASURE", vec![0], vec![], measure);
            measure0["classical_targets"] = json!([0]);
            let mut measure1 = gate("MEASURE", vec![1], vec![], measure);
            measure1["classical_targets"] = json!([1]);
            let columns = vec![
                json!({"step":0,"gates":[gate("H",vec![1],vec![],h)]}),
                json!({"step":1,"gates":[gate("CNOT",vec![2],vec![1],cnot)]}),
                json!({"step":2,"gates":[gate("CNOT",vec![1],vec![0],cnot)]}),
                json!({"step":3,"gates":[gate("H",vec![0],vec![],h)]}),
                json!({"step":4,"gates":[measure0]}),
                json!({"step":5,"gates":[measure1]}),
                json!({"step":6,"gates":[conditional_gate("X",2,x,m1)]}),
                json!({"step":7,"gates":[conditional_gate("Z",2,z,m0)]}),
            ];
            (3, json!(["+", "0", "0"]), 2, columns)
        }
        "bit_flip_repetition" => {
            let mut m0 = gate("MEASURE", vec![3], vec![], measure);
            m0["classical_targets"] = json!([0]);
            let mut m1 = gate("MEASURE", vec![4], vec![], measure);
            m1["classical_targets"] = json!([1]);
            let columns = vec![
                json!({"step":0,"gates":[gate("CNOT",vec![1],vec![0],cnot)]}),
                json!({"step":1,"gates":[gate("CNOT",vec![2],vec![0],cnot)]}),
                json!({"step":2,"gates":[gate("X",vec![1],vec![],x)]}),
                json!({"step":3,"gates":[gate("CNOT",vec![3],vec![0],cnot)]}),
                json!({"step":4,"gates":[gate("CNOT",vec![3],vec![1],cnot)]}),
                json!({"step":5,"gates":[gate("CNOT",vec![4],vec![1],cnot)]}),
                json!({"step":6,"gates":[gate("CNOT",vec![4],vec![2],cnot)]}),
                json!({"step":7,"gates":[m0]}),
                json!({"step":8,"gates":[m1]}),
                json!({"step":9,"gates":[conditional_gate("X",0,x,json!([{"bit":0,"value":1},{"bit":1,"value":0}]))]}),
                json!({"step":10,"gates":[conditional_gate("X",1,x,json!([{"bit":0,"value":1},{"bit":1,"value":1}]))]}),
                json!({"step":11,"gates":[conditional_gate("X",2,x,json!([{"bit":0,"value":0},{"bit":1,"value":1}]))]}),
            ];
            (5, json!(["1", "0", "0", "0", "0"]), 2, columns)
        }
        _ => {
            return Err(invalid(
                "circuit_preset must be bell, teleportation, or bit_flip_repetition.",
            ));
        }
    };
    Ok(
        json!({"logical_qubits":n,"initial_states":initial,"classical_bits":classical,"columns":columns,"annotations":[]}),
    )
}

fn build_custom(config: &Value, defaults: &Value) -> Result<Value, CompileError> {
    let n = config
        .get("logical_qubits")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid("logical_qubits is required."))?;
    let raw_columns = config
        .get("columns")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if raw_columns.len() > 200 {
        return Err(invalid("circuit exceeds the maximum number of columns."));
    }
    let mut total_gates = 0usize;
    let mut columns = Vec::new();
    let mut annotations = config
        .get("annotations")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if annotations.len() > 200 {
        return Err(invalid(
            "circuit exceeds the maximum number of annotations.",
        ));
    }
    for column in &raw_columns {
        let step = column.get("step").and_then(Value::as_u64).unwrap_or(0);
        let raw_gates = column
            .get("gates")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        if raw_gates.len() > 16 {
            return Err(invalid("circuit exceeds the maximum gates per column."));
        }
        total_gates += raw_gates.len();
        if total_gates > 800 {
            return Err(invalid(
                "circuit exceeds the maximum number of API gate operations.",
            ));
        }
        let mut gates = Vec::new();
        for (index, raw) in raw_gates.iter().enumerate() {
            let kind = raw
                .get("type")
                .and_then(Value::as_str)
                .ok_or_else(|| invalid("gate type is required."))?;
            if !GATE_NAMES.contains(&kind) {
                return Err(invalid("unsupported circuit gate."));
            }
            if kind == "RECEIVED" {
                annotations.push(json!({"kind":"RECEIVED","id":format!("received-column-{step}-gate-{index}"),"source_id":null,"column_index":step,"qubits":raw.get("targets").cloned().unwrap_or_else(||json!([]))}));
                continue;
            }
            let targets = raw
                .get("targets")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            let controls = raw
                .get("controls")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            let params = raw
                .get("params")
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default();
            let mut normalized = Map::new();
            for (key, value) in params {
                if ![
                    "duration_us",
                    "theta_rad",
                    "control_value",
                    "control_state",
                    "animation_parameter_t",
                    "marked_index",
                ]
                .contains(&key.as_str())
                {
                    return Err(invalid("unsupported gate parameter."));
                }
                normalized.insert(key, value);
            }
            if !normalized.contains_key("duration_us") {
                let base = if kind == "QFT" || kind == "ORACLE" {
                    duration(defaults, kind)? * targets.len() as f64
                } else {
                    duration(defaults, kind)?
                };
                normalized.insert("duration_us".to_owned(), json!(base));
            }
            let condition = raw.get("condition").cloned().unwrap_or(Value::Null);
            let conditions = raw
                .get("conditions")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            let mut conditions = conditions;
            if !condition.is_null() && !conditions.contains(&condition) {
                conditions.insert(0, condition.clone());
            }
            let condition = conditions.first().cloned().unwrap_or(Value::Null);
            gates.push(json!({"type":kind,"targets":targets,"controls":controls,"params":normalized,
                "classical_targets":raw.get("classical_targets").cloned().unwrap_or_else(||json!([])),"condition":condition,"conditions":conditions}));
        }
        columns.push(json!({"step":step,"gates":gates}));
    }
    let initial_states = config
        .get("initial_states")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
        .into_iter()
        .map(|state| match state.as_u64() {
            Some(value) => json!(value.to_string()),
            None => state,
        })
        .collect::<Vec<_>>();
    Ok(json!({"logical_qubits":n,"initial_states":initial_states,
        "classical_bits":config.get("classical_bits").and_then(Value::as_u64).unwrap_or(0),"columns":columns,"annotations":annotations}))
}

fn validate_circuit(circuit: &Value) -> Result<(), CompileError> {
    let n = circuit["logical_qubits"].as_u64().unwrap_or(0) as usize;
    let initials = circuit["initial_states"]
        .as_array()
        .ok_or_else(|| invalid("initial_states is required."))?;
    if n == 0 || n > 18 || initials.len() != n {
        return Err(invalid("initial_states must match logical_qubits."));
    }
    for initial in initials {
        if !matches!(initial.as_str(), Some("0" | "1" | "+" | "-"))
            && !matches!(initial.as_u64(), Some(0 | 1))
        {
            return Err(invalid("initial state is unsupported."));
        }
    }
    for column in circuit["columns"].as_array().into_iter().flatten() {
        for gate in column["gates"].as_array().into_iter().flatten() {
            let kind = gate["type"].as_str().unwrap_or("");
            if !GATE_NAMES.contains(&kind) {
                return Err(invalid("unsupported circuit gate."));
            }
            let targets = gate["targets"].as_array().map(Vec::as_slice).unwrap_or(&[]);
            let controls = gate["controls"]
                .as_array()
                .map(Vec::as_slice)
                .unwrap_or(&[]);
            for q in gate["targets"]
                .as_array()
                .into_iter()
                .flatten()
                .chain(gate["controls"].as_array().into_iter().flatten())
            {
                if q.as_u64().is_none_or(|q| q as usize >= n) {
                    return Err(invalid("Gate operand is outside the logical qubit range."));
                }
            }
            let valid_shape = match kind {
                "CNOT" => {
                    !controls.is_empty()
                        && targets.len() == 1
                        && controls.len() + 1
                            == controls
                                .iter()
                                .chain(targets)
                                .collect::<std::collections::HashSet<_>>()
                                .len()
                }
                "CZ" | "CP" => {
                    controls.len() == 1 && targets.len() == 1 && controls[0] != targets[0]
                }
                "CCX" => {
                    controls.len() == 2
                        && targets.len() == 1
                        && controls[0] != controls[1]
                        && controls[0] != targets[0]
                        && controls[1] != targets[0]
                }
                "SWAP" => controls.is_empty() && targets.len() == 2 && targets[0] != targets[1],
                "QFT" | "ORACLE" => {
                    controls.is_empty()
                        && !targets.is_empty()
                        && targets
                            .iter()
                            .collect::<std::collections::HashSet<_>>()
                            .len()
                            == targets.len()
                }
                "RECEIVED" => true,
                _ => targets.len() == 1 && controls.is_empty(),
            };
            if !valid_shape {
                return Err(invalid("gate operands do not match the gate type."));
            }
            if let Some(params) = gate["params"].as_object() {
                for (name, value) in params {
                    if let Some(number) = value.as_f64() {
                        if !number.is_finite() || (name == "duration_us" && number < 0.0) {
                            return Err(invalid("gate parameters contain an invalid number."));
                        }
                    }
                }
            }
        }
    }
    Ok(())
}

fn compile_direct(circuit: Value) -> Result<Value, CompileError> {
    let columns = circuit["columns"].as_array().cloned().unwrap_or_default();
    let mut sorted = columns.clone();
    sorted.sort_by_key(|column| column["step"].as_u64().unwrap_or(0));
    let mut compiled_columns = Vec::new();
    let mut source_map = Vec::new();
    let mut logical_count = 0usize;
    let mut duration_sum = 0.0;
    for (logical_index, column) in sorted.iter().enumerate() {
        let step = compiled_columns.len();
        let gates = column["gates"].as_array().cloned().unwrap_or_default();
        logical_count += gates.len();
        let column_duration = gates
            .iter()
            .filter_map(|gate| gate["params"]["duration_us"].as_f64())
            .fold(0.0f64, f64::max);
        duration_sum += column_duration;
        for (gate_index, gate) in gates.iter().enumerate() {
            let kind = gate["type"].as_str().unwrap_or("").to_uppercase();
            source_map.push(json!({"logical_column":logical_index,"logical_gate_index":gate_index,"source_gate":kind,
                "targets":gate["targets"],"controls":gate["controls"],"rule_id":null,
                "compiled_operations":[{"compiled_column":step,"gate":kind,"targets":gate["targets"],"controls":gate["controls"],"params":gate["params"]}]}));
        }
        compiled_columns.push(json!({"step":step,"gates":gates}));
    }
    let mut compiled = circuit;
    compiled["columns"] = json!(compiled_columns);
    let depth = compiled_columns.len();
    Ok(
        json!({"compilation":{"mode":"logical_direct","native_gate_set_id":NATIVE_GATE_SET_ID,"logical_gate_count":logical_count,
        "compiled_gate_count":logical_count,"logical_depth":depth,"compiled_depth":depth,"logical_duration_us":duration_sum,
        "compiled_duration_us":duration_sum,"decomposition_rules_used":[],"source_map":source_map,"compiled_circuit":compiled}}),
    )
}

#[cfg(test)]
mod tests {
    use super::compile_preview;
    use serde_json::{Value, json};

    const BELL_DIRECT_CONTRACT: &str =
        include_str!("../contracts/circuit-compile-bell-logical-direct-v1.json");

    #[test]
    fn bell_direct_preview_keeps_the_compiler_contract() {
        let actual = compile_preview(&json!({"circuit_preset":"bell"})).unwrap();
        let expected: Value = serde_json::from_str(BELL_DIRECT_CONTRACT).unwrap();
        assert_eq!(actual, expected);
    }

    #[test]
    fn auto_decompose_is_explicitly_unavailable_in_this_slice() {
        let error =
            compile_preview(&json!({"circuit_preset":"bell","compilation_mode":"auto_decompose"}))
                .unwrap_err();
        assert_eq!(error.status, axum::http::StatusCode::NOT_IMPLEMENTED);
    }
}
