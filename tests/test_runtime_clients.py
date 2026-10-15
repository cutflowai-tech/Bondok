"""HTTP clients of the Bondok runtime against a recording fake transport (no network): request shapes, pagination,
error classes, token handling. Acceptance A21 (classification by stage), A32 (unsupported operations)."""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401  (src/ on the path)

from waset_runtime import dropbox as D
from waset_runtime.dry import dry
from waset_runtime.http import Http, HttpError
from waset_runtime.instagram import Instagram
from waset_runtime.monday import Monday, MondayError


class Transport:
    def __init__(self, *answers):
        self.answers, self.requests = list(answers), []

    def __call__(self, method, url, headers, body, timeout):
        self.requests.append({'method': method, 'url': url, 'headers': headers,
                              'body': json.loads(body) if body and body[:1] in (b'{', b'[') else body})
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        status, payload = a
        return status, {}, payload if isinstance(payload, bytes) else json.dumps(payload).encode()


class MondayClient(unittest.TestCase):
    def test_pagination_and_no_partial_snapshot(self):
        t = Transport((200, {'data': {'boards': [{'items_page': {'cursor': 'c1', 'items': [{'id': '1'}]}}]}}),
                      (200, {'data': {'next_items_page': {'cursor': None, 'items': [{'id': '2'}]}}}))
        m = Monday('tok', Http(t), sleep=lambda s: None)
        self.assertEqual([i['id'] for i in m.social_snapshot()], ['1', '2'])
        self.assertEqual(t.requests[0]['headers']['Authorization'], 'tok')
        self.assertIn('items_page(limit:500)', t.requests[0]['body']['query'])
        t = Transport((200, {'data': {'boards': [{'items_page': {'cursor': 'c1', 'items': []}}]}}),
                      (200, {'data': {'next_items_page': None}}))
        with self.assertRaises(MondayError):
            Monday('tok', Http(t), sleep=lambda s: None).social_snapshot()

    def test_rate_limit_retried_then_reported(self):
        t = Transport((429, {'error': 'rate'}), (200, {'data': {'items': []}}))
        self.assertEqual(Monday('t', Http(t), sleep=lambda s: None).items(['1']), [])
        t = Transport((200, {'errors': [{'message': 'bad column'}]}))
        with self.assertRaises(MondayError) as e:
            Monday('t', Http(t), sleep=lambda s: None).items(['1'])
        self.assertTrue(e.exception.answered)

    def test_unanswered_create_is_not_retried(self):
        t = Transport(OSError('timeout'))
        with self.assertRaises(MondayError) as e:
            Monday('t', Http(t), sleep=lambda s: None).create_item('x')
        self.assertFalse(e.exception.answered)
        self.assertEqual(len(t.requests), 1)

    def test_fresh_item_refuses_deleted_or_archived_items(self):
        for state in ('deleted', 'archived'):
            t = Transport((200, {'data': {'items': [{'id': '1', 'state': state, 'board': {'id': '5105608159'},
                                                     'column_values': []}]}}))
            with self.assertRaises(MondayError):
                Monday('t', Http(t), sleep=lambda s: None).fresh_item('1')

    def test_fresh_item_refuses_other_boards(self):
        t = Transport((200, {'data': {'items': [{'id': '1', 'board': {'id': '999'}, 'column_values': []}]}}))
        with self.assertRaises(MondayError):
            Monday('t', Http(t), sleep=lambda s: None).fresh_item('1')

    def test_display_write_shape(self):
        t = Transport((200, {'data': {'change_multiple_column_values': {'id': '1'}, 'move_item_to_group': {'id': '1'}}}))
        Monday('t', Http(t), sleep=lambda s: None).change_columns('1', {'status': {'label': 'Scheduled'}}, group='g')
        q = t.requests[0]['body']
        self.assertIn('create_labels_if_missing:false', q['query'])
        self.assertIn('move_item_to_group', q['query'])
        self.assertEqual(json.loads(q['variables']['v']), {'status': {'label': 'Scheduled'}})


class DropboxClient(unittest.TestCase):
    def test_classification_matches_the_workflow(self):
        self.assertEqual(D.classify(409, 'path/not_found/..')[0], 'config')
        self.assertEqual(D.classify(429, 'too_many_requests/')[0], 'infra')
        self.assertEqual(D.classify(401, 'invalid_access_token/')[0], 'infra')     # credential: not the content
        self.assertEqual(D.classify(None, None, '[editor] No final video')[0], 'editor')
        self.assertEqual(D.classify(None, 'shared_link_not_found/', 'shared_link_not_found/')[0], 'config')
        self.assertEqual(D.classify(500, None, 'boom')[0], 'infra')

    def test_refresh_token_and_retry_on_401(self):
        t = Transport((200, {'access_token': 'A1', 'expires_in': 14400}),
                      (401, {'error_summary': 'expired_access_token/'}),
                      (200, {'access_token': 'A2', 'expires_in': 14400}),
                      (200, {'.tag': 'file', 'id': 'id:1', 'rev': 'r'}))
        d = D.Dropbox(refresh_token='R', app_key='k', app_secret='s', http=Http(t))
        self.assertEqual(d.metadata('/x')['rev'], 'r')
        self.assertEqual(t.requests[-1]['headers']['Authorization'], 'Bearer A2')

    def test_share_reuses_an_existing_link(self):
        t = Transport((409, {'error_summary': 'shared_link_already_exists/metadata/..'}),
                      (200, {'links': [{'url': 'https://www.dropbox.com/s/a/v.mp4?dl=0'}]}))
        d = D.Dropbox(token='T', http=Http(t))
        self.assertEqual(D.raw_link(d.share('/p/v.mp4')), 'https://www.dropbox.com/s/a/v.mp4?raw=1')

    def test_raw_link_round_trip(self):
        self.assertEqual(D.strip_raw('https://x/s/a.mp4?raw=1'), 'https://x/s/a.mp4')
        self.assertEqual(D.strip_raw('https://x/s/a.mp4?rlkey=k&raw=1'), 'https://x/s/a.mp4?rlkey=k')
        self.assertEqual(D.raw_link('https://x/s/a.mp4?rlkey=k&dl=0'), 'https://x/s/a.mp4?rlkey=k&raw=1')

    def test_large_upload_uses_a_session(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b'x' * 10)
        self.addCleanup(os.unlink, f.name)
        old = D.SINGLE_UPLOAD_MAX, D.SESSION_CHUNK
        D.SINGLE_UPLOAD_MAX, D.SESSION_CHUNK = 4, 4
        self.addCleanup(lambda: setattr(D, 'SINGLE_UPLOAD_MAX', old[0]) or setattr(D, 'SESSION_CHUNK', old[1]))
        t = Transport((200, {'session_id': 'S'}), (200, None), (200, {'id': 'id:U', 'rev': 'r', 'content_hash': 'h'}))
        r = D.Dropbox(token='T', http=Http(t)).upload(f.name, '/Social Media/Prepared/x.mp4')
        self.assertEqual(r['rev'], 'r')
        self.assertEqual([x['url'].rsplit('/', 2)[-2] + '/' + x['url'].rsplit('/', 1)[-1] for x in t.requests],
                         ['upload_session/start', 'upload_session/append_v2', 'upload_session/finish'])


class InstagramClient(unittest.TestCase):
    def ig(self, *answers):
        t = Transport(*answers)
        return Instagram('TOKEN', '178400', Http(t)), t

    def test_reel_payload_and_token_never_in_url(self):
        ig, t = self.ig((200, {'id': 'C1'}))
        ig.create_container({'media_type': 'REELS', 'video_url': 'https://v', 'caption': 'c', 'share_to_feed': True})
        r = t.requests[0]
        self.assertTrue(r['url'].endswith('/v26.0/178400/media'))
        self.assertEqual(r['body'], {'media_type': 'REELS', 'video_url': 'https://v', 'caption': 'c',
                                     'share_to_feed': True})
        self.assertNotIn('TOKEN', r['url'])
        self.assertEqual(r['headers']['Authorization'], 'Bearer TOKEN')

    def test_story_payload_has_only_type_and_url(self):
        ig, t = self.ig((200, {'id': 'C1'}))
        ig.create_container({'media_type': 'STORIES', 'video_url': 'https://v', 'caption': 'ignored'})
        self.assertEqual(t.requests[0]['body'], {'media_type': 'STORIES', 'video_url': 'https://v'})

    def test_publish_answers_are_returned_unanswered_raises(self):
        ig, _ = self.ig((400, {'error': {'code': 100, 'message': 'bad'}}))
        self.assertEqual(ig.publish('C1')[0], 400)
        ig, _ = self.ig(OSError('timed out'))
        with self.assertRaises(HttpError) as e:
            ig.publish('C1')
        self.assertFalse(e.exception.answered)

    def test_unconfigured_account_refused(self):
        with self.assertRaises(ValueError):
            Instagram('t', '<IG_ACCOUNT>', Http(Transport()))

    def test_unsupported_operations_are_named(self):
        r = Instagram.unsupported('delete a published post')
        self.assertEqual(r['state'], 'unsupported')
        self.assertIn('Nothing was changed', r['reason'])


class DryRun(unittest.TestCase):
    def test_writes_are_logged_not_sent(self):
        t = Transport((200, {'data': {'items': []}}))
        m = dry(Monday('t', Http(t), sleep=lambda s: None))
        m.items(['1'])
        m.change_columns('1', {'status': {'label': 'Posted'}})
        self.assertEqual(len(t.requests), 1)
        self.assertEqual(m.log[0][0], 'change_columns')

    def test_shadow_copy_never_opens_the_live_store_for_writing(self):
        from waset_runtime.service import shadow_copy
        d = Path(tempfile.mkdtemp())
        live = d / 'state.sqlite'
        c = sqlite3.connect(live)
        c.execute('CREATE TABLE t(x)')
        c.execute('INSERT INTO t VALUES(1)')
        c.commit()
        c.close()
        copy = shadow_copy(str(live))
        self.assertNotEqual(Path(copy).resolve(), live.resolve())
        self.assertEqual(sqlite3.connect(copy).execute('SELECT x FROM t').fetchone()[0], 1)


class InstagramTokenRefresh(unittest.TestCase):
    def test_refresh_after_thirty_days_is_persisted_privately(self):
        from waset_runtime.instagram import TokenStore
        d = Path(tempfile.mkdtemp())
        now = [1_000_000.0]
        t = Transport((200, {'access_token': 'NEW', 'expires_in': 5184000}))
        ts = TokenStore(d / 'tok.json', 'OLD', Http(t), clock=lambda: now[0])
        self.assertEqual(ts.token(), 'OLD')
        now[0] += 31 * 86400
        self.assertEqual(ts.token(), 'NEW')
        self.assertNotIn('OLD', t.requests[0]['url'])
        self.assertEqual(os.stat(d / 'tok.json').st_mode & 0o777, 0o600)
        self.assertEqual(TokenStore(d / 'tok.json', 'IGNORED', Http(Transport()), clock=lambda: now[0]).token(), 'NEW')

    def test_failed_refresh_keeps_the_working_token(self):
        from waset_runtime.instagram import TokenStore
        d = Path(tempfile.mkdtemp())
        now = [0.0]
        ts = TokenStore(d / 'tok.json', 'OLD', Http(Transport((500, {}))), clock=lambda: now[0])
        now[0] += 40 * 86400
        self.assertEqual(ts.token(), 'OLD')


if __name__ == '__main__':
    unittest.main()
