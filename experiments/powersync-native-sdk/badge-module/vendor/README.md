# Candidate-only dependency relocation adaptation

`foldhash/` is the cached crates.io foldhash 0.2.0 source (licenses retained).
Its only changes are in `src/seed.rs`: the no_std per-hasher counter moves from a
private function-local static to the exported `PSBADGE_FOLDHASH_COUNTER` symbol,
and global seed storage gets the exported `PSBADGE_FOLDHASH_GLOBAL_SEED_STORAGE`
link name (without exposing its private Rust type).
The mixing algorithm, seed storage, memory ordering and hash behavior are
unchanged. The exported symbol gives Cortex-M PIC code a relocatable GOT
reference rather than a fixed PC-relative reference to module BSS, which the
MicroPython native loader cannot place next to code.

This patch applies only to the isolated badge bridge's Cargo dependency graph.
The installed registry cache and main SDK dependency source remain unchanged.
