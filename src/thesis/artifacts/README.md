# Artifact import

This package imports current harness manifests and verified bytes into workspace publication. It rejects obsolete field spellings; conversion does not run in readers.

- `__init__.py` exports artifact import contracts and the importer.
- `importer.py` validates manifest provenance, hashes bytes, and publishes immutable workspace references.
