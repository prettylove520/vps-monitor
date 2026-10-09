import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from bs4 import BeautifulSoup
from network import validate_url, FetchError

PRESETS = {
 'bero': {'name':'BERO · Anniversary M', 'adapter':'bero', 'url':'https://bero-host.de/server/prepaid-kvm-rootserver-mieten', 'max_price':'49.00', 'currency':'EUR', 'coupon':'DISCORD10', 'interval':15, 'note':'4 核 / 12 GB / 120 GB · 365 天。截图已验证首次优惠价 €44.10，续费 €49。'},
 'vmiss': {'name':'VMISS · TRI Basic', 'adapter':'vmiss', 'url':'https://app.vmiss.com/store/us-los-angeles-tri', 'max_price':'5.00', 'currency':'CAD', 'coupon':'10%OFF', 'interval':300, 'note':'1 核 / 1 GB / 10 GB · 月付。此前曾被网站拒绝访问。'},
 'generic': {'name':'', 'adapter':'generic', 'url':'', 'max_price':'', 'currency':'EUR', 'coupon':'', 'interval':60, 'scope':'', 'identity':'', 'price_selector':'', 'in_stock':'', 'out_stock':'', 'note':''}
}

def validate(data):
    if not isinstance(data, dict):
        raise ValueError('无效任务')
    adapter = data.get('adapter')
    if adapter not in PRESETS:
        raise ValueError('请选择支持的适配方式')
    result = {'adapter':adapter}
    for key, size in [('name',100),('url',1500),('max_price',20),('currency',3),('coupon',80),('note',500),('scope',200),('identity',300),('price_selector',200),('in_stock',100),('out_stock',100)]:
        val = data.get(key,'')
        if not isinstance(val,str) or len(val)>size:
            raise ValueError('字段长度或类型不正确：'+key)
        result[key]=val.strip()
    if not result['name']:
        raise ValueError('请填写任务名称')
    validate_url(result['url'])
    try:
        interval = int(data.get('interval',60))
        price = Decimal(result['max_price'])
        if interval < 15 or interval > 86400 or not price.is_finite() or price<=0 or price>100000 or price.as_tuple().exponent < -2:
            raise ValueError()
    except (ValueError, TypeError, InvalidOperation):
        raise ValueError('间隔须为 15–86400 秒，价格须为正数且最多两位小数') from None
    if result['currency'] not in ('EUR','CAD','USD','GBP','CNY'):
        raise ValueError('不支持该币种')
    result['interval']=interval
    if adapter!='generic':
        preset=PRESETS[adapter]
        if result['url']!=preset['url'] or result['currency']!=preset['currency']:
            raise ValueError('专用适配器的商品地址和币种不能更改，请改用通用规则')
    else:
        if any(not result[k] for k in ('scope','identity','price_selector','in_stock','out_stock')):
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

def one(root, selector):
    nodes=root.select(selector)
    if len(nodes)!=1:
        raise ValueError('匹配元素不是唯一一个：'+selector)
    return nodes[0]

def inspect(html, cfg):
    soup=BeautifulSoup(html,'html.parser')
    for tag in soup(['script','style','template']):
        tag.decompose()
    if soup.title and any(x in soup.title.get_text().lower() for x in ('just a moment','attention required','access denied')):
        raise FetchError('网站要求验证，任务已暂停',blocked=True)
    kind=cfg['adapter']
    if kind=='bero':
        matches=soup.select('[wire\\:click="selectPackage(109)"]')
        # Ignore controls nested inside the card (e.g. an extra "select" button) and accept
        # duplicated cards (e.g. desktop/mobile copies) only if they all say exactly the same.
        cards=[m for m in matches if not any(p in matches for p in m.parents)]
        if not cards:
            raise ValueError('未找到 Anniversary M 套餐卡片')
        readings=set()
        for card in cards:
            # A logged-in BERO account may render in English ("Runtime:", "INCL. VAT.").
            text=re.sub(r'\b(?:Runtime|Laufzeit)\s*:','Laufzeit:',card.get_text(' ',strip=True),flags=re.I)
            expected=('Anniversary M','CPU: 4 Kerne','RAM: 12 GB','NVME: 120 GB','IPv4: 1 Adresse','IPv6: 1 /64 Netz','Backups: 3 Slots','Laufzeit: 365 Tage')
            if any(x not in text for x in expected):
                raise ValueError('套餐配置发生变化')
            panels=card.select('.bero-orange-border')
            sold=bool(re.search(r'\bAusverkauft\b|\bSold out\b',text,re.I))
            if panels:
                faded=any(re.search(r'opacity\s*:\s*0\.22\b',panel.get('style','')) for panel in panels)
                if sold!=faded:
                    raise ValueError('售罄和按钮外观标记不一致')
            prices=set(re.findall(r'(\d+[,.]\d{2})\s*€\s*(?:INKL\. MWST\.|INCL\. VAT\.)',text,re.I))
            if len(prices)!=1:
                raise ValueError('未找到唯一的含税价格')
            readings.add((sold,prices.pop().replace(',','.')))
        if len(readings)!=1:
            raise ValueError('页面上多个 Anniversary M 卡片的库存或价格不一致')
        sold,price_text=readings.pop()
        price=Decimal(price_text)
        status='out_of_stock' if sold else 'available'
        detail='页面售罄标记已消失，结账库存仍需确认' if not sold else '官网显示售罄'
    elif kind=='vmiss':
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
        status='available' if stock else 'out_of_stock'
        detail='页面库存：'+str(stock)
    else:
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
        detail='根据配置的页面文字识别；请在购买前确认套餐与结账总价'
    if price<=0:
        raise ValueError('价格无效')
    if price>Decimal(cfg['max_price']):
        status='price_exceeded'
        detail='页面标价超过设定上限'
    return {'status':status,'price':str(price),'currency':cfg['currency'],'detail':detail,'coupon':cfg['coupon'],'coupon_verified':False}
