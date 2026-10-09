"""Merchant adapters.

Each adapter module provides:
  NAME, LABEL, PRESETS (task templates), SUPPORTS_CHECKOUT
  validate(result, data) -> result      merchant-specific task fields
  inspect(soup, cfg) -> (status, Decimal price, detail)   public stock check
Adapters with SUPPORTS_CHECKOUT also provide:
  MERCHANT, HOST, ORDERS_URL, CURRENCY, ERRORS, LEGACY_DEFAULTS
  account_ready(), session() (context manager yielding a logged-in client)
  preflight(client, cfg, policy) -> report      dry run, never orders
  purchase(client, cfg, policy, task_id) -> report   armed one-time order

To add a merchant: create merchants/<name>.py and register it below.
"""
from merchants import bero, generic, vmiss

REGISTRY={m.NAME:m for m in (bero,vmiss,generic)}
PRESETS={key:preset for m in REGISTRY.values() for key,preset in m.PRESETS.items()}

def get(name):
    try:
        return REGISTRY[name]
    except KeyError:
        raise ValueError('请选择支持的适配方式') from None

def for_config(cfg):
    return get(cfg['adapter'])

def checkout_merchant(cfg):
    merchant=for_config(cfg)
    if not getattr(merchant,'SUPPORTS_CHECKOUT',False):
        raise ValueError('%s 暂不支持自动结账'%merchant.LABEL)
    return merchant
