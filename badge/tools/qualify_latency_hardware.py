"""Run three real games on an explicitly selected Tufty and save each sample.

Requires mpremote (or its cached installation). Uses normal app files unchanged,
dedicated anonymous test accounts/rooms, and the existing authenticated RPC.
Public captures contain no sessions or UUIDs. Private recovery files are ignored.
"""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parents[1]
sys.path.insert(0, str(TOOLS))
from benchmark_latency import RawDevice
from latency_host import arm_download, clock_probe, clock_us
from prepare_latency import build_device_source

KEEPER = '''import machine, time, sys, select, json
if '/system/lib' not in sys.path: sys.path.insert(0,'/system/lib')
hw_watchdog=machine.WDT(timeout=16000)
hw_poll=select.poll()
hw_poll.register(sys.stdin,select.POLLIN)
print('HW_KEEPER_READY')
while True:
 hw_watchdog.feed()
 if hw_poll.poll(20):
  hw_line=sys.stdin.readline()
  if not hw_line: continue
  try:
   hw_msg=json.loads(hw_line)
   exec(hw_msg['source'],globals())
  except BaseException as hw_error:
   print('HW_KEEPER_ERROR',type(hw_error).__name__,str(hw_error))
  hw_watchdog.feed()
  print('HW_KEEPER_DONE',hw_msg.get('id',0))
'''


def save_json(path, value, private=False):
    temporary = path.with_name(path.name + '.next')
    with open(temporary, 'w') as output:
        json.dump(value, output, indent=2, allow_nan=False)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())
    temporary.chmod(0o600 if private else 0o644)
    temporary.replace(path)


class Qualification:
    def __init__(self, port, output):
        self.output = output
        self.private = output / '.private'
        output.mkdir(parents=True, exist_ok=True)
        self.private.mkdir(mode=0o700, exist_ok=True)
        (output / '.gitignore').write_text('.private/\n*.next\n')
        try:
            from mpremote.transport_serial import SerialTransport
        except ImportError:
            cache = Path.home() / '.cache/uv/archive-v0'
            for pattern in ('*/mpremote/__init__.py', '*/serial/__init__.py'):
                sys.path[:0] = [str(path.parent.parent) for path in cache.glob(pattern)]
            from mpremote.transport_serial import SerialTransport
        self.transport = SerialTransport(port, timeout=2)
        # Start with a fresh Python heap. Old interrupted development sessions
        # can retain native/VFS buffers even after their database was closed.
        self.transport.enter_raw_repl(soft_reset=True)
        self.transport.exec_raw_no_follow(KEEPER)
        ready = self.transport.read_until(1, b'HW_KEEPER_READY\r\n', timeout=8, timeout_overall=8)
        if not ready.endswith(b'HW_KEEPER_READY\r\n'):
            raise RuntimeError('Device caretaker did not start')
        self.sequence = 0
        spec = importlib.util.spec_from_file_location('badge_package', TOOLS / 'package.py')
        package = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(package)
        self.sources = package.collect()
        self.config = {'__file__': '/system/apps/dots_boxes/dots_config.py'}
        exec(self.sources['dots_config.py'], self.config)

    def send(self, source):
        self.sequence += 1
        self.transport.serial.write((json.dumps({'id': self.sequence, 'source': source}) + '\n').encode())
        return self.sequence

    def finish(self, identity, buffered=b'', timeout=30):
        ending = ('HW_KEEPER_DONE ' + str(identity) + '\r\n').encode()
        data = buffered
        if not data.endswith(ending):
            data += self.transport.read_until(1, ending, timeout=timeout, timeout_overall=timeout)
        if not data.endswith(ending):
            raise TimeoutError('Device caretaker response timed out')
        text = data[:-len(ending)].decode(errors='replace').replace('\r\n', '\n')
        if 'HW_KEEPER_ERROR' in text:
            raise RuntimeError(text.strip())
        return text

    def raw(self, source, timeout=30):
        return self.finish(self.send(source), timeout=timeout)

    def api(self, path, body, token=None):
        lines = ['url = ' + json.dumps(self.config['SUPABASE_URL'] + path), 'request = "POST"',
                 'header = ' + json.dumps('apikey: ' + self.config['SUPABASE_ANON_KEY']),
                 'header = "Content-Type: application/json"', 'data = ' + json.dumps(json.dumps(body))]
        if token:
            lines.append('header = ' + json.dumps('Authorization: Bearer ' + token))
        sent = clock_us()
        result = subprocess.run(['curl', '--silent', '--show-error', '--fail-with-body',
                                 '--connect-timeout', '10', '--max-time', '25', '--config', '-'],
                                input='\n'.join(lines) + '\n', capture_output=True, text=True)
        received = clock_us()
        if result.returncode:
            raise RuntimeError('Benchmark API curl code ' + str(result.returncode))
        return json.loads(result.stdout), sent, received

    def inspect(self):
        installed = json.loads(self.raw("import os, sys, json, gc, machine, time, hashlib, binascii\n"
            "checks={}\nfor name in " + repr(list(self.sources)) + ":\n"
            " h=hashlib.sha256(); length=0\n"
            " with open('/system/apps/dots_boxes/'+name,'rb') as f:\n"
            "  while True:\n   hw_watchdog.feed(); block=f.read(4096)\n"
            "   if not block: break\n   h.update(block); length+=len(block)\n"
            " checks[name]={'bytes':length,'sha256':binascii.hexlify(h.digest()).decode()}\n"
            "print(json.dumps(checks))"))
        expected = {name: {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                    for name, data in self.sources.items()}
        if installed != expected:
            raise RuntimeError('Installed app files differ from source; no benchmark started')
        self.installed = installed
        # Back up existing app state before pairing it to dedicated test rooms.
        backup = json.loads(self.raw("backup={}\ntry:\n names=os.listdir('/state/dots_boxes')\n"
            "except OSError: names=[]\nfor name in names:\n"
            " with open('/state/dots_boxes/'+name,'rb') as f:\n"
            "  backup[name]=binascii.b2a_base64(f.read()).decode()\n"
            " hw_watchdog.feed()\nprint(json.dumps(backup))"))
        backup_path = self.private / 'before-state'
        backup_path.mkdir(mode=0o700, exist_ok=False)
        for name, encoded in backup.items():
            path = backup_path / name
            path.write_bytes(base64.b64decode(encoded))
            path.chmod(0o600)
        identity = json.loads(self.raw("import gc\ngc.collect()\nsys.path.insert(0,'/system/apps/dots_boxes')\n"
            "from dots_storage import Store\nfrom dots_session import SessionFile\n"
            "store=Store(heartbeat=hw_watchdog.feed).open()\n"
            "record=SessionFile(store.root).load(store.new_uuid)\n"
            "assert record['pending_move'] is None\n"
            "print(json.dumps({'device_id':record['device_id']}))\nstore.close()"))
        self.device_id = identity['device_id']
        save_json(self.output / 'metadata-before.json', self.metadata())
        print('Verified all', len(installed), 'installed files; saved private recovery state.', flush=True)

    def metadata(self):
        source = """import os, sys, json, gc, machine, network
gc.collect()
sys.path.insert(0,'/system/apps/dots_boxes')
from dots_storage import Store
from dots_session import SessionFile
info={'uname':list(os.uname()),'implementation':str(sys.implementation),
      'cpu_hz':machine.freq(),'heap_free_bytes':gc.mem_free()}
store=Store(heartbeat=hw_watchdog.feed).open()
for key,sql in [('schema','PRAGMA user_version'),('cache_pages','PRAGMA cache_size'),
 ('synchronous','PRAGMA synchronous'),('journal_mode','PRAGMA journal_mode'),
 ('page_size','PRAGMA page_size'),('pages','PRAGMA page_count'),
 ('free_pages','PRAGMA freelist_count'),('published_moves','SELECT count(*) FROM moves'),
 ('sql_crud','SELECT count(*) FROM ps_crud')]: info[key]=store.scalar(sql)
info['database_bytes']=os.stat(store.root+'/state.sqlite')[6]
info['pending']=SessionFile(store.root).load(store.new_uuid)['pending_move'] is not None
try:
 wlan=network.WLAN(network.STA_IF)
 info['wifi_connected']=wlan.isconnected()
 info['wifi_rssi_dbm']=wlan.status('rssi')
except Exception: pass
store.close()
print(json.dumps(info))
"""
        return json.loads(self.raw(source))

    def setup(self, label, reuse=None):
        if reuse:
            private = json.loads(reuse.read_text())
            host, guest = private['host'], private['guest']
            self.opponent, self.physical = private['opponent'], private['physical']
        else:
            host = self.api('/auth/v1/signup', {})[0]
            guest = self.api('/auth/v1/signup', {})[0]
            ticket = self.api('/rest/v1/rpc/create_room', {}, host['access_token'])[0]
            guest_ticket = self.api('/rest/v1/rpc/join_room', {'target': ticket['room_id']}, guest['access_token'])[0]
            self.opponent = self.api('/functions/v1/badge-pair', {'code': guest_ticket['pairing_code'],
                'device_id': str(uuid.uuid4())}, guest['access_token'])[0]
            self.physical = self.api('/functions/v1/badge-pair', {'code': ticket['pairing_code'],
                'device_id': self.device_id}, host['access_token'])[0]
        save_json(self.private / (label + '-sessions.json'), {'host': host, 'guest': guest,
                  'physical': self.physical, 'opponent': self.opponent}, private=True)
        record = {'version': 1, 'device_id': self.device_id, 'user_id': self.physical['user_id'],
                  'room_id': self.physical['room_id'], 'seat': self.physical['seat'],
                  'access_token': self.physical['access_token'], 'refresh_token': self.physical['refresh_token'],
                  'expires_at': 0, 'pending_move': None}
        self.raw("import sys,time\nsys.path.insert(0,'/system/apps/dots_boxes')\n"
            "from dots_session import SessionFile\n"
            "assert SessionFile('/state/dots_boxes').load(lambda:None)['pending_move'] is None\n"
            "record=" + repr(record) + "\nrecord['expires_at']=int(time.time())+"
            + str(self.physical['expires_in']) + "\nSessionFile('/state/dots_boxes').save(record)")
        print(label, 'dedicated room paired', flush=True)

    def calibrate(self):
        source = "import gc,time,json\ngc.collect()\ncal_ns={'__name__':'latency_calibration'}\nexec(" + repr(
            (TOOLS / 'latency_probe.py').read_text()) + ",cal_ns)\n"
        source += """cal_probe=cal_ns['Probe'](1)
cal_iterations=2000
cal_start=time.ticks_us()
for cal_i in range(cal_iterations): pass
cal_loop=time.ticks_diff(time.ticks_us(),cal_start)
cal_start=time.ticks_us()
for cal_i in range(cal_iterations): cal_probe.now()
cal_now=time.ticks_diff(time.ticks_us(),cal_start)
sys.path.insert(0,'/system/apps/dots_boxes')
from dots_storage import Store
from dots_session import SessionFile
from dots_codec import decode_uuid
cal_store=Store(heartbeat=hw_watchdog.feed).open()
cal_record=SessionFile(cal_store.root).load(cal_store.new_uuid)
cal_id=decode_uuid(cal_store.scalar('SELECT id FROM moves LIMIT 1'))
cal_times=[]
for cal_i in range(110):
 hw_watchdog.feed()
 cal_start=time.ticks_us()
 assert cal_store.has_move(cal_id,cal_record['room_id'])
 cal_elapsed=time.ticks_diff(time.ticks_us(),cal_start)
 if cal_i>=10: cal_times.append(cal_elapsed)
cal_times.sort()
cal_store.close()
print(json.dumps({'probe_now':{'iterations':cal_iterations,'total_us':cal_now,
 'per_call_including_loop_us':cal_now/cal_iterations,'empty_loop_total_us':cal_loop},
 'exact_move_correlation_query':{'warmups':10,'iterations':100,'p50_us':cal_times[49],
 'p95_us':cal_times[94],'max_us':cal_times[-1]}}))
"""
        save_json(self.output / 'observer-calibration.json', json.loads(self.raw(source)))

    def match(self, label):
        capture = {'format_version': 1, 'device': {'format_version': 1, 'local': [], 'downloads': []},
                   'host': {'format_version': 1, 'sync_probes': [], 'download_rpc': []},
                   'capture': {'label': label, 'status': 'started', 'warmup_moves': 4,
                    'date': datetime.now().astimezone().date().isoformat(),
                    'started_utc': datetime.now(timezone.utc).isoformat(),
                    'move_order': 'horizontal row/column order, then vertical row/column order',
                    'host_rpc_transport': 'curl HTTPS; fresh process, no connection reuse',
                    'local_trigger': 'scheduled Controller.tap after normal step, before draw',
                    'serial_policy': 'no host traffic inside samples; sample_done after final submission',
                    'app_files_verified': self.installed, 'annotations': []}}
        path = self.output / (label + '-capture.json')
        save_json(path, capture)
        device = RawDevice(self.transport)
        command_id = 0
        def command(name, **args):
            nonlocal command_id
            command_id += 1
            return device.command({'command': name, 'request_id': command_id, **args})
        def sync_clock():
            nonlocal command_id
            command_id += 1
            capture['host']['sync_probes'].append(clock_probe(device.command, command_id))
        source = build_device_source(seconds=240, capacity=32, app_path='/system/apps/dots_boxes')
        keeper_id = self.send(source)
        stage = 'ready'
        try:
            ready = device.read_message(timeout=40)
            if ready.get('command') != 'ready' or ready.get('accepted') is not True:
                raise RuntimeError('Device not ready')
            stage = 'initial_sync'
            deadline = time.monotonic() + 90
            while True:
                status = command('status')
                capture['capture']['last_startup_status'] = status
                save_json(path, capture)
                if status.get('app_failure') or status.get('failure') or status.get('step_error'):
                    raise RuntimeError('App startup failed; diagnostic saved')
                if status.get('synced') and status.get('phase') == 'stream': break
                if time.monotonic() > deadline: raise TimeoutError('Initial sync timeout')
                time.sleep(.25)
            sync_clock()
            edges = [('h', r, c) for r in range(5) for c in range(5)]
            edges += [('v', r, c) for r in range(4) for c in range(6)]
            for index, edge in enumerate(edges, 1):
                stage = 'move_' + str(index)
                status = command('status')
                if status['moves'] != index - 1 or not status['synced']:
                    raise RuntimeError('Unexpected board state')
                if status['current_seat'] == self.physical['seat']:
                    acknowledgement = command('schedule_local', edge=list(edge), delay_frames=3)
                    sample_id, kind = acknowledgement['sample_id'], 'local'
                else:
                    kind, sample_id, move_id = 'download', status['download_count'], str(uuid.uuid4())
                    sync_clock()
                    command_id += 1
                    arm_download(device.command, sample_id, move_id, edge, index, command_id)
                    reply, sent, received = self.api('/rest/v1/rpc/submit_move', {
                        'move_id': move_id, 'target': self.opponent['room_id'],
                        'orientation': edge[0], 'row_i': edge[1], 'col_i': edge[2]}, self.opponent['access_token'])
                    rpc = {'sample_id': sample_id, 'host_send_us': sent, 'host_receive_us': received,
                           'accepted': reply.get('ok') is True, 'duplicate': reply.get('duplicate', False) is True}
                    capture['host']['download_rpc'].append(rpc)
                    if not rpc['accepted'] or rpc['duplicate']: raise RuntimeError('Rejected or duplicate RPC')
                done = device.read_message(timeout=35)
                if (done.get('command') != 'sample_done' or done.get('kind') != kind
                        or done.get('sample_id') != sample_id):
                    raise RuntimeError('Unexpected completion notification')
                capture['capture']['annotations'].append({'kind': kind, 'sample_id': sample_id,
                    'move_index': index, 'warmup': index <= 4})
                if kind == 'download': sync_clock()
                capture['device'] = command('export')['device']
                save_json(path, capture)
                print(label, index, kind, 'done', flush=True)
            capture['capture']['status'] = 'complete'
        except BaseException as error:
            capture['capture'].update(status='failed', error=type(error).__name__, error_stage=stage)
        finally:
            try:
                sent = clock_us()
                stopped = command('stop')
                received = clock_us()
                capture['host']['sync_probes'].append({'host_send_us': sent,
                    'device_us': stopped['device_us'], 'host_receive_us': received})
                capture['device'] = device.wait_finished()['device']
                self.finish(keeper_id, bytes(device.buffer), timeout=10)
            except BaseException as error:
                capture['capture'].update(status='failed', cleanup_error=type(error).__name__)
            save_json(path, capture)
        print(label, capture['capture']['status'], 'local', len(capture['device']['local']),
              'download', len(capture['device']['downloads']), flush=True)
        if capture['capture']['status'] != 'complete':
            raise RuntimeError('Qualification stopped; partial capture saved')
        save_json(self.output / (label + '-metadata.json'), self.metadata())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--games', type=int, default=3)
    parser.add_argument('--reuse-sessions', type=Path, help='Reuse private setup from a failed pre-move capture; one game only')
    parser.add_argument('--return-home', action='store_true', help='Reset to the normal launcher after all app/database work has closed')
    args = parser.parse_args()
    if args.return_home:
        cache = Path.home() / '.cache/uv/archive-v0'
        for pattern in ('*/mpremote/__init__.py', '*/serial/__init__.py'):
            sys.path[:0] = [str(path.parent.parent) for path in cache.glob(pattern)]
        from mpremote.transport_serial import SerialTransport
        transport = SerialTransport(args.port, timeout=2)
        try:
            transport.enter_raw_repl(soft_reset=False)
            transport.exec_raw_no_follow('import machine; machine.reset()')
        finally:
            transport.serial.close()
        print('Normal badge reset requested after benchmark cleanup.')
        return
    if args.output is None:
        parser.error('--output is required for a capture')
    if not 1 <= args.games <= 5:
        parser.error('--games must be 1..5')
    if args.reuse_sessions and args.games != 1:
        parser.error('--reuse-sessions requires --games=1')
    if args.output.exists():
        parser.error('Use a new output directory to preserve prior captures')
    qualification = Qualification(args.port, args.output)
    try:
        qualification.inspect()
        for index in range(1, args.games + 1):
            label = 'run' + str(index)
            qualification.setup(label, args.reuse_sessions)
            qualification.match(label)
        qualification.calibrate()
    finally:
        # Keep watchdog servicing after app/database close, until the user resets
        # or the next serial controller takes over. No extra Ctrl-C on cleanup.
        qualification.transport.close()


if __name__ == '__main__':
    main()
