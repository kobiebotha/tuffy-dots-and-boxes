"""Platform contracts around SDK-owned CRUD upload and native sync mailboxes."""
import base64
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1] / 'apps/dots_boxes_native'
sys.path.insert(0, str(APP))
from powersync.sync import SyncClient, SDKHTTPS
from powersync.http import PENDING, Response, Gateway
from dots_sync import GameConnector
from dots_storage import Store as GameStore

ROOM = '550e8400-e29b-41d4-a716-446655440000'
MOVE = '550e8400-e29b-41d4-a716-446655440001'
ENDPOINT = 'https://fixture.invalid'


def move_value(identity=MOVE):
    return {'id': identity, 'room_id': ROOM, 'orientation': 'h', 'row': 0, 'col': 0}


class Records:
    def __init__(self):
        self.record = {'version': 1, 'device_id': '550e8400-e29b-41d4-a716-446655440002',
                       'user_id': '550e8400-e29b-41d4-a716-446655440003', 'seat': 1,
                       'room_id': ROOM,
                       'refresh_token': 'fixture-refresh', 'access_token': 'fixture-token',
                       'expires_at': int(time.time()) + 3600}
        self.saved = []

    def save(self, record):
        self.record = record
        self.saved.append(record)


class Native:
    def __init__(self):
        self.state = {'connected': True, 'synced': True, 'generation': 0,
                      'full_generation': 0, 'requests': [], 'close': [], 'can_feed': [],
                      'credentials_needed': False, 'upload': None, 'upload_close': [], 'changed_watches': []}
        self.replies = []
        self.stopped = 0

    def sdk_start(self, schema, endpoint, room):
        self.started = (schema, endpoint, room)

    def sdk_poll(self, elapsed, budget):
        result = dict(self.state)
        self.state['requests'] = []
        self.state['close'] = []
        self.state['upload_close'] = []
        self.state['changed_watches'] = []
        return json.dumps(result)

    def sdk_reply(self, value):
        self.replies.append(json.loads(value))

    def sdk_stop(self):
        self.stopped += 1
        return True


class Store:
    schema = '{"tables":[]}'
    validate_move = staticmethod(GameStore.validate_move)

    def __init__(self):
        self.native = Native()
        self.published = set()
        self.commands = []
        self.watch_notifications = []

    def retire_subscriptions(self):
        self.retired = True

    def control(self, name, value):
        self.commands.append((name, value))

    def has_move(self, move, room):
        return (move, room) in self.published

    def notify_watches(self, identities):
        self.watch_notifications.extend(identities)

    def execute(self, sql):
        raise AssertionError('Upload callbacks must not write application SQL')


class Remote:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class HttpTask(Remote):
    status = 200
    class Response:
        content_type = 'application/x-ndjson'
    response = Response()

    def __init__(self, request, roots, heartbeat=None):
        super().__init__()
        self.identity = request['id']
        self.steps = 0
        self.results = [PENDING]

    def step(self):
        self.steps += 1
        return self.results.pop(0) if self.results else PENDING


class GatewayTask(Remote):
    def __init__(self, results):
        super().__init__()
        self.results = list(results)
        self.body = None

    def start(self, body):
        self.body = dict(body)

    def step(self):
        result = self.results.pop(0)
        if callable(result):
            result = result(self)
        if isinstance(result, Exception):
            raise result
        return result


class NativeAdapterTests(unittest.TestCase):
    def create(self):
        store, records = Store(), Records()
        connector = GameConnector(records, ENDPOINT + '/auth', ENDPOINT + '/move',
                                  'x' * 24, (), store=store)
        sync = SyncClient(store, connector, ENDPOINT,
                          {'name': 'badge_game_v2', 'params': {'room_id': ROOM}}, (),
                          http_factory=HttpTask)
        return store, records, connector, sync

    def request_upload(self, store, identity=1, move=None):
        store.native.state['upload'] = {'id': identity, 'entry_id': identity, 'move': move or move_value()}
        store.native.state['synced'] = False

    def gateways(self, connector, scripts):
        created = []
        def factory(url, roots, **kwargs):
            task = GatewayTask(scripts[len(created)])
            task.url = url
            task.token = kwargs.get('token')
            created.append(task)
            return task
        connector.json_factory = factory
        return created

    def upload_replies(self, store):
        return [v for v in store.native.replies if v['op'] == 'upload']

    def test_requires_real_sdk_methods_and_preserves_sdk_checkpoint_target(self):
        store, records, connector, sync = self.create()
        self.assertTrue(store.retired)
        self.assertEqual(store.commands, [])
        self.assertEqual(store.native.started, (store.schema, ENDPOINT, ROOM))
        store.native = object()
        with self.assertRaisesRegex(RuntimeError, 'full native'):
            SyncClient(store, connector, ENDPOINT, sync.subscription, ())

    def test_rpc_starts_only_for_an_sdk_upload_request(self):
        store, records, connector, sync = self.create()
        created = self.gateways(connector, [])
        for _ in range(4):
            sync.step(10000)
        self.assertEqual(created, [])
        self.assertEqual(records.saved, [])
        self.assertFalse(hasattr(sync, '_connector_retry_ms'))

    def test_accepted_sdk_mutation_needs_no_application_pending_or_receipts(self):
        store, records, connector, sync = self.create()
        self.assertNotIn('pending_move', records.record)
        self.request_upload(store)
        created = self.gateways(connector, [[{}]])
        sync.step(16)
        self.assertEqual(created[0].body, {'move_id': MOVE, 'target': ROOM,
            'orientation': 'h', 'row_i': 0, 'col_i': 0})
        self.assertEqual(self.upload_replies(store), [{'op': 'upload', 'id': 1, 'result': 'success'}])
        self.assertEqual(records.saved, [])
        self.assertEqual(store.commands, [])
        self.assertEqual(store.published, set())
        self.assertFalse(sync.synced)
        self.assertFalse(hasattr(store, 'upload_receipt'))

    def test_uncertain_timeout_is_returned_to_sdk_and_only_sdk_retries_same_uuid(self):
        for server_committed in (False, True):
            with self.subTest(server_committed=server_committed):
                store, records, connector, sync = self.create()
                self.request_upload(store)
                server_moves = {}
                def uncertain(task):
                    if server_committed:
                        server_moves[task.body['move_id']] = dict(task.body)
                    raise OSError('HTTPS deadline')
                def accept(task):
                    before = server_moves.setdefault(task.body['move_id'], dict(task.body))
                    self.assertEqual(before, task.body)
                    return {}
                created = self.gateways(connector, [[PENDING, uncertain], [accept]])
                sync.step(16)
                self.assertTrue(sync._uploading)
                sync.step(16)
                self.assertTrue(created[0].closed)
                self.assertIsNone(connector.remote)
                self.assertEqual(self.upload_replies(store), [{'op': 'upload', 'id': 1, 'result': 'retry'}])
                self.assertEqual(store.native.stopped, 0)
                self.assertEqual(store.native.state['upload']['move']['id'], MOVE)
                for _ in range(3):
                    sync.step(60000)  # The platform cannot create the SDK's next callback.
                self.assertEqual(len(created), 1)
                store.native.state['upload_close'] = [1]
                self.request_upload(store, 2)
                sync.step(16)
                self.assertEqual(created[0].body, created[1].body)
                self.assertEqual(created[1].body['move_id'], MOVE)
                self.assertEqual(list(server_moves), [MOVE])
                self.assertEqual(self.upload_replies(store)[-1], {'op': 'upload', 'id': 2, 'result': 'success'})
                self.assertEqual(records.saved, [])
                self.assertFalse(sync.synced)
                store.native.state.update(upload=None, upload_close=[2], synced=True,
                                          generation=1, full_generation=1)
                sync.step(16)
                self.assertTrue(sync.synced)

    def test_exact_canonical_membership_skips_idempotent_move_rpc(self):
        store, records, connector, sync = self.create()
        store.published.add((MOVE, ROOM))
        self.request_upload(store)
        created = self.gateways(connector, [])
        sync.step(16)
        self.assertEqual(created, [])
        self.assertEqual(self.upload_replies(store), [{'op': 'upload', 'id': 1, 'result': 'success'}])
        self.assertEqual(records.saved, [])

    def test_canonical_uuid_in_another_room_does_not_skip_rpc(self):
        store, records, connector, sync = self.create()
        store.published.add((MOVE, '550e8400-e29b-41d4-a716-446655440004'))
        self.request_upload(store)
        created = self.gateways(connector, [[PENDING]])
        sync.step(16)
        self.assertEqual(len(created), 1)
        self.assertTrue(sync._uploading)
        self.assertEqual(self.upload_replies(store), [])

    def test_exact_canonical_publication_completes_inflight_sdk_upload(self):
        store, records, connector, sync = self.create()
        self.request_upload(store)
        created = self.gateways(connector, [[PENDING]])
        sync.step(16)
        store.published.add((MOVE, ROOM))
        store.native.state.update(generation=1, full_generation=1)
        sync.step(16)
        self.assertTrue(created[0].closed)
        self.assertEqual(self.upload_replies(store), [{'op': 'upload', 'id': 1, 'result': 'success'}])
        self.assertEqual(records.saved, [])

    def test_unrelated_full_publication_does_not_ack_inflight_sdk_upload(self):
        store, records, connector, sync = self.create()
        self.request_upload(store)
        created = self.gateways(connector, [[PENDING, PENDING]])
        sync.step(16)
        store.published.add(('550e8400-e29b-41d4-a716-446655440004', ROOM))
        store.native.state.update(generation=1, full_generation=1)
        sync.step(16)
        self.assertFalse(created[0].closed)
        self.assertTrue(sync._uploading)
        self.assertEqual(self.upload_replies(store), [])
        self.assertEqual(records.saved, [])

    def test_mutation_from_another_credential_room_is_never_uploaded(self):
        store, records, connector, sync = self.create()
        self.request_upload(store, move=dict(move_value(), room_id='550e8400-e29b-41d4-a716-446655440004'))
        created = self.gateways(connector, [])
        sync.step(16)
        self.assertEqual(created, [])
        self.assertEqual(self.upload_replies(store), [])
        self.assertEqual(sync.phase, 'blocked')
        self.assertEqual(records.saved, [])

    def test_rejection_waits_for_sdk_covering_full_without_python_checkpoint(self):
        store, records, connector, sync = self.create()
        store.native.state.update(generation=1, full_generation=1)
        sync._poll(0)
        self.assertTrue(sync.synced)
        self.request_upload(store)
        self.gateways(connector, [[{'http_status': 409, 'message': 'Not your turn'}]])
        sync.step(16)
        self.assertEqual(self.upload_replies(store), [{'op': 'upload', 'id': 1, 'result': 'rejected'}])
        self.assertEqual(connector.notice, 'Not your turn')
        self.assertTrue(sync.needs_write_barrier)
        self.assertFalse(sync.synced)
        self.assertEqual(records.saved, [])
        self.assertFalse(any(v['op'] == 'checkpoint' for v in store.native.replies))
        store.native.state.update(upload=None, upload_close=[1], synced=True,
                                  generation=2, full_generation=1)
        sync.step(60000)
        self.assertTrue(sync.needs_write_barrier)
        self.assertFalse(sync.synced)
        store.native.state.update(generation=3, full_generation=2)
        sync.step(16)
        self.assertFalse(sync.needs_write_barrier)
        self.assertTrue(sync.synced)

    def test_credentials_failure_is_returned_to_sdk_without_python_backoff(self):
        store, records, connector, sync = self.create()
        records.record.update(access_token=None, expires_at=0)
        store.native.state['credentials_needed'] = True
        self.request_upload(store)
        created = self.gateways(connector, [[OSError('Wi-Fi unavailable')]])
        sync.step(16)
        self.assertTrue(created[0].closed)
        self.assertIn({'op': 'credentials_error'}, store.native.replies)
        self.assertEqual(self.upload_replies(store)[-1]['result'], 'retry')
        self.assertEqual(store.native.stopped, 0)
        self.assertEqual(records.saved, [])

    def test_upload_401_returns_retry_then_sdk_callback_refreshes_credentials(self):
        store, records, connector, sync = self.create()
        self.request_upload(store)
        created = self.gateways(connector, [[{'http_status': 401}],
            [{'access_token': 'new-token', 'refresh_token': 'new-refresh', 'expires_in': 3600}], [{}]])
        sync.step(16)
        self.assertIsNone(records.record['access_token'])
        self.assertEqual(self.upload_replies(store)[-1]['result'], 'retry')
        store.native.state['upload_close'] = [1]
        self.request_upload(store, 2)
        sync.step(16)
        self.assertEqual([t.url for t in created], [ENDPOINT + '/move', ENDPOINT + '/auth', ENDPOINT + '/move'])
        self.assertEqual(created[0].body, created[2].body)
        self.assertEqual(created[2].token, 'new-token')

    def test_service_status_429_and_503_return_retry_without_completing(self):
        for status in (429, 503):
            with self.subTest(status=status):
                store, records, connector, sync = self.create()
                self.request_upload(store)
                self.gateways(connector, [[{'http_status': status}]])
                sync.step(16)
                self.assertEqual(self.upload_replies(store)[-1]['result'], 'retry')
                self.assertEqual(store.native.state['upload']['move']['id'], MOVE)
                self.assertEqual(records.saved, [])

    def test_auth_session_save_oserror_stays_blocked(self):
        store, records, connector, sync = self.create()
        records.record.update(access_token=None, expires_at=0)
        store.native.state['credentials_needed'] = True
        self.gateways(connector, [[{'access_token': 'new-token', 'refresh_token': 'new-refresh'}]])
        def disk_full(record):
            raise OSError('No space left')
        records.save = disk_full
        sync.step(16)
        self.assertEqual(sync.phase, 'blocked')
        self.assertEqual(store.native.stopped, 1)
        self.assertNotIn({'op': 'credentials_error'}, store.native.replies)

    def test_upload_canceled_by_sdk_closes_its_inflight_rpc(self):
        store, records, connector, sync = self.create()
        self.request_upload(store)
        created = self.gateways(connector, [[PENDING]])
        sync.step(16)
        store.native.state.update(upload=None, upload_close=[1])
        sync.step(16)
        self.assertTrue(created[0].closed)
        self.assertIsNone(sync.upload)
        self.assertEqual(records.saved, [])

    def test_same_poll_upload_creation_and_cancellation_never_starts_rpc(self):
        store, records, connector, sync = self.create()
        self.request_upload(store)
        store.native.state['upload_close'] = [1]
        created = self.gateways(connector, [])
        sync.step(16)
        self.assertEqual(created, [])
        self.assertEqual(self.upload_replies(store), [])

    def test_cached_synced_and_partial_progress_need_a_fresh_full_after_reconnect(self):
        store, records, connector, sync = self.create()
        sync._poll(0)
        self.assertFalse(sync.synced)
        store.native.state.update(generation=1, full_generation=0)
        sync._poll(0)
        self.assertFalse(sync.synced)
        store.native.state.update(generation=2, full_generation=1)
        sync._poll(0)
        self.assertTrue(sync.synced)
        store.native.state['connected'] = False
        sync._poll(0)
        self.assertFalse(sync.synced)
        store.native.state['connected'] = True
        sync._poll(0)
        self.assertFalse(sync.synced)
        store.native.state.update(generation=3, full_generation=1)
        sync._poll(0)
        self.assertFalse(sync.synced)
        store.native.state.update(generation=4, full_generation=2)
        sync._poll(0)
        self.assertTrue(sync.synced)
        self.assertEqual(records.saved, [])

    def test_sdk_watch_notifications_are_forwarded_without_application_writes(self):
        store, records, connector, sync = self.create()
        store.native.state['changed_watches'] = [2, 5]
        sync._poll(0)
        self.assertEqual(store.watch_notifications, [2, 5])
        sync._poll(0)
        self.assertEqual(store.watch_notifications, [2, 5])
        self.assertEqual(records.saved, [])

    def test_binary_fragment_uses_base64_and_honors_capacity(self):
        store, records, connector, sync = self.create()
        task = HttpTask({'id': 1}, ())
        task.results = [b'\xc3']
        sync.remote[1] = task
        sync._service_http()
        self.assertEqual(task.steps, 0)
        sync.can_feed = [1]
        sync._service_http()
        self.assertEqual(base64.b64decode(store.native.replies[1]['data_b64']), b'\xc3')

    def test_checkpoint_http_seeds_before_stream_and_cannot_be_starved(self):
        store, records, connector, sync = self.create()
        sync.remote = {1: HttpTask({'id': 1}, ()), 2: HttpTask({'id': 2}, ())}
        sync.request_urls = {1: ENDPOINT + '/sync/stream', 2: ENDPOINT + '/sync/checkpoint-request'}
        sync._await_seed.add(1)
        sync.remote[2].results = [PENDING, None]
        sync.can_feed = [1, 2]
        checkpoint = sync.remote[2]
        sync._service_http()
        self.assertEqual([sync.remote[1].steps, checkpoint.steps], [0, 1])
        sync._service_http()
        self.assertTrue(checkpoint.closed)
        sync._service_http()
        self.assertEqual(sync.remote[1].steps, 1)

    def test_later_checkpoint_preserves_stream_and_resumes_after_json(self):
        store, records, connector, sync = self.create()
        sync.remote = {1: HttpTask({'id': 1}, ())}
        sync.request_urls = {1: ENDPOINT + '/sync/stream'}
        sync.can_feed = [1]
        stream = sync.remote[1]
        sync._service_http()
        checkpoint = HttpTask({'id': 2}, ())
        checkpoint.results = [PENDING, b'{}', None]
        sync.remote[2] = checkpoint
        sync.request_urls[2] = ENDPOINT + '/sync/checkpoint-request'
        sync.can_feed = [1, 2]
        for _ in range(3):
            sync._service_http()
            self.assertEqual(stream.steps, 1)
            self.assertFalse(stream.closed)
        self.assertTrue(checkpoint.closed)
        self.assertIs(sync.remote[1], stream)
        self.assertNotIn({'op': 'error', 'id': 1}, store.native.replies)
        sync._service_http()
        self.assertEqual(stream.steps, 2)
        # HTTP completion cannot turn cached SDK status into a fresh full.
        sync._poll(0)
        self.assertFalse(sync.synced)
        self.assertEqual(records.saved, [])
        self.assertEqual(self.upload_replies(store), [])

    def test_later_checkpoint_releases_stream_in_single_tls_fallback(self):
        store, records, connector, sync = self.create()
        sync._single_tls = True
        sync.remote = {1: HttpTask({'id': 1}, ())}
        sync.request_urls = {1: ENDPOINT + '/sync/stream'}
        sync.can_feed = [1]
        stream = sync.remote[1]
        sync._service_http()
        checkpoint = HttpTask({'id': 2}, ())
        sync.remote[2] = checkpoint
        sync.request_urls[2] = ENDPOINT + '/sync/checkpoint-request'
        sync.can_feed = [1, 2]
        sync._service_http()
        self.assertTrue(stream.closed)
        self.assertIn({'op': 'error', 'id': 1}, store.native.replies)
        self.assertEqual(checkpoint.steps, 1)
        self.assertEqual(records.saved, [])
        self.assertEqual(self.upload_replies(store), [])

    def test_checkpoint_memory_pressure_releases_tls_without_application_state(self):
        store, records, connector, sync = self.create()
        stream = HttpTask({'id': 1}, ())
        sync.remote = {1: stream}
        sync.request_urls = {1: ENDPOINT + '/sync/stream'}
        sync.can_feed = [1]
        sync._service_http()
        checkpoint = HttpTask({'id': 2}, ())
        waiting = HttpTask({'id': 3}, ())
        sync.remote.update({2: checkpoint, 3: waiting})
        sync.request_urls.update({2: ENDPOINT + '/sync/checkpoint-request',
                                  3: ENDPOINT + '/sync/stream'})
        sync._await_seed.add(3)
        sync.can_feed = [1, 2, 3]
        with patch.object(checkpoint, 'step', side_effect=MemoryError('TLS arena full')):
            sync._service_http()
        self.assertTrue(sync._single_tls)
        self.assertTrue(checkpoint.closed)
        self.assertTrue(stream.closed)
        self.assertFalse(waiting.closed)
        self.assertEqual(waiting.steps, 0)
        self.assertEqual({reply['id'] for reply in store.native.replies if reply['op'] == 'error'}, {1, 2})
        self.assertEqual(records.saved, [])
        self.assertEqual(records.saved, [])
        self.assertEqual(self.upload_replies(store), [])
        self.assertEqual(store.native.stopped, 0)

        # A new SDK-authored checkpoint can still seed its unopened stream;
        # the platform adapter does not create its own retry or stop the SDK.
        store.native.state.update(requests=[{'id': 4, 'url': ENDPOINT + '/sync/checkpoint-request'}],
                                  can_feed=[3, 4])
        sync._poll(0)
        retry = sync.remote[4]
        retry.results = [None]
        sync._service_http()
        self.assertTrue(retry.closed)
        sync._service_http()
        self.assertEqual(waiting.steps, 1)
        self.assertEqual(records.saved, [])

    def test_request_canceled_in_same_poll_does_not_open_orphan(self):
        store, records, connector, sync = self.create()
        store.native.state.update(close=[1], requests=[{'id': 1, 'url': ENDPOINT + '/sync/stream'}])
        sync._poll(0)
        self.assertEqual(sync.remote, {})

    def test_failed_socket_teardown_cannot_block_native_reconnection(self):
        store, records, connector, sync = self.create()
        class Broken(Remote):
            def close(self):
                raise OSError('socket already gone')
        sync.remote[1] = Broken()
        sync.request_urls[1] = ENDPOINT + '/sync/stream'
        sync.headers_sent.add(1)
        sync._await_seed.add(1)
        sync._active_http = 1
        store.native.state['close'] = [1]
        sync.step(16)
        self.assertEqual(sync.remote, {})
        self.assertEqual(sync.request_urls, {})
        self.assertEqual(sync.headers_sent, set())
        self.assertEqual(sync._await_seed, set())
        self.assertIsNone(sync._active_http)
        self.assertNotEqual(sync.phase, 'blocked')

    def test_memory_pressure_returns_sdk_upload_retry_and_selects_single_tls(self):
        store, records, connector, sync = self.create()
        self.request_upload(store)
        self.gateways(connector, [[MemoryError('second TLS')]])
        sync.remote[1] = stream = HttpTask({'id': 1}, ())
        sync.request_urls[1] = ENDPOINT + '/sync/stream'
        sync.step(16)
        self.assertTrue(sync._single_tls)
        self.assertTrue(stream.closed)
        self.assertEqual(self.upload_replies(store)[-1]['result'], 'retry')
        self.assertEqual(store.native.state['upload']['move']['id'], MOVE)
        self.assertNotEqual(sync.phase, 'blocked')

    def test_close_cancels_native_and_python_transports(self):
        store, records, connector, sync = self.create()
        sync.remote[1] = task = HttpTask({'id': 1}, ())
        connector.remote = rpc = Remote()
        sync.close()
        self.assertTrue(task.closed and rpc.closed)
        self.assertEqual(store.native.stopped, 1)

    def test_sdk_request_preserves_token_auth_and_forces_ndjson(self):
        request = {'method': 'POST', 'url': ENDPOINT + '/sync/stream', 'body': '{"x":1}',
                   'headers': [['Authorization', 'Token fixture'], ['Accept', 'application/vnd.powersync.bson-stream']]}
        with patch.object(time, 'ticks_ms', lambda: 0, create=True):
            task = SDKHTTPS(request, ())
        self.assertIn(b'Authorization: Token fixture\r\n', task.request)
        self.assertIn(b'Accept: application/x-ndjson\r\n', task.request)
        self.assertNotIn(b'bson-stream', task.request)
        request['headers'] = [['Authorization', 'Token fixture\r\nInjected: yes']]
        with patch.object(time, 'ticks_ms', lambda: 0, create=True):
            with self.assertRaises(ValueError):
                SDKHTTPS(request, ())

    def test_gateway_classifies_non_json_retryable_status_before_decoding(self):
        for status, body in ((429, b'<html>rate limit</html>'), (503, b''), (502, b'not json'), (503, b'x' * 20000)):
            with self.subTest(status=status):
                class HTTP(Remote):
                    def __init__(self, *args, **kwargs):
                        super().__init__()
                        self.status = status
                        self.results = [body, None]
                    def step(self):
                        return self.results.pop(0)
                gateway = Gateway(ENDPOINT, (), factory=HTTP)
                gateway.start({})
                self.assertEqual(gateway.step()['http_status'], status)
                self.assertIsNone(gateway.task)
                self.assertEqual(gateway.buffer, b'')

    def test_response_exposes_content_type_without_relaxing_framing(self):
        response = Response()
        self.assertEqual(response.feed(b'HTTP/1.1 200 OK\r\nContent-Type: application/x-ndjson\r\nContent-Length: 2\r\n\r\n{}'), b'{}')
        self.assertEqual(response.content_type, 'application/x-ndjson')
        response.finish()


if __name__ == '__main__':
    unittest.main()
