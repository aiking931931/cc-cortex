library (importable Python/JS/Rust module)
  ✓ evidence: pytest run with N/N passing AND `python -c "import
    lib; lib.foo(...)"` (or equivalent) showing actual return value.
  ✗ evidence: tests pass but the public API was never invoked
    from a fresh process.
