"""Small persistent-request budget calculations, independent of recognition policy."""
from decimal import Decimal, ROUND_CEILING
import json
import os
from pathlib import Path


PRICING = json.loads(Path(os.getenv('VISION_PRICING_FILE') or Path(__file__).with_name('vision_pricing.json')).read_text())
OUTPUT_TOKEN_LIMIT = 2048
CONTEXT_BYTE_LIMIT = 32768


def money(value):
    return float(Decimal(str(value)).quantize(Decimal('0.000001'), rounding=ROUND_CEILING))


def reservation(model, text_bytes, image_count, *, input_rate=None, output_rate=None):
    rates = PRICING['models'].get(model)
    if rates is None and (input_rate is None or output_rate is None):
        raise ValueError('Provider/model pricing has not been verified or configured')
    rates = rates or {'input_usd_per_million': input_rate, 'output_usd_per_million': output_rate,
                      'cache_write_multiplier': 1.25, 'high_detail_max_patches': 2500,
                      'image_patch_multiplier': 1.2}
    # High detail has a documented patch ceiling. Byte count conservatively bounds
    # ordinary BPE text tokens; allowance covers role/schema framing and rounding.
    image_bound = int(image_count * (rates['high_detail_max_patches'] * rates['image_patch_multiplier'] + 2))
    input_bound = text_bytes + image_bound + 1024
    if input_bound >= 272000:
        raise ValueError('Fallback input exceeded the bounded standard-price request profile')
    amount = (Decimal(input_bound) * Decimal(str(rates['input_usd_per_million'])) *
              Decimal(str(rates.get('cache_write_multiplier', 1.25))) +
              Decimal(OUTPUT_TOKEN_LIMIT) * Decimal(str(rates['output_usd_per_million']))) / Decimal(1000000)
    return {'reserved_usd': money(amount), 'input_token_bound': input_bound,
            'output_token_bound': OUTPUT_TOKEN_LIMIT, 'rates': rates,
            'pricing_verified_at': PRICING['verified_at'], 'pricing_sources': PRICING['sources']}


def usage_charge(usage, reserved):
    if not usage or 'prompt_tokens' not in usage or 'completion_tokens' not in usage:
        # Timeout/lost response may still have been billed. Retain the reservation.
        return reserved['reserved_usd']
    rates = reserved['rates']
    amount = (Decimal(usage['prompt_tokens']) * Decimal(str(rates['input_usd_per_million'])) *
              Decimal(str(rates.get('cache_write_multiplier', 1.25))) +
              Decimal(usage['completion_tokens']) * Decimal(str(rates['output_usd_per_million']))) / Decimal(1000000)
    return money(amount)


def committed(cycles):
    return money(sum(Decimal(str(c.get('fallback_cost_usd', c.get('fallback_budget', {}).get('reserved_usd', 0))))
                     for c in cycles))
