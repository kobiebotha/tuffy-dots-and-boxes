#!/usr/bin/env python3
"""Inspect/install the isolated Native SDK app on the connected Tufty."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import time
from mpremote.transport_serial import SerialTransport

HERE = Path(__file__).resolve().parent
TARGET = "/system/apps/dots_boxes_native"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("inspect", "network-inspect", "recovery-live", "recovery-state", "recover", "storage", "backup-old-state", "clear-old-state", "install", "smoke", "diagnose", "reset"))
    parser.add_argument("--port", default="/dev/cu.usbmodem1101")
    parser.add_argument("--room")
    parser.add_argument("--move")
    args = parser.parse_args()
    device = SerialTransport(args.port)
    device.enter_raw_repl(soft_reset=args.mode != "recovery-live")

    def run(source, timeout=60):
        def consume(data):
            if data == b"\x04":
                return
            print(data.decode(errors="replace").replace("\r\n", "\n"), end="", flush=True)
        out, err = device.exec_raw(source, timeout=timeout, data_consumer=consume)
        if err:
            raise RuntimeError(err.decode(errors="replace"))
        return (out or b"").decode(errors="replace").replace("\r\n", "\n")

    try:
        print(run("import os,sys,machine,gc,json,binascii,hashlib; install_watchdog=machine.WDT(timeout=16000); install_watchdog.feed(); assert sys.implementation._mpy==7942 and sys.implementation._build=='tufty'; print(sys.implementation)"), end="", flush=True)
        if args.mode == "inspect":
            print(run("import badgeware,rp2,vfs; print('BADGEWARE',dir(badgeware)); print('RP2',dir(rp2)); print('MOUNTS',vfs.mount()); print('MODULES',list(sys.modules)); print('FREE',gc.mem_free()); print('FS',os.statvfs('/system'))"), end="", flush=True)
            print(run("install_fsmod=sys.modules.get('.frozen/badgeware/filesystem'); print('FILESYSTEM_MODULE',dir(install_fsmod)); print('FILESYSTEM_VALUES',[(k,str(v)) for k,v in install_fsmod.__dict__.items() if not k.startswith('__') and not callable(v)])"), end="", flush=True)
            return
        if args.mode == "reset":
            device.exec_raw_no_follow("machine.reset()")
            return
        if args.mode == "network-inspect":
            run("import badgeware,builtins,network;print('BADGE_BUILTINS_TYPE',type(badgeware.builtins));print('BADGE_BUILTINS_KEYS',list(badgeware.builtins) if isinstance(badgeware.builtins,dict) else dir(badgeware.builtins));sys.path.append('/system/lib');import network_runtime;print('NETWORK_RUNTIME',dir(network_runtime));print('WIFI',network.WLAN(network.STA_IF).isconnected());print('ROM_FILES',os.listdir('/rom'));print('BADGE_RUN',badgeware._run)")
            run("print('BARE_RUN',run,type(run));print('BARE_BADGE',type(badge));print('RUN_ATTRS',dir(run));print('GLOBALS_RUN',globals().get('run'))")
            return
        if args.mode == "recovery-live":
            # Interrupt the current foreground loop without resetting its
            # Python globals. Print status only, never credential dictionaries.
            run("print('LIVE_GLOBAL_NAMES',list(globals()));print('LIVE_APP_MODULES',[n for n in sys.modules if 'dots' in n or 'launch' in n])\nfor name,module in list(sys.modules.items()):\n values=getattr(module,'__dict__',{})\n obj=values.get('client')\n if obj is not None and hasattr(obj,'sync'):\n  link=obj.sync\n  print('LIVE_CLIENT',name,obj.mode,obj.failure)\n  if link is not None:print('LIVE_SYNC',link.phase,link.status,link.last_error,link.connected,link.synced,link._uploading)\ninstall_watchdog.feed()")
            return
        if args.mode == "recovery-state":
            run("sys.path.insert(0,%r);sys.path.insert(0,%r)\nfrom dots_storage import Store\nfrom dots_session import SessionFile\ninstall_store=Store(heartbeat=install_watchdog.feed)\ntry:\n install_store.open();install_record=SessionFile(install_store.root).load(install_store.new_uuid);install_move=install_record['pending_move'];print('SAVED_ROOM',install_record['room_id']);print('PENDING_MOVE',json.dumps(install_move));print('LOCAL_ROOM',json.dumps(install_store.room(install_record['room_id'])));print('PENDING_PUBLISHED',bool(install_move and install_store.has_move(install_move['id'],install_record['room_id'])));print('STATE_FREE',os.statvfs('/state'))\nfinally:\n install_store.close();install_watchdog.feed()" % (TARGET,TARGET+"/powersync"))
            return
        if args.mode == "recover":
            prefix = "RECOVERY_TARGET=%r;RECOVERY_EXPECTED_ROOM=%r;RECOVERY_EXPECTED_MOVE=%r\n" % (TARGET,args.room,args.move)
            run(prefix+(HERE/"recovery_probe.py").read_text(), timeout=180)
            return
        if args.mode == "storage":
            run("def inspect_tree(root):\n for name in os.listdir(root):\n  path=root.rstrip('/')+'/'+name;info=os.stat(path)\n  if info[0]&0xf000==0x4000:inspect_tree(path)\n  else:print('STATE_FILE',path,info[6])\n  install_watchdog.feed()\nprint('STATE_FREE',os.statvfs('/state'));inspect_tree('/state');print('ROOT_NAMES',os.listdir('/'))")
            return
        if args.mode == "clear-old-state":
            # Explicitly authorized by the operator after the storage diagnosis.
            # Only the old game's four known files are affected.
            run("clear_root='/state/dots_boxes';clear_names=os.listdir(clear_root);assert set(clear_names)=={'owner.txt','pending.json','session.json','state.sqlite'}\nfor n in clear_names:\n p=clear_root+'/'+n;assert os.stat(p)[0]&0xf000==0x8000\nfor n in clear_names:\n os.remove(clear_root+'/'+n);install_watchdog.feed()\nos.rmdir(clear_root);os.sync();print('OLD_GAME_ACTIVE_STATE_CLEARED');print('STATE_FREE',os.statvfs('/state'))")
            return
        if args.mode == "backup-old-state":
            # Preserve the previous game's bytes on the separate FAT volume.
            # This mode does not remove or change its active saved state.
            run("import rp2,vfs;install_mount=next(m for m,p in vfs.mount() if p=='/system');install_flash=rp2.Flash();install_mem=machine.mem32;install_bptr=install_mem[id(install_mount)+16];assert install_mem[install_bptr]==install_mem[id(install_flash)];install_base=install_mem[install_bptr+4];install_size=install_mem[install_bptr+8];install_default_base=install_mem[id(install_flash)+4];install_default_size=install_mem[id(install_flash)+8];assert install_default_base<=install_base and install_base+install_size<=install_default_base+install_default_size;install_bdev=rp2.Flash(start=install_base-install_default_base,len=install_size);install_writable=vfs.VfsFat(install_bdev);assert install_writable.statvfs('/')[:3]==os.statvfs('/system')[:3]")
            run("def backup_hash(path):\n h=hashlib.sha256();n=0\n with open(path,'rb') as f:\n  while True:\n   b=f.read(4096)\n   if not b:break\n   h.update(b);n+=len(b);install_watchdog.feed()\n return n,binascii.hexlify(h.digest()).decode()\nbackup_source='/state/dots_boxes';backup_destination='/system/backups/dots_boxes-before-native-20261026';backup_names=os.listdir(backup_source);assert set(backup_names)=={'owner.txt','pending.json','session.json','state.sqlite'};backup_before={n:backup_hash(backup_source+'/'+n) for n in backup_names}")
            mounted = False
            try:
                run("os.chdir('/');vfs.umount('/system');vfs.mount(install_writable,'/system',readonly=False);install_watchdog.feed()")
                mounted = True
                run("for p in ('/system/backups',backup_destination):\n try:os.mkdir(p)\n except OSError as e:\n  if e.args[0]!=17:raise\nfor n in backup_names:\n src=backup_source+'/'+n;dst=backup_destination+'/'+n\n try:\n  os.stat(dst)\n except OSError as e:\n  if e.args[0]!=2:raise\n  with open(src,'rb') as source,open(dst+'.copying','wb') as target:\n   while True:\n    data=source.read(4096)\n    if not data:break\n    assert target.write(data)==len(data);install_watchdog.feed()\n   target.flush()\n  assert backup_hash(dst+'.copying')==backup_before[n];os.rename(dst+'.copying',dst)\n assert backup_hash(dst)==backup_before[n];assert backup_hash(src)==backup_before[n]\nos.sync();print('BACKUP_VERIFIED',backup_destination,len(backup_names));print('ORIGINAL_STATE_UNCHANGED')")
            finally:
                if mounted:
                    run("os.chdir('/');os.sync();vfs.umount('/system');vfs.mount(install_mount,'/system',readonly=True);install_watchdog.feed();print('SYSTEM_READONLY_RESTORED')")
            return
        if args.mode == "diagnose":
            # Match app startup while reporting filenames/tracebacks only,
            # never pairing records, tokens or SQLite contents.
            run("sys.path.insert(0,%r);sys.path.insert(0,%r);print('STARTUP_FREE',gc.mem_free());print('STATE_FREE',os.statvfs('/state'))\ntry:\n print('STATE_FILES',[(n,os.stat('/state/dots_boxes_native/'+n)[6]) for n in os.listdir('/state/dots_boxes_native')])\nexcept OSError as e:\n print('STATE_DIRECTORY',e)" % (TARGET,TARGET+"/powersync"))
            run("from dots_storage import Store\nfrom dots_client import Client\nfrom dots_session import SessionFile\ninstall_store=Store(heartbeat=install_watchdog.feed)\ntry:\n print('STORE_OPEN_BEGIN');install_store.open();print('STORE_OPEN_OK');install_client=Client(install_store,SessionFile(install_store.root),heartbeat=install_watchdog.feed);print('CLIENT_START_OK',install_client.mode);install_client.close()\nexcept Exception as e:\n print('STARTUP_ERROR');sys.print_exception(e)\n if install_store.io:print('VFS_ERROR',install_store.io.last_error)\nfinally:\n install_store.close();install_watchdog.feed();print('STARTUP_DIAGNOSTIC_COMPLETE')")
            return
        if args.mode == "install":
            manifest = json.loads((HERE / "manifest.json").read_text())
            assert manifest["appId"] == "dots_boxes_native"
            payloads = {}
            for item in manifest["files"]:
                local = HERE / "apps/dots_boxes_native" / item["path"]
                assert local.is_file() and not local.is_symlink()
                data = local.read_bytes()
                assert len(data) == item["bytes"] and hashlib.sha256(data).hexdigest() == item["sha256"]
                payloads[item["path"]] = data
            # The exact pinned firmware permanently clears VfsFat's cached
            # writeblocks method on readonly mount. Reconstruct the same flash
            # partition from its *read-only* native object metadata, then check
            # geometry before mounting a fresh FAT wrapper. No flash is formatted.
            print(run("import rp2,vfs; install_mount=next(m for m,p in vfs.mount() if p=='/system'); install_flash=rp2.Flash(); install_mem=machine.mem32; install_bptr=install_mem[id(install_mount)+16]; assert install_mem[install_bptr]==install_mem[id(install_flash)]; install_base=install_mem[install_bptr+4]; install_size=install_mem[install_bptr+8]; install_default_base=install_mem[id(install_flash)+4]; install_default_size=install_mem[id(install_flash)+8]; assert install_default_base<=install_base and install_base+install_size<=install_default_base+install_default_size; assert install_size>=os.statvfs('/system')[0]*os.statvfs('/system')[2]; install_bdev=rp2.Flash(start=install_base-install_default_base,len=install_size); install_writable=vfs.VfsFat(install_bdev); assert install_writable.statvfs('/')[:3]==os.statvfs('/system')[:3]; print('PARTITION_GEOMETRY_VERIFIED',install_size)"), end="", flush=True)
            print(run("def install_hash(path):\n h=hashlib.sha256(); n=0\n with open(path,'rb') as f:\n  while True:\n   b=f.read(4096)\n   if not b: break\n   h.update(b);n+=len(b);install_watchdog.feed()\n return n,binascii.hexlify(h.digest()).decode()\ndef install_snapshot(root):\n result={}\n try: names=os.listdir(root)\n except OSError as e:\n  if e.args[0]==2: return result\n  raise\n for name in names:\n  p=root+'/'+name\n  if os.stat(p)[0]&0xf000==0x4000: result.update(install_snapshot(p))\n  else: result[p]=install_hash(p)\n return result\ninstall_before=install_snapshot('/system/apps/dots_boxes');install_old_state=install_snapshot('/state/dots_boxes');install_native_state=install_snapshot('/state/dots_boxes_native'); print('BASELINE_SAVED',len(install_before),len(install_old_state),len(install_native_state))"), end="", flush=True)
            mounted = False
            verified = []
            try:
                print(run("os.chdir('/');vfs.umount('/system');vfs.mount(install_writable,'/system',readonly=False);install_watchdog.feed();print('SYSTEM_WRITABLE')"), end="", flush=True)
                mounted = True
                parents = {TARGET}
                for item in manifest["files"]:
                    parts = item["path"].split("/")[:-1]
                    for i in range(1, len(parts)+1):
                        parents.add(TARGET+"/"+"/".join(parts[:i]))
                for path in sorted(parents, key=lambda p: (p.count("/"), p)):
                    run("try:\n os.mkdir(%r)\nexcept OSError as e:\n if e.args[0]!=17: raise\ninstall_watchdog.feed()" % path)
                for item in sorted(manifest["files"], key=lambda i: (i["path"] == "__init__.py", i["path"])):
                    destination = TARGET+"/"+item["path"]
                    data = payloads[item["path"]]
                    same, err = device.exec_raw("try:\n print(int(install_hash(%r)==(%d,%r)));install_watchdog.feed()\nexcept OSError as e:\n if e.args[0]!=2:raise\n print(0)" % (destination,len(data),item["sha256"]), timeout=60)
                    if err:
                        raise RuntimeError(err.decode(errors="replace"))
                    if same.strip() == b"1":
                        verified.append(item["path"])
                        print("VERIFIED_UNCHANGED",item["path"],len(data),flush=True)
                        continue
                    run("install_file=open(%r,'wb');install_watchdog.feed()" % (destination+".installing"))
                    for offset in range(0, len(data), 4096):
                        chunk = data[offset:offset+4096]
                        encoded = base64.b64encode(chunk).decode()
                        run("assert install_file.write(binascii.a2b_base64(%r))==%d;install_watchdog.feed()" % (encoded,len(chunk)))
                    run("install_file.flush();install_file.close();os.sync();assert install_hash(%r)==(%d,%r);os.rename(%r,%r);install_watchdog.feed()" % (destination+".installing",len(data),item["sha256"],destination+".installing",destination))
                    verified.append(item["path"])
                    print("VERIFIED",item["path"],len(data),flush=True)
                print(run("os.sync();assert install_snapshot('/system/apps/dots_boxes')==install_before;assert install_snapshot('/state/dots_boxes')==install_old_state;assert install_snapshot('/state/dots_boxes_native')==install_native_state;print('ORIGINAL_APP_AND_STATE_UNCHANGED');print('NATIVE_SAVE_AND_PENDING_MOVE_UNCHANGED');install_watchdog.feed()"), end="", flush=True)
            finally:
                if mounted:
                    print(run("os.chdir('/');os.sync();vfs.umount('/system');vfs.mount(install_mount,'/system',readonly=True);install_watchdog.feed();print('SYSTEM_READONLY_RESTORED')"), end="", flush=True)
            report = {"app":TARGET,"native_sha256":manifest["native"]["sha256"],"verified_files":verified,"original_app_and_state_unchanged":True,"native_save_and_pending_move_unchanged":True,"system_readonly_restored":True,"firmware_replaced":False}
            (HERE/"install-result.json").write_text(json.dumps(report,indent=2)+"\n")
            print("INSTALL_COMPLETE",TARGET,flush=True)
            return
        if args.mode == "smoke":
            print(run("sys.path.insert(0,%r);sys.path.insert(0,%r);import powersync.runtime as install_runtime;install_native=install_runtime.load_native(install_watchdog.feed);print('NATIVE_IMPORT_OK');gc.collect();print('NATIVE_GC_OK',gc.mem_free())" % (TARGET,TARGET+"/powersync")),end="",flush=True)
            # In-memory lifecycle only. No pairing/session or real game state
            # is created or modified, and no network request is performed.
            run("import time\ndef install_io(op,target,a,b):\n if op==9:return 0,os.urandom(a)\n if op==10:\n  now=int(time.time());return 0,(now//86400,now%86400)\n return 10,None\ninstall_native.configure(install_io);print('IO_CONFIGURED')")
            run("install_watchdog.feed();print('CORE_OPEN_BEGIN');install_native.open(':memory:');print('CORE_OPEN_OK');assert install_native.exec('SELECT powersync_rs_version()')==[['0.5.4/cratesio']];print('CORE_IDENTITY_OK')")
            run("install_watchdog.feed();print('SDK_START_BEGIN');install_native.sdk_start('{\"tables\":[]}', 'https://fixture.invalid', '550e8400-e29b-41d4-a716-446655440000');print('SDK_START_RETURNED')")
            run("install_watchdog.feed();print('SDK_POLL_BEGIN');install_s=json.loads(install_native.sdk_poll(0,6));print('SDK_POLL_RETURNED');assert install_s['credentials_needed'];print('SDK_EMBASSY_START_OK');gc.collect();install_watchdog.feed();print('SDK_ACTIVE_GC_OK')")
            run("install_native.sdk_reply('{\"op\":\"credentials\",\"token\":\"fixture-only\"}');install_watchdog.feed();install_s=json.loads(install_native.sdk_poll(16,6));print('SDK_REQUESTS',len(install_s['requests']));assert install_s['requests'];print('SDK_SQL_GUARD_OK',install_native.exec('SELECT count(*) FROM ps_crud'));install_done=False\nfor install_i in range(32):\n install_watchdog.feed()\n if install_native.sdk_stop():install_done=True;break\nassert install_done;install_native.close();gc.collect();print('SDK_STOP_CLOSE_GC_OK',gc.mem_free())")
            return
    finally:
        try:
            device.exit_raw_repl()
        except OSError:
            pass
        try:
            device.close()
        except OSError:
            pass


if __name__ == "__main__":
    main()
