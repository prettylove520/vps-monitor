"""Stock from a public JSON endpoint, for shops whose product page is rendered by JavaScript.

The task URL is the API address the page itself loads (visible in the browser's Network tab). One entry of a list
in the response is picked by a field value, e.g. items[id == 13]; it is in stock when its stock field is above 0.
"""
import json
from decimal import Decimal, InvalidOperation
from network import fetch, validate_url, FetchError

NAME='jsonapi'
LABEL='JSON 接口'
SUPPORTS_CHECKOUT=False
PRICE_OPTIONAL=True
PRESETS={
 'jsonapi':{'name':'ESTKme Plus+','adapter':'jsonapi','url':'https://api.estk.me/user/shop/products/1',
            'page_url':'https://store.estk.me/products/1','list_path':'data.product.items',
            'match_field':'id','match_value':'13','stock_field':'stock','price_field':'price',
            'max_price':'200.00','currency':'CNY','coupon':'','interval':60,
            'note':'读取商店的公开库存接口；币种请以商店页面为准。'},
}
FIELDS=(('list_path',120),('match_field',60),('match_value',100),('stock_field',60),('price_field',60),('page_url',1500))

def validate(result,data):
    for key,size in FIELDS:
        val=data.get(key,'')
        if not isinstance(val,str) or len(val)>size:
            raise ValueError('字段长度或类型不正确：'+key)
        result[key]=val.strip()
    for key in ('list_path','match_field','match_value','stock_field'):
        if not result[key]:
            raise ValueError('请填写列表路径、匹配字段、匹配值和库存字段')
    if result['page_url']:
        validate_url(result['page_url'])
    else:
        del result['page_url']
    if not result['price_field']:
        del result['price_field']
    return result

def order_url(cfg):
    """The link people open from a notification: the shop page, not the API address."""
    return cfg.get('page_url') or cfg['url']

def dig(value,path):
    for part in path.split('.'):
        if isinstance(value,list):
            if not part.isdigit() or int(part)>=len(value):
                raise ValueError('接口返回中找不到路径：'+path)
            value=value[int(part)]
        elif isinstance(value,dict) and part in value:
            value=value[part]
        else:
            raise ValueError('接口返回中找不到路径：'+path)
    return value

def number(value,name):
    if isinstance(value,bool) or not isinstance(value,(int,float,str)):
        raise ValueError('字段 %s 不是数字'%name)
    try:
        number=Decimal(str(value).strip())
    except InvalidOperation:
        raise ValueError('字段 %s 不是数字'%name) from None
    if not number.is_finite():
        raise ValueError('字段 %s 不是数字'%name)
    return number

def classify(data,cfg):
    items=dig(data,cfg['list_path'])
    if not isinstance(items,list):
        raise ValueError('列表路径指向的不是列表：'+cfg['list_path'])
    found=[x for x in items if isinstance(x,dict) and str(x.get(cfg['match_field']))==cfg['match_value']]
    if len(found)!=1:
        raise ValueError('接口里没有找到唯一匹配的商品（%s = %s）'%(cfg['match_field'],cfg['match_value']))
    item=found[0]
    if cfg['stock_field'] not in item:
        raise ValueError('商品里没有库存字段：'+cfg['stock_field'])
    stock=number(item[cfg['stock_field']],cfg['stock_field'])
    price=number(item[cfg['price_field']],cfg['price_field']) if cfg.get('price_field') and cfg['price_field'] in item else None
    title=item.get('title') or item.get('name') or cfg['match_value']
    if stock>0:
        return 'available',price,'接口显示「%s」库存 %s'%(title,stock.normalize())
    return 'out_of_stock',price,'接口显示「%s」库存为 0'%title

def check(cfg):
    page=fetch(cfg['url'],kind='json')
    try:
        data=json.loads(page.html)
    except ValueError:
        raise FetchError('接口返回的不是有效 JSON') from None
    if isinstance(data,dict) and data.get('code') not in (None,0,200):
        raise ValueError('接口返回错误：%s'%str(data.get('message',data.get('code')))[:100])
    return classify(data,cfg)

def inspect(soup,cfg):
    raise ValueError('JSON 接口任务通过接口检查，不解析网页')
