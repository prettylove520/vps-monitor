"""Task validation and public stock inspection, dispatched to merchants/*."""
import json
from decimal import Decimal, InvalidOperation
from bs4 import BeautifulSoup
from network import validate_url, parse_proxy, FetchError
import merchants
from merchants.common import one  # noqa: F401  (kept for compatibility)
from store import db

PRESETS=merchants.PRESETS
COMMON=(('name',100),('url',1500),('max_price',20),('currency',3),('coupon',80),('note',500))

def validate(data):
    if not isinstance(data,dict):
        raise ValueError('无效任务')
    merchant=merchants.get(data.get('adapter'))
    result={'adapter':merchant.NAME}
    for key,size in COMMON:
        val=data.get(key,'')
        if not isinstance(val,str) or len(val)>size:
            raise ValueError('字段长度或类型不正确：'+key)
        result[key]=val.strip()
    if not result['name']:
        raise ValueError('请填写任务名称')
    validate_url(result['url'])
    proxy=data.get('proxy','')
    if not isinstance(proxy,str) or len(proxy)>300:
        raise ValueError('代理地址格式不正确')
    if proxy.strip():
        parse_proxy(proxy.strip())
        result['proxy']=proxy.strip()
    try:
        interval=int(data.get('interval',60))
        if interval<15 or interval>86400:
            raise ValueError()
        if not (result['max_price']=='' and getattr(merchant,'PRICE_OPTIONAL',False)):
            price=Decimal(result['max_price'])
            if not price.is_finite() or price<=0 or price>100000 or price.as_tuple().exponent<-2:
                raise ValueError()
    except (ValueError,TypeError,InvalidOperation):
        raise ValueError('间隔须为 15–86400 秒，价格须为正数且最多两位小数') from None
    if result['currency'] not in ('EUR','CAD','USD','GBP','CNY'):
        raise ValueError('不支持该币种')
    result['interval']=interval
    return merchant.validate(result,data)

def inspect(html,cfg):
    soup=BeautifulSoup(html,'html.parser')
    for tag in soup(['script','style','template']):
        tag.decompose()
    if soup.title and any(x in soup.title.get_text().lower() for x in ('just a moment','attention required','access denied')):
        raise FetchError('网站要求验证，任务已暂停',blocked=True)
    return finish(*merchants.for_config(cfg).inspect(soup,cfg),cfg)

def finish(status,price,detail,cfg):
    """Apply the page price cap; price may be None for merchants that cannot read it."""
    if price is not None:
        if price<=0:
            raise ValueError('价格无效')
        if cfg.get('max_price') and price>Decimal(cfg['max_price']):
            status='price_exceeded'
            detail='页面标价超过设定上限'
    return {'status':status,'price':str(price) if price is not None else None,'currency':cfg['currency'],
            'detail':detail,'coupon':cfg['coupon'],'coupon_verified':False}

def check(cfg,get_html):
    """Run one stock check for a task: the merchant's own fetch, or page download + inspect()."""
    merchant=merchants.for_config(cfg)
    if hasattr(merchant,'check'):
        return finish(*merchant.check(cfg),cfg)
    return inspect(get_html(cfg['url']),cfg)

def migrate():
    """Fill fields added in later versions into stored task configs (revision unchanged)."""
    with db() as c:
        for row in c.execute('SELECT id,config FROM tasks').fetchall():
            cfg=json.loads(row['config'])
            merchant=merchants.REGISTRY.get(cfg.get('adapter'))
            defaults=getattr(merchant,'LEGACY_DEFAULTS',None)
            if defaults and any(k not in cfg for k in defaults):
                c.execute('UPDATE tasks SET config=? WHERE id=?',(json.dumps(defaults|cfg,ensure_ascii=False),row['id']))

migrate()
