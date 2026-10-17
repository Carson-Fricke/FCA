# Lake query application

`src/` owns coverage-backed file and snapshot planning, bounded SQL queries and
read projection. It consumes catalog, storage and acquisition ports; provider
execution and HTTP hosting remain outside this package. `tests/` verifies its
public query behavior.

`Cargo.toml` pins DuckDB exactly. Both workspace lockfiles resolve that same
wrapper/native engine pair so the container dependency stages reuse one native
build. Update both lockfiles together when changing that pin.
Keep the native build dependencies aligned too: a transitive build-script
dependency change also invalidates the compiled native artifact.
