import contextlib
import io
import unittest
from unittest.mock import Mock

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

    def mappings(self, site):
        return {10: ('Europe', '123')}

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

    def test_report_only_and_unknown_mapping(self):
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
        client.get = Mock(side_effect=[([{'id': 100}], '2'), ([{'id': 101}], '2')])
        self.assertEqual(client.approved_ids(), {100, 101})
        for call in client.get.call_args_list:
            self.assertEqual(call.kwargs['_fields'], 'id')
            self.assertEqual(call.kwargs['status'], 'approved')

    def test_incomplete_scan_aborts(self):
        client = sync.WooClient('www.tsim.in', 'key', 'secret')
        client.get = Mock(return_value=([], '2'))
        with self.assertRaises(sync.SyncError):
            client.approved_ids()

    def test_utc_and_missing_optional_fields(self):
        record = sync.prepare_review('www.tsim.in', review(1), None)
        self.assertEqual(record[3:5], (None, None))
        self.assertEqual(record[-1].isoformat(), '2026-01-01T12:00:00+00:00')


if __name__ == '__main__':
    unittest.main()
