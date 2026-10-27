// Minimal independent proof that the installed target has core/alloc, not std.
// The full SDK failure is reproduced separately by reproduce.py.
#![no_std]
extern crate std;

pub fn std_requirement() -> std::time::Duration {
    std::time::Duration::from_millis(1)
}
