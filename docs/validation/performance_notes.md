# Performance Notes

These timings are produced by the lightweight Phase 8 regression test.
They are informational and intentionally not used as strict pass/fail thresholds.

Scope: noise-free circuits over a 0.001 us window on the Python dense
backend only. They are not comparable to the Rust vs Python benchmark in
`formalweb/website/docs/performance/rust-acceleration.md`, which uses much
longer windows with noise; run `scripts/benchmark_rust_dense.py` for that.

| Operation | Elapsed seconds |
| --- | ---: |
| 1-qubit H | 0.000914 |
| 1-qubit X | 0.000844 |
| 2-qubit Bell | 0.001388 |
| 1-qubit comparison | 0.001703 |
| 2-qubit comparison | 0.002801 |
| expert data generation | 0.000842 |
| result JSON export | 0.000201 |
| result CSV export | 0.000038 |
| Markdown export | 0.001158 |
| save/load config | 0.004954 |
