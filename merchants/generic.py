"""Generic rule for static HTTPS product pages: CSS scope, identity text, price, stock texts."""
import re
from decimal import Decimal
from merchants.common import one

NAME='generic'
LABEL='通用 HTML 规则'
SUPPORTS_CHECKOUT=False
PRESETS={
 'generic':{'name':'','adapter':'generic','url':'','max_price':'','currency':'EUR','coupon':'','interval':60,
            'scope':'','identity':'','price_selector':'','in_stock':'','out_stock':'','note':''},
}
FIELDS=(('scope',200),('identity',300),('price_selector',200),('in_stock',100),('out_stock',100))

def validate(result,data):
    for key,size in FIELDS:
        val=data.get(key,'')
        if not isinstance(val,str) or len(val)>size:
            raise ValueError('字段长度或类型不正确：'+key)
        result[key]=val.strip()
    if any(not result[k] for k,_ in FIELDS):
        raise ValueError('请完整填写商品范围、身份文字、价格和库存规则')
    if result['in_stock'].casefold()==result['out_stock'].casefold():
        raise ValueError('有货与缺货文字不能相同')
    import soupsieve
    try:
        soupsieve.compile(result['scope'])
        soupsieve.compile(result['price_selector'])
    except Exception:
        raise ValueError('CSS 选择器格式错误') from None
    return result

def inspect(soup,cfg):
    card=one(soup,cfg['scope'])
    text=card.get_text(' ',strip=True)
    if cfg['identity'] not in text:
        raise ValueError('商品身份文字不匹配')
    price_text=one(card,cfg['price_selector']).get_text(' ',strip=True)
    symbols={'EUR':'€','CAD':'CAD','USD':'USD','GBP':'£','CNY':'CNY'}
    if cfg['currency'] not in price_text and symbols[cfg['currency']] not in price_text:
        raise ValueError('价格文字中没有明确币种')
    prices=re.findall(r'(?<![\d.,])\d+(?:[.,]\d{1,2})?(?![\d.,])',price_text)
    if len(prices)!=1:
        raise ValueError('价格含多个数字或使用不支持的千分位格式')
    price=Decimal(prices[0].replace(',','.'))
    sold=cfg['out_stock'].casefold() in text.casefold()
    available=cfg['in_stock'].casefold() in text.casefold()
    # Out-of-stock takes precedence (e.g. "in stock" inside "not in stock").
    if sold:
        status='out_of_stock'
    elif available:
        status='available'
    else:
        raise ValueError('页面中未找到明确的库存文字')
    return status,price,'根据配置的页面文字识别；请在购买前确认套餐与结账总价'
