"""VMISS US.LA.TRI.Basic (WHMCS store page). Stock monitor only."""
import re
from decimal import Decimal
from merchants.common import one

NAME='vmiss'
LABEL='VMISS'
SUPPORTS_CHECKOUT=False
URL='https://app.vmiss.com/store/us-los-angeles-tri'
PRESETS={
 'vmiss':{'name':'VMISS · TRI Basic','adapter':'vmiss','url':URL,'max_price':'5.00','currency':'CAD',
          'coupon':'10%OFF','interval':300,'note':'1 核 / 1 GB / 10 GB · 月付。此前曾被网站拒绝访问。'},
}

def validate(result,data):
    if result['url']!=URL or result['currency']!='CAD':
        raise ValueError('专用适配器的商品地址和币种不能更改，请改用通用规则')
    return result

def inspect(soup,cfg):
    card=one(soup,'#product32')
    expected='1 Core 1024 MB 10GB SSD 200Mbps Port 500GB Bandwidth 1 IPv4 Included'
    if one(card,'.package-title').get_text(' ',strip=True)!='US.LA.TRI.Basic' or one(card,'.package-content').get_text(' ',strip=True)!=expected:
        raise ValueError('套餐名称或配置不匹配')
    prices=card.select('.price-amount')
    matches=[re.fullmatch(r'\$\s*(\d+\.\d{2})\s+CAD',p.get_text(' ',strip=True)) for p in prices]
    if not matches or not all(matches) or len({m[1] for m in matches})!=1:
        raise ValueError('价格或币种不匹配')
    price=Decimal(matches[0][1])
    cycles=card.select('.price-cycle')
    if not cycles or any(c.get_text(strip=True).lower() not in ('monthly','每月') for c in cycles):
        raise ValueError('付款周期不是月付')
    qty=re.fullmatch(r'(\d+)\s*(?:可用|Available)',one(card,'.package-qty').get_text(' ',strip=True),re.I)
    button=one(card,'#product32-order-button')
    if not qty:
        raise ValueError('库存数量无法识别')
    disabled='disabled' in button.get('class',[]) or button.has_attr('disabled') or button.get('aria-disabled')=='true'
    stock=int(qty[1])
    if (stock>0)==disabled:
        raise ValueError('库存数量与按钮状态不一致')
    return ('available' if stock else 'out_of_stock'),price,'页面库存：'+str(stock)
