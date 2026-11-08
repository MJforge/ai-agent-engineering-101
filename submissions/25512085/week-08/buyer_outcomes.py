"""Passive, sanitized buyer evidence. Never a pricing/ranking input."""
from __future__ import annotations

from collections import Counter, OrderedDict
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import sys
import threading

MAX_BYTES = 256 * 1024 * 1024
MAX_ORDERS = 20000
MAX_EVENTS = 200000
MAX_BATCH_ROWS = 2000
_LOCK = threading.RLock()
_ID = re.compile(r"[A-Za-z0-9_.:\-]{1,128}\Z")


def identifier(value):
    return value if type(value) is str and _ID.fullmatch(value) else None


def number(value):
    return value if type(value) in (int, float) and 0 <= value <= 10**12 and math.isfinite(value) else None


def normalize(rows, catalog, own_name, observed_at):
    """Only existing ledger data; no receipt, budget, URL, name/time matching."""
    counts = Counter()
    events = []
    for index, row in enumerate(rows):
        if index >= MAX_BATCH_ROWS:
            counts['batch_cap_unknown'] += len(rows) - index
            break
        kind = row.get('kind')
        if kind not in ('settle', 'broken'):
            continue
        data = row.get('data') or {}
        if not isinstance(data, dict):
            counts['invalid_data'] += 1
            continue
        buyer = identifier(data.get('buyer'))
        if not buyer:
            counts['no_identity'] += 1
            continue
        if buyer != own_name:
            continue  # Including our seller's refunds to other buyers.
        order = identifier(data.get('order'))
        seq, ts = row.get('seq'), number(row.get('ts'))
        if not order:
            counts['no_order'] += 1
            continue
        if type(seq) is not int or not 0 <= seq <= 10**12 or ts is None:
            counts['no_ledger_or_timestamp'] += 1
            continue
        event = dict(version=1, kind=kind, buyer=buyer, order=order, seq=seq,
                     ts=ts, observed_at=observed_at, unit=identifier(data.get('unit')))
        if kind == 'settle':
            sku = identifier(data.get('sku') or data.get('item'))
            meta = catalog.get(sku, {})
            lp = number(meta.get('list_price')) if isinstance(meta, dict) else None
            if not lp:
                counts['unknown_price'] += 1
            event.update(sku=sku, paid_amount=number(data.get('amount')),
                         list_price=lp or None,
                         list_price_source='catalog_at_observation' if lp else None,
                         grade=data.get('grade') if data.get('grade') in ('A', 'B', 'C') else None,
                         warranty_hours=number(data.get('warranty_hours')),
                         goods=bool(sku and re.fullmatch(r'[gG][0-9a-fA-F]{7,10}', sku)))
        else:
            # The existing seller observer documents this field, not `amount`.
            event['refunded_amount'] = number(data.get('refunded'))
        events.append(event)
    return events, dict(counts)


def diagnostic(reason):
    # No exception text, filenames, raw input or credentials.
    try:
        print('[buyer-outcomes] ' + reason, file=sys.stderr, flush=True)
    except Exception:
        pass


@contextmanager
def exclusive(path):
    """Nonblocking, fail-closed advisory lock; failure never bypasses locking."""
    with _LOCK:
        with path.with_suffix('.lock').open('a+b') as lock:
            if os.name == 'nt':
                import msvcrt
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                if os.name == 'nt':
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def observe(rows, catalog, own_name, observed_at, path):
    """Append-only, best effort. No network and no hub state mutation."""
    try:
        events = prepare(rows, catalog, own_name, observed_at)
    except Exception:
        diagnostic('journal_prepare_failed_unknown')
        return
    append_events(events, path)


def prepare(rows, catalog, own_name, observed_at):
    """In-memory snapshot only; safe to call while the hub is locked."""
    events, counts = normalize(rows, catalog, own_name, observed_at)
    if events or counts:
        events.append(dict(version=1, kind='coverage', observed_at=observed_at,
                           unlinked=counts, complete=False))
    return events


def append_events(events, path):
    """Blocking append/flush/fsync, to be called AFTER releasing hub locks."""
    if not events:
        return
    try:
        payload = ''.join(json.dumps(e, separators=(',', ':'), allow_nan=False) + '\n' for e in events).encode('utf-8')
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with exclusive(path):
            with path.open('a+b') as stream:
                stream.seek(0, 2)
                size = stream.tell()
                # Separate a crash-truncated tail WITHOUT editing the original bytes.
                prefix = b''
                if size:
                    stream.seek(-1, 2)
                    if stream.read(1) != b'\n':
                        prefix = b'\n'
                if size + len(prefix) + len(payload) > MAX_BYTES:
                    diagnostic('journal_capacity_unknown')
                    return
                stream.seek(0, 2)
                stream.write(prefix + payload)
                stream.flush()
                os.fsync(stream.fileno())
    except Exception:
        diagnostic('journal_write_failed_unknown')


_COVERAGE_KEYS = ('no_identity', 'no_order', 'no_ledger_or_timestamp',
                  'unknown_price', 'invalid_data', 'batch_cap_unknown')


def validate_event(raw):
    """One v1 boundary for file AND direct replay; return allowlisted copies only."""
    if type(raw) is not dict or type(raw.get('version')) is not int or raw['version'] != 1:
        return None
    kind = raw.get('kind')
    if type(kind) is not str or kind not in ('settle', 'broken', 'coverage'):
        return None
    observed = number(raw.get('observed_at'))
    if observed is None:
        return None
    clean = dict(version=1, kind=kind, observed_at=observed)
    if kind == 'coverage':
        counts = raw.get('unlinked')
        if type(counts) is not dict or raw.get('complete') is not False:
            return None
        known = {}
        for key in _COVERAGE_KEYS:
            if key in counts:
                value = counts[key]
                if type(value) is not int or not 0 <= value <= 10**12:
                    return None
                known[key] = value
        clean.update(unlinked=known, complete=False)
        return clean
    seq, ts = raw.get('seq'), number(raw.get('ts'))
    if type(seq) is not int or not 0 <= seq <= 10**12 or ts is None:
        return None
    buyer, order = identifier(raw.get('buyer')), identifier(raw.get('order'))
    if buyer is None or order is None:
        return None
    clean.update(buyer=buyer, order=order, seq=seq, ts=ts)
    for key in ('unit', 'sku') if kind == 'settle' else ('unit',):
        value = raw.get(key)
        if value is not None and identifier(value) is None:
            return None
        clean[key] = value
    for key in ('paid_amount', 'list_price', 'warranty_hours') if kind == 'settle' else ('refunded_amount',):
        value = raw.get(key)
        if value is not None and number(value) is None:
            return None
        clean[key] = value
    if kind == 'settle':
        grade, source, goods = raw.get('grade'), raw.get('list_price_source'), raw.get('goods')
        if grade is not None and (type(grade) is not str or grade not in ('A', 'B', 'C')):
            return None
        if source is not None and (type(source) is not str or source != 'catalog_at_observation'):
            return None
        if type(goods) is not bool:
            return None
        lp = clean['list_price']
        if ((lp is not None and (lp <= 0 or source != 'catalog_at_observation'))
                or (lp is None and source is not None)):
            return None
        is_goods = bool(clean['sku'] and re.fullmatch(r'[gG][0-9a-fA-F]{7,10}', clean['sku']))
        if goods != is_goods:
            return None
        clean.update(grade=grade, list_price_source=source, goods=goods)
    return clean


def read_events(path):
    """Streaming offline replay; corrupt/partial rows become explicit unknown evidence."""
    with Path(path).open('rb') as stream:
        while True:
            line = stream.readline(4097)
            if not line:
                return
            if len(line) > 4096:
                while line and not line.endswith(b'\n'):
                    line = stream.readline(4097)
                yield {'kind': 'corrupt'}
                continue
            try:
                event = json.loads(line)
                yield validate_event(event) or {'kind': 'corrupt'}
            except (ValueError, UnicodeError, RecursionError):
                yield {'kind': 'corrupt'}


def summarize(events, own_name, now, *, max_orders=MAX_ORDERS):
    """Bounded pure replay. No aggregate official Saved without complete cohorts."""
    orders, breaks = OrderedDict(), OrderedDict()
    seen = set()
    counters = Counter()
    truncated = False
    try:
        iterator = iter(events)
    except TypeError:
        iterator = iter([None])
    for index in range(MAX_EVENTS + 1):
        try:
            raw = next(iterator)
        except StopIteration:
            break
        except Exception:
            counters['iterator_error_unknown'] += 1
            truncated = True
            break
        if index >= MAX_EVENTS:
            counters['event_cap_unknown'] += 1
            truncated = True
            break
        e = validate_event(raw)
        if e is None:
            counters['corrupt_unknown'] += 1
            continue
        kind = e.get('kind')
        if kind == 'coverage':
            counters.update(e.get('unlinked', {}))
            continue
        if kind not in ('settle', 'broken') or e.get('buyer') != own_name:
            continue
        key = (kind, e.get('seq'))
        if key in seen:
            continue
        seen.add(key)
        order = e.get('order')
        if not identifier(order):
            counters['no_order'] += 1
            continue
        target = orders if kind == 'settle' else breaks
        if order in target:
            # Cannot assume multiple refunds are incremental; no summation guess.
            target[order]['conflict'] = True
        else:
            target[order] = dict(e)
        if len(target) > max_orders:
            target.popitem(last=False)
            counters['pending_eviction_unknown'] += 1
            truncated = True
    results = []
    for order, sale in orders.items():
        broken = breaks.get(order)
        paid, lp = sale.get('paid_amount'), sale.get('list_price')
        reason = None
        contribution = None
        if sale.get('conflict') or (broken and broken.get('conflict')):
            reason = 'conflicting_events'
        elif broken and sale.get('unit') and broken.get('unit') and sale['unit'] != broken['unit']:
            reason = 'unit_mismatch'
        elif broken and broken['ts'] < sale['ts']:
            reason = 'break_before_settlement'
        elif not sale.get('goods'):
            reason = 'food_served_value_unknown'
        elif number(paid) is None or not number(lp):
            reason = 'price_unknown'
        elif broken:
            refund = broken.get('refunded_amount')
            if number(refund) is None or refund > paid:
                reason = 'refund_unknown'
            elif refund == paid and paid > 0:
                contribution = 0
            elif refund == 0:
                contribution = -paid / lp
            else:
                reason = 'partial_refund_semantics_unknown'
        else:
            reason = 'ledger_coverage_unproven'
        if truncated:
            reason = 'replay_truncated_unknown'
            contribution = None
        results.append(dict(order=order, ledger=sale.get('seq'),
                            mature_6h=now >= sale['ts'] + 21600,
                            observation_complete=False,
                            surface_discount=(lp-paid)/lp if number(paid) is not None and number(lp) else None,
                            known_broken_contribution=contribution, unknown_reason=reason,
                            broken_ts=broken.get('ts') if broken else None,
                            refunded_amount=broken.get('refunded_amount') if broken else None))
    counters['broken_without_settle'] = len(set(breaks) - set(orders))
    return dict(cohorts=results, coverage=dict(counters), official_saved=None,
                complete=False, truncated=truncated)
