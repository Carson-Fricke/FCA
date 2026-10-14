# Fixed workflow graph

Pure control logic for the existing workflow host. No persistence, conversations,
provider calls, domain folds, clocks or scheduling belong here.

- `__init__.py`: Intentional graph projection and validation facade.
- `topology.py`: Validates fixed split/join regions and lexical branch ownership.
- `control.py`: Reconstructs current positions, exact region occurrences and scoped
  accepted handoffs from the authoritative execution history.

The execution store remains the sole accepted history. Live completion and restart
use the same projection; domain extensions receive only each position's causal path.
