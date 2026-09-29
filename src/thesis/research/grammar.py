"""Shared value grammars for Thesis research artifacts.

One definition per grammar, imported by every validator on the path that
advertises, collects, and composes a hypothesis contract, so the JSON Schema
handed to the model provider, the contract error collector, and the typed
composers can never drift apart.  Underscores are word characters in every
grammar here: statistical names such as ``gamma_ci_upper`` and ``data_ok`` are
the normal case, not the exception.
"""

from __future__ import annotations

import re

#: Safe identifiers for models, outputs, observables, symbols, and falsifier ids.
#: A leading digit or underscore is allowed; later characters may be dots,
#: colons, at-signs, slashes, and hyphens in addition to word characters.
IDENTIFIER_PATTERN = r"^[A-Za-z0-9_][A-Za-z0-9._:@/-]{0,127}$"

IDENTIFIER = re.compile(IDENTIFIER_PATTERN)

#: Lowercase workspace indicator names.
NAME_PATTERN = r"^[a-z][a-z0-9_]{0,127}$"

NAME = re.compile(NAME_PATTERN)

#: Semantic versions, for example ``1.0.0`` or ``2.1.0-rc.1``.
VERSION_PATTERN = r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$"

VERSION = re.compile(VERSION_PATTERN)


__all__ = [
    "IDENTIFIER",
    "IDENTIFIER_PATTERN",
    "NAME",
    "NAME_PATTERN",
    "VERSION",
    "VERSION_PATTERN",
]
