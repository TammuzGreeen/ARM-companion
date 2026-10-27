import unittest

from recognition_helper.budget import committed, reservation, usage_charge
from recognition_helper.config import Settings
from recognition_helper.vision import Vision


class BudgetTest(unittest.TestCase):
    def test_documented_astra_model_has_bounded_quote(self):
        quote = reservation('gpt-6-astra', 5000, 2)
        self.assertGreater(quote['reserved_usd'], .1)
        self.assertLess(quote['reserved_usd'], 1)
        self.assertEqual(quote['output_token_bound'], 2048)
        self.assertGreater(quote['input_token_bound'], 5000)

    def test_unknown_model_does_not_invent_pricing(self):
        with self.assertRaisesRegex(ValueError, 'pricing'):
            reservation('unverified-model', 1000, 2)

    def test_lost_response_retains_entire_reservation(self):
        quote = reservation('gpt-6-astra', 5000, 2)
        self.assertEqual(usage_charge(None, quote), quote['reserved_usd'])

    def test_settled_usage_replaces_reservation_conservatively(self):
        quote = reservation('gpt-6-astra', 5000, 2)
        charge = usage_charge({'prompt_tokens': 3000, 'completion_tokens': 500}, quote)
        self.assertLess(charge, quote['reserved_usd'])
        self.assertEqual(committed([{'fallback_budget': quote}, {'fallback_cost_usd': charge}]),
                         round(quote['reserved_usd'] + charge, 6))

    def test_fallback_context_is_bounded_without_discarding_outputs(self):
        vision = Vision(Settings())
        with self.assertRaisesRegex(ValueError, 'context exceeds'):
            vision.quote({'unbounded_text': 'x' * 40000})

    def test_external_quote_includes_schema_and_prompt(self):
        quote = Vision(Settings()).quote({'source_count': 4})
        self.assertGreater(quote['input_token_bound'], 6000)
