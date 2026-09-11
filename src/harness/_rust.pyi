"""Type stub for the compiled PyO3 bridge ``harness._rust``.

The implementation is a platform-specific cdylib built from
``src/harness/py``; this stub gives static checkers (ty)
the exported surface without importing the compiled module.
"""

def smoke_roundtrip(value: int) -> int: ...
