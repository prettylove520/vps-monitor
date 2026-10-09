"""Generic WHMCS stock monitor (most small VPS hosts run WHMCS).

How it works: request `cart.php?a=add&pid=<id>` without following redirects, exactly what the
"Order Now" button does.
  * In stock     -> WHMCS adds the product to a new cart and redirects to `cart.php?a=confproduct`.
  * Out of stock -> WHMCS stays on the page and shows its "Out of Stock" notice
                    (language key `outofstock` / `outofstockdescription`).
  * Unknown/hidden product -> WHMCS redirects back to the cart or store, reported as an error.
This does not depend on the theme, and needs no login or cookies. Monitor only: no checkout.
"""
import re
from urllib.parse import urlsplit, parse_qs, urlencode, urlunsplit
from bs4 import BeautifulSoup
from network import fetch, validate_url

NAME='whmcs'
LABEL='WHMCS 商家（通用）'
SUPPORTS_CHECKOUT=False
PRICE_OPTIONAL=True
PRESETS={
 'whmcs':{'name':'','adapter':'whmcs','url':'','pid':'','max_price':'','currency':'USD','coupon':'','interval':60,
          'note':''},
}
# WHMCS core language strings for out-of-stock, plus common translations used by language packs.
OUT_OF_STOCK=('out of stock','currently out of stock','缺货','售罄','无库存','已售完','庫存不足','缺貨','ausverkauft',
              'nicht vorrätig','nicht auf lager','rupture de stock','agotado','esaurito','нет в наличии')
CONFIGURE_MARKERS=('frmConfigureProduct','confproduct','id="productConfigurationForm"')

def parse_link(url):
    """Accept the shop's order link (…/cart.php?a=add&pid=123) or the WHMCS base URL."""
    p=urlsplit(url)
    query=parse_qs(p.query)
    pid=None
    if p.path.endswith('cart.php') and query.get('pid'):
        pid=query['pid'][0]
    base=p.path[:-len('cart.php')] if p.path.endswith('cart.php') else (p.path if p.path.endswith('/') else p.path.rsplit('/',1)[0]+'/')
    return urlunsplit(('https',p.netloc,base or '/','','')),pid

def order_url(cfg):
    base,_=parse_link(cfg['url'])
    p=urlsplit(base)
    return urlunsplit(('https',p.netloc,p.path+'cart.php',urlencode({'a':'add','pid':cfg['pid']}),''))

def validate(result,data):
    base,link_pid=parse_link(result['url'])
    pid=str(data.get('pid') or link_pid or '').strip()
    if not re.fullmatch(r'\d{1,9}',pid):
        raise ValueError('请填写商品编号 pid（下单链接 cart.php?a=add&pid=编号 里的数字），或直接粘贴下单链接')
    if link_pid and link_pid!=pid:
        raise ValueError('商品编号与下单链接中的 pid 不一致')
    result['url']=base
    result['pid']=pid
    validate_url(order_url(result))
    return result

def classify(page):
    """Return (status, detail) from a non-following fetch of cart.php?a=add&pid=N."""
    if page.status!=200:
        target=urlsplit(page.location)
        query=parse_qs(target.query)
        if target.path.endswith('cart.php') and query.get('a',[''])[0]=='confproduct':
            return 'available','下单链接已进入配置页（WHMCS 判定有库存）'
        if 'login' in target.path or query.get('a',[''])[0]=='login':
            raise ValueError('该商品需要登录才能下单，无法公开监控')
        raise ValueError('商品编号无效、已下架或被隐藏（跳转到 %s）'%(target.path or '/'))
    soup=BeautifulSoup(page.html,'html.parser')
    for tag in soup(['script','style','template','noscript']):
        tag.decompose()
    text=soup.get_text(' ',strip=True).casefold()
    sold=any(word in text for word in OUT_OF_STOCK)
    configure=any(marker in page.html for marker in CONFIGURE_MARKERS)
    if sold and configure:
        raise ValueError('WHMCS 页面同时出现缺货提示和配置表单，无法判断')
    if sold:
        return 'out_of_stock','WHMCS 显示缺货'
    if configure:
        return 'available','页面显示商品配置表单（WHMCS 判定有库存）'
    raise ValueError('未识别的 WHMCS 页面：既没有缺货提示，也没有进入配置页')

def check(cfg):
    """Fetch and classify. Returns (status, price or None, detail)."""
    page=fetch(order_url(cfg),follow=False)
    if page.status==200:
        soup=BeautifulSoup(page.html,'html.parser')
        if soup.title and any(x in soup.title.get_text().lower() for x in ('just a moment','attention required','access denied')):
            from network import FetchError
            raise FetchError('网站要求验证，任务已暂停',blocked=True)
    status,detail=classify(page)
    return status,None,detail

def inspect(soup,cfg):
    raise ValueError('WHMCS 任务通过下单链接检查，不解析商品页')
