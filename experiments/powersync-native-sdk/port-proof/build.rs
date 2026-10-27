use std::{env, path::PathBuf, process::Command};

fn main() {
    let root = PathBuf::from(env::var("CARGO_MANIFEST_DIR").unwrap()).join("../../..");
    let native = root.join("native-sqlite");
    let out = PathBuf::from(env::var("OUT_DIR").unwrap());
    let object = out.join("sqlite3.o");
    let status = Command::new("cc").args(["-std=c11", "-O2", "-D_DARWIN_C_SOURCE", "-include"])
        .arg(native.join("tests/host_config.h"))
        .arg("-I").arg(native.join("src"))
        .arg("-I").arg(native.join("vendor/sqlite"))
        .arg("-c").arg(native.join("vendor/sqlite/sqlite3.c"))
        .arg("-o").arg(&object).status().unwrap();
    assert!(status.success());
    assert!(Command::new("ar").arg("crs").arg(out.join("libbadge_sqlite.a"))
        .arg(object).status().unwrap().success());
    println!("cargo:rustc-link-search=native={}", out.display());
    println!("cargo:rustc-link-lib=static=badge_sqlite");
    println!("cargo:rustc-link-lib=m");
    for path in ["src/config.h", "tests/host_config.h", "vendor/sqlite/sqlite3.c"] {
        println!("cargo:rerun-if-changed={}", native.join(path).display());
    }
}
