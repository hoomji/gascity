# Retired panel research

The v3.1 requested-action instrument is the validated classifier. The previous silver, collapse and two-stage judge panels did not supply a trustworthy operational reference; operational disagreement should be checked against dispatch/ledgers/model-grades.tsv and operator-reviewed assignments. These historical experiments are retained for reproducibility, not exposed as production CLI commands. No database schema change is required.

Run the archived offline tests from the repository root:

```sh
PYTHONPATH=contrib/agent-observatory:contrib/agent-observatory/archive python3 -m unittest discover -s contrib/agent-observatory/archive/tests
```

The former CLI is preserved in panel/cli.py solely for offline reproduction by the archived tests; the installed production runtime excludes archive/. Active adapters, collector, store, jev.py and transport.py are unchanged.
