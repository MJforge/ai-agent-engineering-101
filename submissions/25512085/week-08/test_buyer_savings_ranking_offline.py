"""Synthetic Patch3.4 ranking regressions; no original state or external I/O."""
import copy
import unittest
from unittest.mock import patch

import market_buyer as buyer


NOW = 1_800_000_000


class BuyerSavingsRankingTests(unittest.TestCase):
    def select(self, prices, *, budget=100000, settles=None, memory=None,
               board=None, hub=None, kind='goods', acceptable=None):
        shops = [{'name': sku, 'url': 'https://' + sku + '.invalid',
                  'offers': [{'sku': sku, 'list_price': 999999999}]}
                 for sku in prices]
        catalog = {sku: {'list_price': price} for sku, price in prices.items()}
        order = {'order': 'synthetic', 'kind': kind,
                 'acceptable': list(prices) if acceptable is None else acceptable}
        hub = copy.deepcopy(hub or {})
        hub.setdefault('demand_radar', {})['sku_settles'] = settles or {}
        memory = copy.deepcopy(memory or {})
        original_inputs = copy.deepcopy((shops, catalog, order, memory))
        candidates = []
        real_filter = buyer.filter_candidates

        def capture(rows, state, now):
            candidates.extend(rows)
            return real_filter(rows, state, now)

        with patch.object(buyer, 'fetch_board_stats', return_value=board or {}), \
                patch.object(buyer, 'check_acp_support', return_value=True) as probe, \
                patch.object(buyer.time, 'time', return_value=NOW), \
                patch.object(buyer, 'filter_candidates', side_effect=capture):
            result = buyer.pick_round3_shop_and_sku(shops, memory, hub, catalog, order, budget)
        self.assertEqual((shops, catalog, order, memory), original_inputs)
        return result, {row[2]['sku']: row[0] for row in candidates}, probe.call_count

    def test_equal_discount_same_base_utility_across_price_scales(self):
        _, scores, _ = self.select({'small': 1000, 'large': 100000})
        self.assertAlmostEqual(scores['small'], scores['large'])
        self.assertAlmostEqual(scores['small'], .20 * (3.8 / 4.2))

    def test_higher_rate_beats_larger_absolute_saving(self):
        result, scores, probes = self.select({'large': 100000, 'small': 10000},
                                            settles={'small': [6500] * 3})
        # 3500 saved at 35% beats 20000 saved at 20%; no quote multiplexing.
        self.assertEqual(result[1]['sku'], 'small')
        self.assertGreater(scores['small'], scores['large'])
        self.assertEqual(probes, 1)

    def test_no_fixed_money_floor(self):
        _, scores, _ = self.select({'small': 1000, 'large': 10000},
                                    settles={'small': [650] * 3, 'large': [6500] * 3})
        self.assertAlmostEqual(scores['small'], .35 * (3.8 / 4.2))
        self.assertAlmostEqual(scores['small'], scores['large'])

    def test_bestseller_trust_and_deal_weights_remain(self):
        _, scores, _ = self.select({'plain': 10000, 'G1515d951': 10000,
                                    'trusted': 10000}, board={
                                        'trusted': {'stars': 5, 'deals': 20}})
        self.assertAlmostEqual(scores['G1515d951'] / scores['plain'], 1.35)
        self.assertAlmostEqual(scores['trusted'], .20 * (5 / 4.2) * 1.15)

    def test_diversity_and_recent_failure_weights_remain(self):
        _, scores, _ = self.select({'plain': 10000, 'recent': 10000}, memory={
            'recent': {'last_visited_ts': NOW - 10,
                       'recent': [{'outcome': 'no-deal'}] * 4}})
        # .35 recent visit + .25 four memories + .22 each of last three failures.
        self.assertAlmostEqual(scores['recent'], .20 * .5)
        self.assertGreater(scores['plain'], scores['recent'])

    def test_feasibility_and_budget_estimate_unchanged(self):
        # list=10000: budgets above .76, between .65/.76, and below .65.
        for budget, pay, mult in [(9000, 8000, 1), (7000, 7000, .45),
                                   (6000, 6000, .15)]:
            with self.subTest(budget=budget):
                _, scores, _ = self.select({'sku': 10000}, budget=budget)
                self.assertAlmostEqual(scores['sku'], (10000-pay)/10000 * mult * (3.8/4.2))

    def test_missing_zero_price_fallback_boundary_and_invalid_prices(self):
        for price in (None, 0):
            with self.subTest(price=price):
                result, scores, _ = self.select({'sku': price}, budget=10000)
                self.assertEqual(result[1]['list_price'], 10000)  # existing goods fallback
                self.assertAlmostEqual(scores['sku'], .20 * (3.8/4.2))
                result, scores, _ = self.select({'sku': price}, kind='food')
                self.assertIsNone(result[0])
                self.assertEqual(scores, {})
        for price in (-1, 'bad', float('inf'), float('nan'), True, False, .5):
            with self.subTest(price=price):
                result, scores, probes = self.select({'sku': price})
                self.assertIsNone(result[0])
                self.assertEqual(scores, {})
                self.assertEqual(probes, 0)
        result, scores, _ = self.select({'sku': None}, budget=0)
        self.assertIsNone(result[0])
        self.assertEqual(scores, {})

    def test_acceptable_and_stockout_filters_remain(self):
        hub = {'adaptive_strategy': {'buyer_failures': [{
            'order': 'old', 'url': 'https://blocked.invalid', 'sku': 'blocked',
            'reason': 'stockout', 'ts': NOW - 10}], 'buyer_cooldowns': []}}
        result, scores, probes = self.select(
            {'outside': 100000, 'blocked': 10000, 'allowed': 10000},
            acceptable=['blocked', 'allowed'], hub=hub, settles={'blocked': [6500]*3})
        self.assertNotIn('outside', scores)
        self.assertEqual(result[1]['sku'], 'allowed')
        self.assertEqual(probes, 1)


if __name__ == '__main__':
    unittest.main()
