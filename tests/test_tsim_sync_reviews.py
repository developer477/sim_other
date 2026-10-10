import contextlib
import io
import unittest
from unittest.mock import MagicMock, Mock

import tsim_sync_reviews as sync


def review(review_id, **changes):
    data = dict(id=review_id, product_id=10, rating=5, review='Good',
                reviewer='Customer', verified=True, status='approved',
                date_created_gmt='2026-01-01T12:00:00')
    data.update(changes)
    return data


class Store:
    def __init__(self):
        self.records = {}
        self.fail_ids = set()

    def existing_ids(self, site):
        return {key[1] for key in self.records if key[0] == site}

    def parent_cids(self, site, product_ids):
        return {10: 'KR'} if 10 in product_ids else {}

    def insert(self, record):
        key = record[:2]
        if record[1] in self.fail_ids:
            raise RuntimeError('simulated DB failure')
        if key in self.records:
            return False
        self.records[key] = record
        return True


class SyncTests(unittest.TestCase):
    def run_sync(self, ids, rows, store=None, apply=True, site='www.tsim.in'):
        client = Mock()
        client.approved_ids.return_value = set(ids)
        client.reviews.return_value = rows
        store = store or Store()
        with contextlib.redirect_stdout(io.StringIO()):
            stats = sync.sync_site(site, client, store, apply)
        return stats, store, client

    def test_import_once_and_delayed_approval(self):
        stats, store, _ = self.run_sync([101], [review(101)])
        self.assertEqual(stats['imported'], 1)
        self.assertEqual(store.records[('www.tsim.in', 101)][3], 'KR')
        stats, _, client = self.run_sync([100, 101], [review(100)], store)
        client.reviews.assert_called_once_with([100])
        self.assertEqual(stats['imported'], 1)
        stats, _, client = self.run_sync([100, 101], [], store)
        client.reviews.assert_not_called()
        self.assertEqual(len(store.records), 2)

    def test_same_id_on_both_sites_is_independent(self):
        _, store, _ = self.run_sync([1], [review(1)])
        stats, _, _ = self.run_sync([1], [review(1)], store, site='www.tsim.mobi')
        self.assertEqual(stats['imported'], 1)
        self.assertEqual(len(store.records), 2)

    def test_report_only_and_unknown_cid(self):
        stats, store, _ = self.run_sync([1], [review(1, product_id=999)], apply=False)
        self.assertEqual(stats['ready'], 1)
        self.assertEqual(stats['unmapped'], 1)
        self.assertFalse(store.records)

    def test_failed_record_retried_without_blocking_others(self):
        store = Store()
        store.fail_ids.add(1)
        stats, _, _ = self.run_sync([1, 2], [review(1), review(2)], store)
        self.assertEqual(stats['failed'], 1)
        self.assertEqual(stats['imported'], 1)
        store.fail_ids.clear()
        stats, _, client = self.run_sync([1, 2], [review(1)], store)
        client.reviews.assert_called_once_with([1])
        self.assertEqual(stats['imported'], 1)

    def test_pending_disappeared_and_invalid_reviews_are_not_inserted(self):
        stats, store, _ = self.run_sync([1, 2, 3],
                                       [review(1, status='hold'), review(2, rating=0)])
        self.assertFalse(store.records)
        self.assertEqual(stats['failed'], 1)
        self.assertEqual(stats['deferred'], 2)

    def test_batch_failure_can_retry_next_run(self):
        client = Mock()
        client.approved_ids.return_value = {1}
        client.reviews.side_effect = sync.SyncError('unavailable')
        store = Store()
        with contextlib.redirect_stdout(io.StringIO()):
            stats = sync.sync_site('www.tsim.in', client, store, True)
        self.assertEqual(stats['failed'], 1)
        self.assertFalse(store.records)
        stats, _, _ = self.run_sync([1], [review(1)], store)
        self.assertEqual(stats['imported'], 1)

    def test_paginated_ids_only_request(self):
        client = sync.WooClient('www.tsim.in', 'key', 'secret')
        client.get = Mock(side_effect=[([{'id': 100, 'rating': 5}], '2'),
                                       ([{'id': 101, 'rating': 0}], '2')])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(client.approved_ids(), {100})
        for call in client.get.call_args_list:
            self.assertEqual(call.kwargs['_fields'], 'id,rating')
            self.assertEqual(call.kwargs['status'], 'approved')

    def test_incomplete_scan_aborts(self):
        client = sync.WooClient('www.tsim.in', 'key', 'secret')
        client.get = Mock(return_value=([], '2'))
        with self.assertRaises(sync.SyncError):
            client.approved_ids()

    def test_utc_and_missing_optional_fields(self):
        record = sync.prepare_review('www.tsim.in', review(1), None)
        self.assertIsNone(record[3])
        self.assertEqual(record[-1].isoformat(), '2026-01-01T12:00:00+00:00')

    def test_failure_logs_reason_without_record_or_database_details(self):
        import json
        client = Mock()
        client.approved_ids.return_value = {1, 2}
        client.reviews.return_value = [review(1, rating=0), review(2)]
        store = Store()
        store.fail_ids.add(2)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            sync.sync_site('www.tsim.in', client, store, True)
        failures = [json.loads(line) for line in output.getvalue().splitlines()
                    if json.loads(line)['event'] == 'record_failed']
        self.assertEqual(failures[0]['reason'], 'Invalid rating')
        self.assertEqual(failures[0]['stage'], 'validation')
        self.assertEqual(failures[1]['reason'], 'Database insertion failed')
        self.assertEqual(failures[1]['stage'], 'insert')
        self.assertNotIn('simulated DB failure', output.getvalue())
        self.assertNotIn('Customer', output.getvalue())

    def test_invalid_timestamp_has_safe_reason(self):
        with self.assertRaisesRegex(ValueError, '^Invalid creation timestamp$'):
            sync.prepare_review('www.tsim.in', review(1, date_created_gmt='private-value'), None)

    def test_store_variation_and_email_directly(self):
        raw = review(990720, reviewer_email='customer@example.test', meta_data=[
            {'key': '_review_variation_id', 'value': '1366335'},
            {'key': '_review_variation_name', 'value': '30 Day 5GB'},
            {'key': '_review_variation_sku', 'value': 'singapore-30d-5GB-esim'},
        ])
        record = sync.prepare_review('www.tsim.in', raw, 'SG')
        self.assertEqual(record[8:12], ('customer@example.test', 1366335,
                                        'singapore-30d-5GB-esim', '30 Day 5GB'))

    def test_legacy_review_unknown_variation_is_null(self):
        record = sync.prepare_review('www.tsim.in', review(1), None)
        self.assertEqual(record[8:12], (None, None, None, None))

    def test_legacy_none_variation_imports_with_nulls(self):
        raw = review(6597, product_id=278, rating=4, meta_data=[
            {'key': key, 'value': 'none'} for key in
            ('_review_variation_id', '_review_variation_sku', '_review_variation_name')
        ])
        stats, store, _ = self.run_sync([6597], [raw])
        self.assertEqual(stats['imported'], 1)
        self.assertEqual(stats['failed'], 0)
        self.assertEqual(store.records[('www.tsim.in', 6597)][9:12], (None, None, None))

    def test_invalid_variation_id_is_identified(self):
        with self.assertRaisesRegex(ValueError, '^Invalid variation ID$'):
            sync.variation_metadata([{'key': '_review_variation_id', 'value': 'invalid'}])

    def test_conflicting_variation_metadata_fails(self):
        with self.assertRaisesRegex(ValueError, 'Invalid variation metadata'):
            sync.variation_metadata([{'key': '_review_variation_sku', 'value': 'one'},
                                     {'key': '_review_variation_sku', 'value': 'two'}])

    def test_insert_passes_metadata_and_email_to_database(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value = Mock(rowcount=1)
        record = sync.prepare_review('www.tsim.in', review(1, meta_data=[
            {'key': '_review_variation_id', 'value': '1366335'},
            {'key': '_review_variation_sku', 'value': 'singapore-30d-5GB-esim'},
            {'key': '_review_variation_name', 'value': '30 Day 5GB'},
        ], reviewer_email='customer@example.test'), 'SG')
        self.assertTrue(sync.ReviewStore(conn).insert(record))
        query, params = cur.execute.call_args.args
        for column in ('source_variation_id', 'variation_sku', 'variation_name', 'reviewer_email'):
            self.assertIn(column, query)
        self.assertEqual(query.count('%s'), len(params))
        self.assertEqual(params, record)
        conn.commit.assert_called_once()

    def test_catalog_cid_lookup_is_domain_and_parent_scoped(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value = Mock()
        cur.fetchall.return_value = [(343939, [{'key': 'cid', 'value': 'KR'}])]
        cids = sync.ReviewStore(conn).parent_cids('www.tsim.in', {343939})
        self.assertEqual(cids, {343939: 'KR'})
        query, params = cur.execute.call_args.args
        self.assertIn('shopshastra_products_latest', query)
        self.assertIn('parent_id = 0', query)
        self.assertEqual(params, ('www.tsim.in', [343939]))

    def test_catalog_missing_and_ambiguous_cids_remain_unresolved(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value = Mock()
        cur.fetchall.return_value = [(1, []), (2, [{'key': 'cid', 'value': 'KR'},
                                                {'key': 'cid', 'value': 'US'}])]
        with contextlib.redirect_stdout(io.StringIO()):
            result = sync.ReviewStore(conn).parent_cids('www.tsim.in', {1, 2})
        self.assertEqual(result, {})

    def test_duplicate_catalog_parent_aborts_resolution(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value = Mock()
        cur.fetchall.return_value = [(1, [{'key': 'cid', 'value': 'KR'}]),
                                    (1, [{'key': 'cid', 'value': 'US'}])]
        with self.assertRaises(sync.SyncError):
            sync.ReviewStore(conn).parent_cids('www.tsim.in', {1})


if __name__ == '__main__':
    unittest.main()
