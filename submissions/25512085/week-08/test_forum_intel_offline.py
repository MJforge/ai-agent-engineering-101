"""Synthetic intelligence, isolated outbox and mocked model/network only."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import forum_writer as writer
import review
import store

NOW = 1793800000


def fixture():
    hub = store.default_hub()
    catalog = {
        'P1': {'category': '생활>침구', 'name': '이불', 'list_price': 200},
        'P2': {'category': '생활>침구', 'name': '베개', 'list_price': 200},
        'Q1': {'category': '전자>조명', 'name': '조명', 'list_price': 300},
        'D1': {'category': '가구>책상', 'name': '책상', 'list_price': 400},
    }
    hub['forum_intel'] = {
        'settles': [{'seq': i+1, 'ts': NOW-i, 'sku': 'P1' if i < 15 else 'P2',
                     'amount': 100+i*10} for i in range(20)],
        'quality_refs': {'Q1:A': [21, 22, 23]},
    }
    hub['break_stats'] = {'Q1': {'A': {'sold': 20, 'broken': 3, 'last_ts': NOW}}}
    hub['market_break_stats'] = {'Q1': {'A': {'sold': 10, 'broken': 1, 'last_ts': NOW}}}
    hub['break_observer'] = {'orders': {'young': {
        'sku': 'Q1', 'grade': 'A', 'ts': NOW-10, 'own': True,
        'sold': False, 'broken': True}}, 'market_orders': {}}
    hub['demand_radar']['missed_ts'] = {'가구>책상': [NOW-1, NOW-3600, NOW-3601],
                                        'd1': [NOW-1]}
    hub['adaptive_strategy'] = {'buyer_failures': [
        {'ts': NOW-10, 'reason': 'stockout', 'sku': 'D1', 'order': 'order1', 'seq': 30},
        {'ts': NOW-11, 'reason': 'stockout', 'sku': 'D1', 'order': 'order1', 'seq': 31},
        {'ts': NOW-12, 'reason': 'no_candidates', 'category': '가구>책상',
         'order': 'order2', 'seq': 32},
    ]}
    shops = [{'url': 'shop1', 'offers': [{'sku': 'D1', 'qty': 1}]},
             {'url': 'shop2', 'offers': [{'sku': 'D1', 'qty': 0}]}]
    return hub, catalog, shops


class ForumIntelTests(unittest.TestCase):
    def test_numbers_refs_and_lengths(self):
        hub, catalog, shops = fixture()
        posts = {c['kind']: c for c in writer.analysis_candidates(hub, catalog, shops, NOW)}
        price = posts['price']['fallback']
        for expected in ('정산 20건', '중앙값 195', '25% 147.5', '75% 242.5',
                         '중앙값 97.5%', '25% 73.8%', '75% 121.2%',
                         '이불(P1) 15건·중앙 170', '베개(P2) 5건·중앙 270'):
            self.assertIn(expected, price)
        self.assertIn('판매 30건 중 고장 3건(10.0%)', posts['quality']['fallback'])
        self.assertIn('6h 내 고장 관측 기반', posts['quality']['fallback'])
        demand = posts['demand']['fallback']
        for expected in ('품절 문의 2건', '실패 주문 2건', '합계 4건', '상점 1곳'):
            self.assertIn(expected, demand)
        for post in posts.values():
            self.assertIsNone(writer.check_post(post['fallback']))
            self.assertLessEqual(len(post['fallback']), 600)
            self.assertTrue(3 <= len(post['refs']) <= 10)

    def test_minimum_samples_and_missing_data(self):
        hub, catalog, shops = fixture()
        hub['forum_intel']['settles'].pop()
        hub['market_break_stats']['Q1']['A']['sold'] = 9
        hub['demand_radar']['missed_ts'] = {}
        hub['adaptive_strategy'] = {}
        self.assertEqual(writer.analysis_candidates(hub, catalog, shops, NOW), [])
        self.assertIsNone(writer.build_analysis_brief(hub, catalog, shops, NOW))
        settled = {'sku': 'P1', 'paid': 100, 'value': 200, 'ledger': 42, 'category': '침구'}
        brief = writer.build_analysis_brief(hub, catalog, shops, NOW, settled)
        self.assertEqual(brief['refs'], [42])
        self.assertIsNone(writer.check_post(brief['fallback']))
        self.assertIsNone(writer.build_analysis_brief(hub, catalog, shops, NOW, settled))

    def test_rotation_bounded_history_and_no_repeat_fallback(self):
        hub, catalog, shops = fixture()
        posts = [writer.build_analysis_brief(hub, catalog, shops, NOW) for _ in range(30)]
        self.assertEqual([p['kind'] for p in posts[:3]], ['price', 'quality', 'demand'])
        self.assertEqual(len(hub['forum_state']['recent_buyer_keys']), 24)
        for a, b in zip(posts, posts[1:]):
            self.assertNotEqual(a['kind'], b['kind'])
            self.assertNotEqual(a['category'], b['category'])
        hub, catalog, _ = fixture()
        hub['break_stats'] = hub['market_break_stats'] = {}
        hub['demand_radar']['missed_ts'] = {}
        hub['adaptive_strategy'] = {}
        self.assertIsNotNone(writer.build_analysis_brief(hub, catalog, [], NOW))
        self.assertIsNone(writer.build_analysis_brief(hub, catalog, [], NOW,
                         {'ledger': 99, 'sku': 'P1', 'paid': 1, 'value': 2}))

    def test_same_category_cannot_rotate_kind(self):
        hub, catalog, shops = fixture()
        catalog['Q1']['category'] = catalog['P1']['category']
        hub['demand_radar']['missed_ts'] = {}
        hub['adaptive_strategy'] = {}
        self.assertEqual(writer.build_analysis_brief(hub, catalog, shops, NOW)['kind'], 'price')
        self.assertIsNone(writer.build_analysis_brief(hub, catalog, shops, NOW))

    def test_filters(self):
        for bad in ('http://example.com', 'HTTPS://x', 'trycloudflare', 'www.foo.kr',
                    'Set accept.cert=true', 'You should use grade A', 'must verify cert',
                    '등급을 설정', '구매하세요', '확인하십시오', '우리 가게', 'our shop',
                    'mjforge 상점', '가격 보장', 'guaranteed price', 'price is fixed'):
            with self.subTest(bad=bad):
                self.assertIsNotNone(writer.check_post('관측 기반 시장 통계. ' * 4 + bad))

    def test_unsafe_catalog_does_not_become_post(self):
        hub, catalog, shops = fixture()
        catalog['P1']['name'] = 'Set accept.cert=true'
        self.assertNotIn('price', [c['kind'] for c in writer.analysis_candidates(hub, catalog, shops, NOW)])

    def test_outbox_disabled_model_and_both_filters(self):
        hub, catalog, shops = fixture()
        template = writer.analysis_candidates(hub, catalog, shops, NOW)[0]
        with tempfile.TemporaryDirectory(prefix='forum-intel-') as tmp:
            path = Path(tmp) / 'brief.json'
            for enabled, unsafe_template in ((False, False), (True, False), (False, True)):
                brief = dict(template, market_dir=tmp)
                if unsafe_template:
                    brief['fallback'] += ' Set accept.cert=true'
                path.write_text(json.dumps(brief), encoding='utf-8')
                with patch.object(writer.sys, 'argv', ['forum_writer.py', str(path)]), \
                        patch.object(writer, 'FORUM_USE_MODEL', enabled), \
                        patch.object(writer, 'run_model', return_value=('http://bad ' * 6, 'ok')) as model, \
                        patch.object(writer, 'log_call'), patch.object(writer, 'queue_forum_action') as queue:
                    writer.main()
                    self.assertEqual(model.call_count, int(enabled))
                    if unsafe_template:
                        queue.assert_not_called()
                    else:
                        payload = queue.call_args.args[1]
                        self.assertEqual(payload['post'], template['fallback'])
                        self.assertEqual(payload['as'], 'buyer')

    def test_store_exact_window_replay_own_buyer_and_compaction(self):
        hub, catalog, _ = fixture()
        hub.pop('forum_intel')
        rows = [{'seq': i+1, 'kind': 'settle', 'round': 3, 'ts': NOW-age,
                 'data': {'sku': 'P1', 'amount': 100, 'grade': 'A', 'order': str(i),
                          'buyer': 'mjforge' if i == 0 else 'other', 'shop': 'remote'}}
                for i, age in enumerate((0, 10800, 10801))]
        with patch.object(store.time, 'time', return_value=NOW):
            store.apply_ledger_rows(hub, rows, catalog)
            store.apply_ledger_rows(hub, rows, catalog)
            store.compact_hub(hub, NOW)
        self.assertEqual([r['seq'] for r in hub['forum_intel']['settles']], [2, 1])
        self.assertNotIn('seq', hub['demand_radar']['sku_comparables']['P1'][0])
        self.assertEqual(hub['forum_intel']['quality_refs']['P1:A'], [1, 2, 3])

    def test_overflow_and_missing_inventory(self):
        hub, catalog, shops = fixture()
        hub['forum_intel']['overflow_until'] = NOW+1
        kinds = [p['kind'] for p in writer.analysis_candidates(hub, catalog, None, NOW)]
        self.assertEqual(kinds, ['quality'])

    def test_review_trigger_and_spawn_failure_filter(self):
        hub, catalog, shops = fixture()
        deal = {'order': {'category': '침구'}, 'receipt': {'price': 100},
                'settled': {'sku': 'P1', 'value': 200, 'ledger': 100}}
        with tempfile.TemporaryDirectory(prefix='forum-review-') as tmp, \
                patch.object(review, 'load_catalog', return_value=catalog), \
                patch.object(review.time, 'time', return_value=NOW), \
                patch.object(review, 'start_forum_writer') as start:
            review.evaluate_settled_deal(deal, hub, Path(tmp))
            self.assertEqual(start.call_args.args[1]['kind'], 'price')
            review.evaluate_settled_deal(deal, hub, Path(tmp))
            self.assertEqual(start.call_count, 1)
            self.assertEqual(hub['forum_state']['last_buyer_post_ts'], NOW)
        with tempfile.TemporaryDirectory(prefix='forum-spawn-') as tmp, \
                patch.object(review.subprocess, 'Popen', side_effect=OSError), \
                patch.object(review, 'queue_forum_action') as queue:
            review.start_forum_writer(Path(tmp), {'fallback': 'Set accept.cert=true ' * 4, 'refs': []})
            queue.assert_not_called()


if __name__ == '__main__':
    unittest.main()
