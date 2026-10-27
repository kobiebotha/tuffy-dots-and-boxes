"""Read-only backend verification for completed latency qualification games."""
import argparse
import json
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    config = {'__file__': str(Path(__file__).resolve().parents[1] / 'app/dots_boxes/dots_config.py')}
    exec(Path(config['__file__']).read_text(), config)
    results = []
    for capture in sorted(args.directory.glob('run*-capture.json')):
        saved = json.loads(capture.read_text())
        if saved['capture']['status'] != 'complete':
            raise ValueError('Only completed qualification games may be verified')
        label = capture.name.removesuffix('-capture.json')
        private = json.loads((args.directory / '.private' / (label + '-sessions.json')).read_text())['physical']
        def get(path):
            lines = ['url = ' + json.dumps(config['SUPABASE_URL'] + path),
                     'header = ' + json.dumps('apikey: ' + config['SUPABASE_ANON_KEY']),
                     'header = ' + json.dumps('Authorization: Bearer ' + private['access_token'])]
            response = subprocess.run(['curl', '--silent', '--show-error', '--fail-with-body',
                '--connect-timeout', '10', '--max-time', '25', '--config', '-'],
                input='\n'.join(lines) + '\n', capture_output=True, text=True)
            if response.returncode:
                raise RuntimeError('Verification HTTPS failed with curl code ' + str(response.returncode))
            return json.loads(response.stdout)
        room_id = private['room_id']
        rooms = get('/rest/v1/rooms?id=eq.' + room_id + '&select=status,score_p1,score_p2,move_count,winner')
        moves = get('/rest/v1/moves?room_id=eq.' + room_id + '&select=move_index&order=move_index.asc')
        if (len(rooms) != 1 or rooms[0]['status'] != 'finished' or rooms[0]['move_count'] != 49
                or rooms[0]['score_p1'] != 10 or rooms[0]['score_p2'] != 10
                or [row['move_index'] for row in moves] != list(range(1, 50))):
            raise ValueError('Canonical game did not match the complete expected board')
        metadata = json.loads((args.directory / (label + '-metadata.json')).read_text())
        if metadata['published_moves'] != '49' or metadata['pending'] or metadata['sql_crud'] != '0':
            raise ValueError('Device game was not fully persisted and resolved')
        results.append({'run': label, 'canonical': rooms[0], 'contiguous_moves': len(moves),
                        'device_moves': int(metadata['published_moves']),
                        'pending': metadata['pending'], 'sql_crud': int(metadata['sql_crud'])})
    if len(results) != 3:
        raise ValueError('Expected three complete qualification games')
    result = {'verified': True, 'games': results}
    (args.directory / 'verification.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
