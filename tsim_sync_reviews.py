#!/usr/bin/env python3
"""Import approved WooCommerce reviews once; default mode never writes."""

import argparse
import base64
from datetime import datetime, timezone
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler


SITES = {
    'www.tsim.in': 'TSIM_IN_API',
    'www.tsim.mobi': 'TSIM_MOBI_API',
}


class SyncError(Exception):
    """Safe operator-facing error; never includes credentials or response bodies."""


def log(event, **fields):
    print(json.dumps({'time': datetime.now(timezone.utc).isoformat(),
                      'event': event, **fields}, sort_keys=True), flush=True)


class NoRedirect(HTTPRedirectHandler):
    # Do not forward the Authorization header to a redirect destination.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class WooClient:
    def __init__(self, site, key, secret):
        self.site = site
        self.base = 'https://' + site + '/wp-json/wc/v3/products/reviews'
        token = base64.b64encode((key + ':' + secret).encode()).decode()
        self.authorization = 'Basic ' + token
        self.opener = build_opener(NoRedirect())

    def get(self, **params):
        request = Request(self.base + '?' + urlencode(params), headers={
            'Authorization': self.authorization,
            'Accept': 'application/json', 'Cache-Control': 'no-cache',
        })
        for attempt in range(3):
            try:
                with self.opener.open(request, timeout=30) as response:
                    result = json.load(response)
                    pages = response.headers.get('X-WP-TotalPages')
                if not isinstance(result, list):
                    raise SyncError('Unexpected review response')
                return result, pages
            except HTTPError as exc:
                retryable = exc.code == 429 or exc.code >= 500
                if not retryable or attempt == 2:
                    raise SyncError('WooCommerce HTTP status %s' % exc.code) from None
            except (URLError, TimeoutError, OSError):
                if attempt == 2:
                    raise SyncError('WooCommerce connection failed') from None
            except (ValueError, UnicodeError):
                raise SyncError('Invalid WooCommerce JSON') from None
            log('request_retry', site=self.site, attempt=attempt + 1)
            time.sleep(2 ** attempt)

    def approved_ids(self):
        ids = set()
        page = 1
        while True:
            rows, pages = self.get(status='approved', _fields='id',
                                   orderby='id', order='asc', per_page=100, page=page)
            try:
                total_pages = int(pages)
                if total_pages < 0:
                    raise ValueError()
                for row in rows:
                    ids.add(positive_id(row['id']))
            except (ValueError, TypeError, KeyError):
                raise SyncError('Invalid approved-ID pagination response') from None
            if page >= total_pages:
                return ids
            if not rows:
                raise SyncError('Incomplete approved-ID scan')
            page += 1

    def reviews(self, ids):
        rows, _ = self.get(status='approved', include=','.join(map(str, ids)),
                           per_page=100, context='edit', orderby='id', order='asc',
                           _fields='id,product_id,status,rating,review,reviewer,verified,date_created_gmt')
        return rows


def positive_id(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError('Invalid ID')
    return value


def prepare_review(site, raw, mapping):
    """Unknown custom fields remain NULL until the website field contract is verified."""
    review_id = positive_id(raw['id'])
    product_id = positive_id(raw['product_id'])
    rating = raw['rating']
    if isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 5:
        raise ValueError('Invalid rating')
    if not isinstance(raw['review'], str) or not isinstance(raw['reviewer'], str):
        raise ValueError('Invalid review text or reviewer')
    verified = raw.get('verified', False)
    if not isinstance(verified, bool):
        raise ValueError('Invalid verified flag')
    # WooCommerce's GMT field is UTC even when its string lacks a suffix.
    created = datetime.fromisoformat(raw['date_created_gmt'].replace('Z', '+00:00'))
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (site, review_id, product_id, *(mapping or (None, None)),
            rating, raw['review'], raw['reviewer'], verified, created)


class ReviewStore:
    def __init__(self, conn):
        self.conn = conn

    def credentials(self, site):
        prefix = SITES[site]
        names = [prefix + '_consumer_key', prefix + '_consumer_secret']
        with self.conn.cursor() as cur:
            cur.execute('SELECT key, value FROM config_values WHERE name = %s AND key = ANY(%s)',
                        (site, names))
            rows = cur.fetchall()
        values = dict(rows)
        if len(rows) != 2 or set(values) != set(names) or not all(values.values()):
            raise SyncError('Missing or ambiguous WooCommerce credentials')
        return values[names[0]], values[names[1]]

    def existing_ids(self, site):
        with self.conn.cursor() as cur:
            cur.execute('SELECT source_review_id FROM tsim_website_reviews WHERE source_site = %s',
                        (site,))
            return {row[0] for row in cur.fetchall()}

    def mappings(self, site):
        with self.conn.cursor() as cur:
            cur.execute('SELECT source_product_id, canonical_parent, cid '
                        'FROM tsim_review_product_mapping WHERE source_site = %s', (site,))
            return {row[0]: (row[1], row[2]) for row in cur.fetchall()}

    def insert(self, record):
        try:
            with self.conn.cursor() as cur:
                cur.execute('''INSERT INTO tsim_website_reviews
                    (source_site, source_review_id, source_product_id, canonical_parent, cid,
                     rating, review_text, reviewer_name, verified_purchase, source_created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (source_site, source_review_id) DO NOTHING''', record)
                inserted = cur.rowcount == 1
            self.conn.commit()
            return inserted
        except Exception:
            self.conn.rollback()
            raise


def sync_site(site, client, store, apply=False):
    approved = client.approved_ids()
    missing = sorted(approved - store.existing_ids(site))
    mappings = store.mappings(site)
    stats = dict(approved_ids=len(approved), missing=len(missing), imported=0,
                 ready=0, unmapped=0, failed=0, deferred=0, duplicates=0)
    for offset in range(0, len(missing), 100):
        batch = missing[offset:offset + 100]
        try:
            rows = client.reviews(batch)
        except SyncError as exc:
            stats['failed'] += len(batch)
            log('batch_failed', site=site, count=len(batch), reason=str(exc))
            continue
        seen = set()
        for raw in rows:
            review_id = None
            try:
                review_id = positive_id(raw['id'])
                if review_id not in batch or review_id in seen:
                    raise ValueError('Unexpected or duplicate response ID')
                seen.add(review_id)
                if raw.get('status') != 'approved':
                    stats['deferred'] += 1
                    continue
                record = prepare_review(site, raw, mappings.get(raw['product_id']))
                if record[3] is None:
                    stats['unmapped'] += 1
                if apply:
                    if store.insert(record):
                        stats['imported'] += 1
                    else:
                        stats['duplicates'] += 1
                stats['ready'] += 1
            except Exception:
                stats['failed'] += 1
                log('record_failed', site=site, review_id=review_id)
        # Approval can change between requests; missing IDs remain eligible next run.
        stats['deferred'] += len(set(batch) - seen)
    log('site_summary', site=site, mode='apply' if apply else 'report', **stats)
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Insert missing approved reviews')
    parser.add_argument('--site', choices=list(SITES), help='Default: both sites')
    args = parser.parse_args()
    try:
        import psycopg2
        # Environment and .pgpass override these server defaults.
        conn = psycopg2.connect(dbname=os.environ.get('PGDATABASE', 'e2fax'),
                                user=os.environ.get('PGUSER', 'domains'),
                                host=os.environ.get('PGHOST', '/tmp'))
        conn.autocommit = False
        if not args.apply:
            conn.set_session(readonly=True)
    except Exception:
        log('startup_failed', reason='PostgreSQL connection or dependency unavailable')
        return 1
    failed = False
    try:
        store = ReviewStore(conn)
        for site in ([args.site] if args.site else SITES):
            try:
                client = WooClient(site, *store.credentials(site))
                stats = sync_site(site, client, store, args.apply)
                failed = failed or bool(stats['failed'])
            except Exception as exc:
                conn.rollback()
                log('site_failed', site=site, error_type=type(exc).__name__,
                    reason=str(exc) if isinstance(exc, SyncError) else 'Database or response failure')
                failed = True
            finally:
                # End read transactions, including report mode, before the next site.
                conn.rollback()
    finally:
        conn.close()
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
