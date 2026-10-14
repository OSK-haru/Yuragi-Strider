# Performance Notes

These timings are produced by the lightweight Phase 8 regression test.
They are informational and intentionally not used as strict pass/fail thresholds.

Scope: noise-free circuits over a 0.001 us window on the Python dense
backend only. They are not comparable to the Rust vs Python benchmark in
`formalweb/website/docs/performance/rust-acceleration.md`, which uses much
longer windows with noise; run `scripts/benchmark_rust_dense.py` for that.

| Operation | Elapsed seconds |
| --- | ---: |
| 1-qubit H | 0.001056 |
| 1-qubit X | 0.001987 |
| 2-qubit Bell | 0.001533 |
| 1-qubit comparison | 0.001744 |
| 2-qubit comparison | 0.002817 |
| expert data generation | 0.002547 |
| result JSON export | 0.000218 |
| result CSV export | 0.000045 |
| Markdown export | 0.000992 |
| save/load config | 0.003298 |
