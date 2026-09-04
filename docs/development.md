# Development and verification

Run the complete test suite from any directory with the project as working tree:

```bash
pytest -q
```

The suite audits every JSON file, verifies package-key normalization and strict
missing-profile errors, checks Debian/Devuan init package selection, exercises
mock output routing for ISO/IMG/QCOW2/VDI/VMDK/tarball/OCI, and constructs a real
small OCI archive whose manifest digest is verified.

Build the HTML manual with warnings treated as errors:

```bash
sphinx-build -W -b html docs docs/_build/html
```
