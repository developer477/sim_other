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
        skipped_zero_rating = 0
        while True:
            rows, pages = self.get(status='approved', _fields='id,rating',
                                   orderby='id', order='asc', per_page=100, page=page)
            try:
                total_pages = int(pages)
                if total_pages < 0:
                    raise ValueError()
                for row in rows:
                    review_id = positive_id(row['id'])
                    rating = row['rating']
                    if isinstance(rating, bool) or not isinstance(rating, int) or not 0 <= rating <= 5:
                        raise ValueError()
                    if rating == 0:
                        skipped_zero_rating += 1
                    else:
                        ids.add(review_id)
            except (ValueError, TypeError, KeyError):
                raise SyncError('Invalid approved-ID pagination response') from None
            if page >= total_pages:
                log('discovery_summary', site=self.site, rated_approved_ids=len(ids),
                    skipped_zero_rating=skipped_zero_rating)
                return ids
            if not rows:
                raise SyncError('Incomplete approved-ID scan')
            page += 1

    def reviews(self, ids):
        rows, _ = self.get(status='approved', include=','.join(map(str, ids)),
                           per_page=100, context='edit', orderby='id', order='asc',
                           _fields='id,product_id,status,rating,review,reviewer,reviewer_email,verified,date_created_gmt,meta_data')
        return rows


def positive_id(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError('Invalid ID')
    return value


def variation_metadata(metadata):
    """Read the three recorded values directly; do not infer a child identity."""
    if metadata is None:
        return None, None, None
    if not isinstance(metadata, list):
        raise ValueError('Invalid variation metadata')
    keys = ('_review_variation_id', '_review_variation_sku', '_review_variation_name')
    values = {}
    for item in metadata:
        if not isinstance(item, dict) or item.get('key') not in keys:
            continue
        key = item['key']
        if key in values:
            raise ValueError('Invalid variation metadata')
        values[key] = item.get('value')
    variation_id, sku, name = [values.get(key) for key in keys]
    if variation_id in (None, ''):
        variation_id = None
    else:
        if isinstance(variation_id, str) and variation_id.isascii() and variation_id.isdigit():
            variation_id = int(variation_id)
        variation_id = positive_id(variation_id)
    for value in (sku, name):
        if value is not None and not isinstance(value, str):
            raise ValueError('Invalid variation metadata')
    return variation_id, sku or None, name or None


def prepare_review(site, raw, mapping):
    """Store source variation metadata as supplied, without a child catalog lookup."""
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
    email = raw.get('reviewer_email')
    if email is not None and not isinstance(email, str):
        raise ValueError('Invalid reviewer email')
    variation = variation_metadata(raw.get('meta_data'))
    # WooCommerce's GMT field is UTC even when its string lacks a suffix.
    try:
        created = datetime.fromisoformat(raw['date_created_gmt'].replace('Z', '+00:00'))
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Invalid creation timestamp') from None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (site, review_id, product_id, *(mapping or (None, None)),
            rating, raw['review'], raw['reviewer'], verified, email or None, *variation, created)


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

    def parent_identities(self, site, product_ids):
        """Resolve source parents from the existing, domain-specific catalog."""
        if not product_ids:
            return {}
        with self.conn.cursor() as cur:
            cur.execute('SELECT id, meta_data FROM shopshastra_products_latest '
                        'WHERE domainname = %s AND id = ANY(%s) AND parent_id = 0',
                        (site, list(product_ids)))
            rows = cur.fetchall()
        identities = {}
        seen = set()
        for product_id, metadata in rows:
            if product_id in seen:
                raise SyncError('Duplicate parent ID in product catalog')
            seen.add(product_id)
            if not isinstance(metadata, list):
                continue
            values = [item.get('value') for item in metadata
                      if isinstance(item, dict) and item.get('key') == 'cid']
            if len(values) == 1 and isinstance(values[0], str) and values[0].strip():
                cid = values[0].strip()
                identities[product_id] = (cid, cid)
            else:
                log('parent_unresolved', site=site, product_id=product_id,
                    reason='Missing or ambiguous CID')
        return identities

    def insert(self, record):
        try:
            with self.conn.cursor() as cur:
                cur.execute('''INSERT INTO tsim_website_reviews
                    (source_site, source_review_id, source_product_id, canonical_parent, cid,
                     rating, review_text, reviewer_name, verified_purchase, reviewer_email,
                     source_variation_id, variation_sku, variation_name, source_created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
    stats = dict(approved_ids=len(approved), missing=len(missing), imported=0,
                 ready=0, unmapped=0, failed=0, deferred=0, duplicates=0)
    for offset in range(0, len(missing), 100):
        batch = missing[offset:offset + 100]
        try:
            rows = client.reviews(batch)
            product_ids = {positive_id(row['product_id']) for row in rows
                           if isinstance(row, dict) and isinstance(row.get('product_id'), int)
                           and not isinstance(row['product_id'], bool) and row['product_id'] > 0}
            mappings = store.parent_identities(site, product_ids)
        except SyncError as exc:
            stats['failed'] += len(batch)
            log('batch_failed', site=site, count=len(batch), reason=str(exc))
            continue
        seen = set()
        for raw in rows:
            review_id = None
            stage = 'validation'
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
                    stage = 'insert'
                    if store.insert(record):
                        stats['imported'] += 1
                    else:
                        stats['duplicates'] += 1
                stats['ready'] += 1
            except Exception as exc:
                stats['failed'] += 1
                # Raw database errors may contain review text and reviewer details.
                reasons = {
                    'Invalid rating', 'Invalid ID', 'Invalid verified flag',
                    'Invalid review text or reviewer', 'Invalid creation timestamp',
                    'Invalid variation metadata', 'Invalid reviewer email',
                    'Unexpected or duplicate response ID',
                }
                reason = 'Database insertion failed' if stage == 'insert' else 'Invalid source record'
                if stage == 'validation' and isinstance(exc, ValueError) and str(exc) in reasons:
                    reason = str(exc)
                elif stage == 'validation' and isinstance(exc, KeyError):
                    reason = 'Missing source field'
                details = dict(stage=stage, error_type=type(exc).__name__, reason=reason)
                sqlstate = getattr(exc, 'pgcode', None)
                if isinstance(sqlstate, str) and len(sqlstate) == 5 and sqlstate.isalnum():
                    details['sqlstate'] = sqlstate
                log('record_failed', site=site, review_id=review_id, **details)
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
