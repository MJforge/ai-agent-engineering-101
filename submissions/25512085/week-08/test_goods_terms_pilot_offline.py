"""Three-SKU terms pilot: synthetic inventory, temporary signing key, no market I/O."""
import copy
import unittest
from unittest.mock import patch

import seller
import test_seller_food_offline as fixture


class GoodsTermsPilotTests(unittest.TestCase):
    setUp = fixture.FoodSafetyTests.setUp
    turn = fixture.FoodSafetyTests.turn
    receipt = fixture.FoodSafetyTests.receipt
    SKUS = ('G021aae64', 'G0be9ed07', 'G81ade83e')

    def stock(self, sku='G021aae64', grades=('A', 'B', 'C')):
        self.agent = seller.Seller(0)
        self.catalog[sku] = dict(kind='goods', category='goods>sofa', list_price=100000)
        self.inventory['units'] = [dict(
            sku=sku, unit=sku+'-'+g, name='sofa', category='goods>sofa', kind='goods',
            grade=g, cost=30000, list_price=100000, supplied_at=self.now-3600,
            cert={'proof': 'offline'}) for g in grades]
        self.stack.enter_context(patch.object(seller, 'queue_inbound_inquiry', lambda *a, **kw: None))
        return sku

    def open_deal(self, text):
        events = self.turn(text)
        self.assertEqual([k for k, _ in events], ['input'])
        return self.agent.open['task-1'], str(events)

    def test_three_skus_unconstrained_prefer_C0(self):
        for sku in self.SKUS:
            with self.subTest(sku=sku):
                self.stock(sku)
                deal, _ = self.open_deal(sku)
                self.assertEqual((deal['grade'], deal['warranty_hours']), ('C', 0))

    def test_A_only_new_quote_is_A0_and_mentions_warrantied_option(self):
        for sku in self.SKUS:
            with self.subTest(sku=sku):
                self.stock(sku)
                deal, text = self.open_deal(sku+' grade A')
                self.assertEqual((deal['grade'], deal['warranty_hours'], deal['break_rate']), ('A', 0, 0))
                self.assertIn('warranty_hours=0', text)
                self.assertIn('72h', text)
                self.assertNotIn('Grade C refurbished', text)

    def test_A_question_is_not_permission_to_sell_C(self):
        sku = self.stock()
        deal, _ = self.open_deal(sku+' A인가요?')
        self.assertEqual((deal['grade'], deal['warranty_hours']), ('A', 0))

    def test_explicit_A72_preserved(self):
        sku = self.stock()
        deal, _ = self.open_deal(sku+' grade=A warranty_hours=72')
        self.assertEqual((deal['grade'], deal['warranty_hours']), ('A', 72))

    def test_B_minimum_not_C_even_when_cheap(self):
        for text in ('B급 이상 저렴', 'B이상 저렴', 'B 이상 저렴', 'minimum grade B cheap', 'grade>=B cheap'):
            with self.subTest(text=text):
                sku = self.stock(grades=('B', 'C'))
                deal, _ = self.open_deal(sku+' '+text)
                self.assertEqual((deal['grade'], deal['warranty_hours']), ('B', 6))

    def test_explicit_C0_exact_selection(self):
        sku = self.stock()
        deal, _ = self.open_deal(sku+' grade=C warranty_hours=0')
        self.assertEqual((deal['grade'], deal['warranty_hours']), ('C', 0))

    def test_six_hour_minimum_uses_warrantied_stock(self):
        sku = self.stock()
        deal, _ = self.open_deal(sku+' warranty 6h')
        self.assertGreaterEqual(deal['warranty_hours'], 6)
        self.assertNotEqual(deal['grade'], 'C')

    def test_C_missing_uses_existing_A_fallback(self):
        sku = self.stock(grades=('A', 'B'))
        deal, _ = self.open_deal(sku)
        self.assertEqual((deal['grade'], deal['warranty_hours']), ('A', 72))

    def test_ambiguous_quality_refund_and_cert_keep_conservative_terms(self):
        for text in ('품질 중요', '환불 가능?', 'good quality', 'cert=true', 'after-sales service', '고장 적은 것', 'A급 품질 보증 필요'):
            with self.subTest(text=text):
                sku = self.stock()
                deal, _ = self.open_deal(sku+' '+text)
                self.assertEqual((deal['grade'], deal['warranty_hours']), ('A', 72))

    def test_other_sku_default_and_A_remain_A72(self):
        for text in ('', ' grade A'):
            sku = self.stock('G00000001')
            deal, _ = self.open_deal(sku+text)
            self.assertEqual((deal['grade'], deal['warranty_hours']), ('A', 72))

    def test_invalid_or_conflicting_conditions_never_sign(self):
        for text in ('grade=D', 'warranty_hours=-1', 'warranty_hours=nan',
                     'warranty_hours=73', 'grade=C warranty 6h', 'grade=A grade=C',
                     'cert=false', 'grade=A warranty_hours=0 warranty 6h', 'grades=[B,C]',
                     'grade=unknown', 'not grade A'):
            with self.subTest(text=text):
                sku = self.stock()
                events = self.turn(sku+' '+text+' offer 999999')
                self.assertIn('reject', [k for k, _ in events])
                self.assertNotIn('receipt', [k for k, _ in events])

    def test_A0_numeric_acceptance_receipt_and_completion_match(self):
        sku = self.stock()
        deal, _ = self.open_deal(sku+' grade A')
        events = self.turn(str(deal['ask']))
        receipt = self.receipt(events)
        self.assertEqual((receipt['unit'], receipt['claim'], receipt['warranty_hours'], receipt['price']),
                         (deal['unit']['unit'], 'A', 0, deal['ask']))
        self.assertIn('warranty=0h', str(events))
        self.assertEqual(receipt['refund_policy'], 'no refund if the item breaks')

    def test_A0_is_disclosed_before_initial_price_can_close(self):
        sku = self.stock()
        deal, text = self.open_deal(sku+' grade A offer 999999')
        self.assertEqual(deal['warranty_hours'], 0)
        self.assertIn('warranty_hours=0', text)
        self.assertFalse(self.agent.committed_units)

    def test_explicit_C0_instant_receipt_matches(self):
        sku = self.stock()
        receipt = self.receipt(self.turn(sku+' grade=C warranty_hours=0 offer 999999'))
        self.assertEqual((receipt['claim'], receipt['warranty_hours']), ('C', 0))

    def test_existing_A72_promise_numeric_reply_never_downgrades(self):
        sku = self.stock()
        deal, _ = self.open_deal(sku+' grade A warranty 72h')
        deal.pop('pilot_A0_option', None)  # Simulate a pre-pilot in-memory promise.
        receipt = self.receipt(self.turn(str(deal['ask'])))
        self.assertEqual(receipt['warranty_hours'], 72)

    def test_later_minimum_warranty_without_price_cannot_be_ignored(self):
        sku = self.stock()
        self.open_deal(sku+' grade A')
        events = self.turn('warranty 6h')
        self.assertIn('reject', [k for k, _ in events])
        self.assertFalse(self.agent.open)

    def test_later_conflicting_grade_with_offer_never_signs(self):
        sku = self.stock()
        self.open_deal(sku)
        events = self.turn('grade=A offer 999999')
        self.assertIn('reject', [k for k, _ in events])
        self.assertNotIn('receipt', [k for k, _ in events])

    def test_A72_option_requires_explicit_new_quote_then_numeric_acceptance(self):
        sku = self.stock()
        self.open_deal(sku+' grade A')
        events = self.turn('choose 72h warranty')
        self.assertEqual([k for k, _ in events], ['input'])
        deal = self.agent.open['task-1']
        self.assertEqual(deal['warranty_hours'], 72)
        self.assertIn('72h', str(events))
        receipt = self.receipt(self.turn(str(deal['ask'])))
        self.assertEqual(receipt['warranty_hours'], 72)

    def test_A0_no_discount_old_floor_and_actual_zero_refund_risk(self):
        sku = self.stock()
        unit = self.inventory['units'][0]
        standard = seller.compute_unit_pricing_and_terms(unit, {}, now=self.now, catalog=self.catalog, clearance=1)
        deal, _ = self.open_deal(sku+' grade A')
        self.assertEqual(deal['ask'], standard['open_ask'])
        self.assertGreaterEqual(deal['hard_floor'], standard['hard_floor'])
        self.assertEqual(deal['break_rate'], 0)
        self.assertGreaterEqual(deal['hard_floor'], seller.cash_policy.profit_floor(
            unit, {}, self.catalog, self.now, warranty=0))

    def test_A0_does_not_relabel_A72_comparable_as_A0(self):
        sku = self.stock()
        hub = {'demand_radar': {'sku_comparables': {sku: [dict(
            grade='A', amount=77777, warranty_hours=72, ts=self.now-i) for i in range(5)]}}}
        with patch.object(seller, 'load_hub', return_value=hub):
            deal, _ = self.open_deal(sku+' grade A')
        self.assertFalse(deal['cash_first_quote'])
        self.assertEqual(deal['break_rate'], 0)

    def test_A_wording_variants_never_sell_C(self):
        for text in ('A only', 'only A', 'A-grade', 'A급만', '등급 A'):
            with self.subTest(text=text):
                sku = self.stock()
                deal, _ = self.open_deal(sku+' '+text)
                self.assertEqual((deal['grade'], deal['warranty_hours']), ('A', 0))

    def test_ambiguous_refund_with_only_C_needs_new_inquiry(self):
        sku = self.stock(grades=('C',))
        events = self.turn(sku+' 환불 가능? offer 999999')
        self.assertIn('reject', [k for k, _ in events])
        self.assertNotIn('receipt', [k for k, _ in events])

    def test_override_is_not_a_global_warranty_switch(self):
        sku = self.stock()
        for value in (True, -1, 6, 72, '0', float('nan')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                seller.compute_unit_pricing_and_terms(self.inventory['units'][0], {}, now=self.now,
                    catalog=self.catalog, clearance=1, warranty_override=value)
        unit = dict(self.inventory['units'][0], sku='G00000001')
        with self.assertRaises(ValueError):
            seller.compute_unit_pricing_and_terms(unit, {}, now=self.now, catalog=self.catalog,
                                                 clearance=1, warranty_override=0)

    def test_A0_concession_receipt_same_conditions_and_no_floor_bypass(self):
        sku = self.stock()
        deal, _ = self.open_deal(sku+' grade A')
        events = self.turn('offer 1')
        self.assertIn('warranty_hours=0', str(events))
        self.assertGreaterEqual(deal['ask'], deal['hard_floor'])
        receipt = self.receipt(self.turn(str(deal['ask'])))
        self.assertEqual((receipt['price'], receipt['warranty_hours']), (deal['ask'], 0))

    def test_explicit_B6_and_no_matching_A_are_not_substituted(self):
        sku = self.stock()
        deal, _ = self.open_deal(sku+' grade=B warranty_hours=6')
        self.assertEqual((deal['grade'], deal['warranty_hours']), ('B', 6))
        sku = self.stock(grades=('B', 'C'))
        events = self.turn(sku+' grade=A offer 999999')
        self.assertIn('reject', [k for k, _ in events])
        self.assertNotIn('receipt', [k for k, _ in events])

    def test_numeric_72_is_not_A72_option_consent(self):
        sku = self.stock()
        deal, _ = self.open_deal(sku+' grade A')
        self.turn('72')
        self.assertEqual(deal['warranty_hours'], 0)

    def test_negated_option_choice_never_changes_terms(self):
        sku = self.stock()
        deal, _ = self.open_deal(sku+' grade A')
        events = self.turn('do not choose 72h warranty')
        self.assertIn('reject', [k for k, _ in events])
        self.assertEqual(deal['warranty_hours'], 0)

    def test_natural_zero_days_and_invalid_warranty_durations_are_not_ignored(self):
        for text in ('warranty 6 days', '6일 보증', 'warranty -1h', 'warranty nan h'):
            with self.subTest(text=text):
                sku = self.stock()
                events = self.turn(sku+' '+text+' offer 999999')
                self.assertIn('reject', [k for k, _ in events])
                self.assertNotIn('receipt', [k for k, _ in events])
        for text in ('grade=C warranty 0h', 'grade=C 0h warranty'):
            with self.subTest(text=text):
                sku = self.stock()
                deal, _ = self.open_deal(sku+' '+text)
                self.assertEqual((deal['grade'], deal['warranty_hours']), ('C', 0))

    def test_review_grade_rejection_never_issues_initial_receipt(self):
        for text in ('grade C is not acceptable', 'C급 말고', 'C급은 안 돼요',
                     'grade C is unacceptable', 'I do not want grade C'):
            with self.subTest(text=text):
                sku = self.stock()
                events = self.turn(sku+' '+text+' offer 999999')
                self.assertEqual([k for k, _ in events], ['reject'])
                self.assertIn('clarify', str(events))
                self.assertIn('No receipt issued', str(events))
                self.assertFalse(self.agent.open)
                self.assertFalse(self.agent.committed_units)

    def test_review_grade_rejection_never_issues_later_receipt(self):
        for text in ('grade C is not acceptable', 'C급 말고', 'C급은 안 돼요'):
            with self.subTest(text=text):
                sku = self.stock()
                self.open_deal(sku)
                events = self.turn(text+' offer 999999')
                self.assertEqual([k for k, _ in events], ['reject'])
                self.assertFalse(self.agent.committed_units)

    def test_review_minimum_warranty_cannot_close_B6_or_C0_initially(self):
        for text in ('보증 최소 72시간', 'warranty of at least 72h', 'warranty covering at least 72h'):
            with self.subTest(text=text):
                sku = self.stock(grades=('B', 'C'))
                events = self.turn(sku+' '+text+' offer 999999')
                self.assertEqual([k for k, _ in events], ['reject'])
                self.assertFalse(self.agent.committed_units)

    def test_review_minimum_warranty_checked_after_B6_quote_without_price_too(self):
        for text in ('보증 최소 72시간', 'warranty of at least 72h', 'warranty covering at least 72h'):
            for offer in ('', ' offer 999999'):
                with self.subTest(text=text, offer=offer):
                    sku = self.stock(grades=('B', 'C'))
                    deal, _ = self.open_deal(sku+' grade=B warranty_hours=6')
                    events = self.turn(text+offer)
                    self.assertEqual([k for k, _ in events], ['reject'])
                    self.assertEqual(deal['warranty_hours'], 6)
                    self.assertFalse(self.agent.open)
                    self.assertFalse(self.agent.committed_units)

    def test_review_minimum_warranty_fulfilled_by_actual_A72(self):
        for text in ('보증 최소 72시간', 'warranty of at least 72h'):
            with self.subTest(text=text):
                sku = self.stock()
                receipt = self.receipt(self.turn(sku+' '+text+' offer 999999'))
                self.assertEqual((receipt['claim'], receipt['warranty_hours']), ('A', 72))

    def test_review_negated_and_unclear_options_preserve_entire_A0_quote(self):
        for text in ('72h 보증 선택 안 함', '72h 보증 선택 안해요', '72h 보증 선택하지 않습니다',
                     '72h 보증 미선택', '72h 보증 선택 여부 미정', '72h 보증 선택할까요?',
                     'do not choose 72h warranty', 'I refuse to select 72h warranty',
                     'should I choose 72h warranty?', 'I might choose 72h warranty',
                     'choose 72h warranty?'):
            with self.subTest(text=text):
                sku = self.stock()
                deal, _ = self.open_deal(sku+' grade A')
                before = copy.deepcopy(deal)
                events = self.turn(text)
                self.assertEqual([k for k, _ in events], ['input'])
                self.assertIn('task-1', self.agent.open)
                self.assertEqual(self.agent.open['task-1'], before)
                self.assertFalse(self.agent.committed_units)

    def test_review_explicit_positive_options_requote_without_signing(self):
        for text in ('choose 72h warranty', 'select 72h warranty', 'I choose 72h warranty',
                     '72h 보증 선택', '72h 보증 선택합니다'):
            with self.subTest(text=text):
                sku = self.stock()
                self.open_deal(sku+' grade A')
                events = self.turn(text)
                self.assertEqual([k for k, _ in events], ['input'])
                self.assertEqual(self.agent.open['task-1']['warranty_hours'], 72)
                self.assertFalse(self.agent.committed_units)


if __name__ == '__main__':
    unittest.main(verbosity=2)
