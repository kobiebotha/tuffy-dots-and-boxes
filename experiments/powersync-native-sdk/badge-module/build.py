#!/usr/bin/env python3
"""Link the full SDK bridge into an isolated Tufty Badgeware native module."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
NATIVE = HERE / "native"
APP = HERE.parents[2]
COMPONENT = APP / "native-sqlite"
SDK = APP.parent / "powersync-native"
TOOLS = APP.parent / "powerbadge/native/.tools"
ARM = TOOLS / "arm-gnu-toolchain-13.2.Rel1-darwin-arm64-arm-none-eabi/bin"
PYTHON = COMPONENT / ".venv/bin/python"
RUST = TOOLS / "rustup/toolchains/1.96.0-aarch64-apple-darwin/bin"


def build_rust(environment):
    rust_env = dict(environment, CARGO_HOME=str(HERE.parent / ".cargo-home"),
                    RUSTC=str(RUST / "rustc"),
                    GIT_CEILING_DIRECTORIES=str(HERE.parent / ".cargo-home/registry"),
                    # RP2350 exclusive transactions do not work in PSRAM.
                    # This isolated binding has one foreground owner, no Rust
                    # IRQ callbacks and no concurrent access to SDK memory.
                    # Lower all LLVM atomics to ordinary accesses only here;
                    # the reusable SDK retains its normal atomic semantics.
                    CARGO_TARGET_THUMBV8M_MAIN_NONE_EABI_RUSTFLAGS="-C target-cpu=cortex-m33 -C relocation-model=pic -C panic=abort -C passes=lower-atomic")
    command = [RUST / "cargo", "build", "--manifest-path", HERE / "rust/Cargo.toml",
        "--target", "thumbv8m.main-none-eabi", "--target-dir", HERE / "rust/target",
        "--release", "--locked", "--offline", "--message-format=json-render-diagnostics"]
    result = capture("rust-build.log", command, rust_env, quiet=True)
    def identity(output):
        identities = []
        for line in output.splitlines():
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("reason") == "build-script-executed" and "powersync_core@" in message.get("package_id", ""):
                identities.extend(v for k, v in message.get("env", []) if k == "GIT_HASH")
        return identities
    if identity(result.stdout) != ["cratesio"]:
        # build.rs does not declare GIT_CEILING_DIRECTORIES as an input, so a
        # previous cached compile may have inherited the consuming app's HEAD.
        # Clean only this crate in this experiment's disposable target folder.
        capture("core-identity-clean.log", [RUST / "cargo", "clean", "--manifest-path", HERE / "rust/Cargo.toml",
            "-p", "powersync_core", "--target", "thumbv8m.main-none-eabi", "--release",
            "--target-dir", HERE / "rust/target"], rust_env, quiet=True)
        result = capture("rust-build.log", command, rust_env, quiet=True)
    if identity(result.stdout) != ["cratesio"]:
        raise SystemExit("Core source archive must identify as 0.5.4/cratesio, never inherit the app Git HEAD")
    return HERE / "rust/target/thumbv8m.main-none-eabi/release/libpsbadge_native.a"


def prepare_sqlite():
    manifest = json.loads((COMPONENT / "sqlite-adaptations.json").read_text())
    source = (COMPONENT / "vendor/sqlite/sqlite3.c").read_bytes()
    if hashlib.sha256(source).hexdigest() != manifest["source_sha256"]:
        raise SystemExit("Pinned SQLite source checksum changed")
    text = source.decode()
    for change in manifest["changes"]:
        if text.count(change["before"]) != change["count"]:
            raise SystemExit("Pinned SQLite adaptation no longer applies")
        text = text.replace(change["before"], change["after"])
    encoded = text.encode()
    if hashlib.sha256(encoded).hexdigest() != manifest["adapted_sha256"]:
        raise SystemExit("Adapted SQLite checksum changed")
    generated = NATIVE / "build/generated/sqlite3-adapted.inc"
    generated.parent.mkdir(parents=True, exist_ok=True)
    if not generated.exists() or generated.read_bytes() != encoded:
        generated.write_bytes(encoded)


def capture(name, command, environment, *, check=True, quiet=False):
    log = HERE / "evidence" / name
    log.parent.mkdir(parents=True, exist_ok=True)
    if not quiet:
        print("+ " + " ".join(map(str, command)), flush=True)
    result = subprocess.run(list(map(str, command)), cwd=NATIVE, env=environment,
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log.write_text("$ " + " ".join(map(str, command)) + "\n" + result.stdout)
    if not quiet or result.returncode:
        print(result.stdout[-8000:], end="", flush=True)
    if check and result.returncode:
        raise SystemExit("Command failed; see " + str(log))
    return result


def audit_archive(archive, environment):
    """Check the SDK's LTO unit before asking the native linker to place it."""
    members = subprocess.check_output([str(ARM / "arm-none-eabi-ar"), "t", str(archive)], text=True)
    member = next((m for m in members.splitlines() if m.startswith("psbadge_native-") and m.endswith(".o")), None)
    if member is None:
        raise SystemExit("Archive lacks the actual psbadge_native SDK bridge object")
    folder = NATIVE / "build/audit"
    folder.mkdir(parents=True, exist_ok=True)
    obj = folder / "bridge.o"
    obj.write_bytes(subprocess.check_output([str(ARM / "arm-none-eabi-ar"), "p", str(archive), member]))
    capture("bridge-attributes.log", [ARM / "arm-none-eabi-readelf", "-A", obj], environment, quiet=True)
    capture("bridge-sections.log", [ARM / "arm-none-eabi-readelf", "-WS", obj], environment, quiet=True)
    capture("bridge-relocations.log", [ARM / "arm-none-eabi-readelf", "-Wr", obj], environment, quiet=True)
    capture("bridge-symbols.log", [ARM / "arm-none-eabi-readelf", "-Ws", obj], environment, quiet=True)
    script = r'''
import json,sys
from elftools.elf.elffile import ELFFile
from elftools.elf.relocation import RelocationSection
with open(sys.argv[1],'rb') as f:
 e=ELFFile(f)
 data=[];fixed=[];types={};sizes={}
 for s in e.iter_sections():
  if s['sh_type'] in ('SHT_PROGBITS','SHT_NOBITS') and s['sh_size']:
   if s.name.startswith('.data') and not s.name.startswith('.data.rel.ro'): data.append(s.name)
   for prefix in ('.text','.rodata','.data.rel.ro','.bss'):
    if s.name.startswith(prefix):sizes[prefix]=sizes.get(prefix,0)+s['sh_size'];break
  if not isinstance(s,RelocationSection):continue
  target=e.get_section(s['sh_info'])
  if not target.name.startswith(('.text','.rodata','.data.rel.ro','.bss')):continue
  syms=e.get_section(s['sh_link'])
  for r in s.iter_relocations():
   t=r['r_info_type'];types[str(t)]=types.get(str(t),0)+1
   sym=syms.get_symbol(r['r_info_sym']);idx=sym['st_shndx']
   if target.name.startswith('.text') and t in (3,10,30) and isinstance(idx,int):
    sec=e.get_section(idx)
    if sec.name.startswith('.bss'):fixed.append({'symbol':sym.name,'section':target.name,'type':t})
 print(json.dumps({'object':sys.argv[1],'nonempty_mutable_data':data,'fixed_text_to_bss':fixed,'relocation_type_counts':types,'bridge_object_section_bytes':sizes},indent=2))
 if data or fixed:sys.exit(2)
'''
    result = capture("bridge-audit.log", [PYTHON, "-c", script, obj], environment, check=False, quiet=True)
    (HERE / "evidence/bridge-audit.json").write_text(result.stdout)
    if result.returncode:
        raise SystemExit("SDK archive violates native loader section/relocation rules; see evidence/bridge-audit.json")
    # Audit every member, including precompiled compiler-builtins, rather than
    # assuming LTO lowered only the user crate. Any selected exclusive loop on
    # PSRAM would never complete on RP2350.
    disassembly = capture("archive-disassembly.log", [ARM / "arm-none-eabi-objdump", "-d", archive],
                          environment, quiet=True)
    exclusives = [line for line in disassembly.stdout.splitlines()
                  if re.search(r"\b(?:ldrex|strex|ldaex|stlex)(?:b|h|d)?\b", line)]
    atomic_report = dict(scope="every SDK archive object, including compiler-builtins",
        candidateOnly=True, atomicLowering="LLVM lower-atomic",
        requiresSingleForegroundOwner=True, concurrentSdkHeapAccessAllowed=False,
        exclusiveInstructionCount=len(exclusives), exclusiveInstructions=exclusives)
    (HERE / "evidence/atomic-audit.json").write_text(json.dumps(atomic_report, indent=2) + "\n")
    if exclusives:
        raise SystemExit("Exclusive instructions remain in the PSRAM SDK archive; see evidence/atomic-audit.json")


def audit_link_inputs(archive, environment):
    """Repeat archive resolution and inspect every C/Rust/runtime object used."""
    gcc = ARM / "arm-none-eabi-gcc"
    profile = [str(gcc), "-mthumb", "-mcpu=cortex-m4", "-mfpu=fpv4-sp-d16", "-mfloat-abi=softfp"]
    libgcc = subprocess.check_output(profile + ["--print-libgcc-file-name"], text=True).strip()
    libm = subprocess.check_output(profile + ["--print-file-name=libm.a"], text=True).strip()
    objects = [NATIVE / ("build/sdk/src/" + n + ".o") for n in ("module", "sqlite_port", "libc_shim", "vfs")]
    script = r'''
import json,re,subprocess,sys
from pathlib import Path
tools,objdump,report,log,*inputs=sys.argv[1:]
sys.path.insert(0,tools)
import mpy_ld,ar_util
env=mpy_ld.LinkEnv('armv7emsp')
objects=[Path(p) for p in inputs[:4]]
for p in objects:
 with p.open('rb') as f:mpy_ld.load_object_file(env,f,str(p))
ar_util.init_cache('.mpy_ld_cache-armv7emsp',ar_util.DEFAULT_CACHE_PREFIX)
archives=[]
for p in inputs[4:]:archives.extend(ar_util.load_archive(p))
selected,_=ar_util.resolve(archives,set(s.name for s in env.unresolved_syms))
folder=Path('build/audit/selected');folder.mkdir(parents=True,exist_ok=True)
sources=[str(p) for p in objects]
for i,(ar,member) in enumerate(selected):
 p=folder/(str(i)+'.o')
 with ar.open(member) as f:p.write_bytes(f.read())
 objects.append(p);sources.append(ar.fn+':'+member)
bad=[]
with open(log,'w') as out:
 for p,source in zip(objects,sources):
  result=subprocess.run([objdump,'-d',str(p)],text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,check=True)
  out.write('OBJECT '+source+'\n'+result.stdout)
  for line in result.stdout.splitlines():
   if re.search(r'\b(?:ldrex|strex|ldaex|stlex)(?:b|h|d)?\b',line):bad.append({'object':source,'instruction':line})
data={'scope':'every object selected for final native module, including C, SDK and runtime libraries','objectsChecked':len(objects),'selectedObjects':sources,'exclusiveInstructionCount':len(bad),'exclusiveInstructions':bad,'requiresSingleForegroundOwner':True,'atomicLowering':'LLVM lower-atomic for isolated SDK bridge'}
Path(report).write_text(json.dumps(data,indent=2)+'\n')
print('Final native object audit:',len(objects),'objects,',len(bad),'exclusive instructions')
if bad:sys.exit(2)
'''
    capture("final-atomic-audit.log", [PYTHON, "-c", script, COMPONENT / "vendor/micropython/tools",
        ARM / "arm-none-eabi-objdump", HERE / "evidence/final-atomic-audit.json",
        HERE / "evidence/final-disassembly.log", *objects, archive, libgcc, libm], environment, quiet=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, help="link a supplied SDK bridge archive instead of building it first")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--no-cache", action="store_true", help="remove this experiment's generated linker cache")
    args = parser.parse_args()
    prepare_sqlite()
    environment = dict(os.environ, PATH=str(ARM) + os.pathsep + os.environ.get("PATH", ""),
                       PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1")
    archive = args.archive.resolve() if args.archive else build_rust(environment)
    if not archive.is_file():
        raise SystemExit("Missing full SDK archive: " + str(archive))
    version = subprocess.check_output([str(ARM / "arm-none-eabi-gcc"), "-dumpfullversion"], text=True).strip()
    if version != "13.2.1":
        raise SystemExit("Expected qualified ARM GCC 13.2.1")
    if args.no_cache:
        cache = NATIVE / ".mpy_ld_cache-armv7emsp"
        if cache.is_symlink():
            raise SystemExit("Refusing symlinked experiment linker cache")
        if cache.exists():
            shutil.rmtree(cache)
    audit_archive(archive, environment)
    command = ["make", "-C", str(NATIVE), "BUILD=build/sdk", "PYTHON=" + str(PYTHON),
               "SDK_ARCHIVE=" + str(archive), "MPY_DIR=" + str(COMPONENT / "vendor/micropython"),
               "SQLITE_DIR=" + str(COMPONENT / "vendor/sqlite"), "all"]
    if args.verbose:
        command.append("V=1")
    capture("build.log", command, environment)
    audit_link_inputs(archive, environment)
    artifact = NATIVE / "native_powersync_sdk.mpy"
    if artifact.read_bytes()[:4] != bytes((77, 6, 31, 31)):
        raise SystemExit("Candidate does not match Tufty ABI 7942")
    capture("archive-symbols.log", [ARM / "arm-none-eabi-nm", "-g", archive], environment, quiet=True)
    for name in ["module", "sqlite_port", "vfs", "libc_shim"]:
        obj = NATIVE / ("build/sdk/src/" + name + ".o")
        capture(name + "-attributes.log", [ARM / "arm-none-eabi-readelf", "-A", obj], environment, quiet=True)
        capture(name + "-sections.log", [ARM / "arm-none-eabi-readelf", "-WS", obj], environment, quiet=True)
    destination = HERE / "dist"
    destination.mkdir(exist_ok=True)
    output = destination / artifact.name
    shutil.copyfile(artifact, output)
    pins = json.loads((COMPONENT / "dependencies.json").read_text())
    inputs = [NATIVE / "Makefile", *sorted((NATIVE / "src").glob("*")), HERE / "build.py"]
    bridge_inputs = [HERE / "rust/Cargo.toml", HERE / "rust/Cargo.lock", HERE / "rust/psbadge.h",
                     *sorted((HERE / "rust/src").rglob("*.rs")),
                     *sorted((HERE / "vendor").rglob("*.rs")),
                     *sorted((HERE / "vendor").rglob("Cargo.toml"))]
    sdk_inputs = [SDK / "Cargo.lock", SDK / "powersync/Cargo.toml",
                  *sorted((SDK / "powersync/src").rglob("*.rs"))]
    metadata = dict(file=output.name, bytes=output.stat().st_size,
        sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
        sdkBaseRevision=subprocess.check_output(["git", "-C", str(SDK), "rev-parse", "HEAD"], text=True).strip(),
        sdkArchiveSha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        sourceFiles={str(p.relative_to(HERE)):hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs if p.is_file()},
        bridgeSourceFiles={str(p.relative_to(HERE)):hashlib.sha256(p.read_bytes()).hexdigest() for p in bridge_inputs if p.is_file()},
        sdkSourceFiles={str(p.relative_to(SDK)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sdk_inputs if p.is_file()},
        sqlite=pins["sqlite"]["version"], powersync=pins["powersync"]["version"],
        micropython=pins["micropython"]["version"], micropythonCommit=pins["micropython"]["commit"],
        mpy=7942, board="tufty2350", floatAbi="softfp", armGcc=version,
        kind="native-powersync-sdk-candidate", firmwareReplaced=False)
    metadata.update(atomicLowering="LLVM lower-atomic", requiresSingleForegroundOwner=True,
                    concurrentSdkHeapAccessAllowed=False, exclusiveInstructionCount=0)
    (destination / "module.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print("Candidate:", output, metadata["bytes"], metadata["sha256"])


if __name__ == "__main__":
    main()
