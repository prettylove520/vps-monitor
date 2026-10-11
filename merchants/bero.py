"""BERO HOST (bero-host.de): stock monitor, checkout preflight and armed one-time purchase.

Verified live order flow (2026-10-09, logged-in account with prepaid balance):
  500 select package -> 1000 configuration -> 1100 overview (+coupon) -> NEXT jumpToStep(1400)
  -> 1400 CHECK page: checkbox wire:click="toggleAgbConfirmation" and
     <form wire:submit="jumpToStep(1500)"> "ORDER NOW" / "JETZT BESTELLEN".
BERO is prepaid: there is no payment-method step for a logged-in account; the order is paid
from the account balance (Guthaben). LOGIN (1200) / PAYMENT (1300) steps mean we are not in
that state and the executor stops.

Everything that identifies *what* to buy (store page, package id, name, specs, runtime,
coupon, price cap) comes from the task configuration and the purchase policy.

The preflight never toggles the terms or submits. Only an explicitly armed purchase job may,
and only once (durable reservation in purchases.purchase_attempts).
"""
import json
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit, urljoin
from bs4 import BeautifulSoup
import accounts
import purchases

NAME='bero'
LABEL='BERO HOST'
MERCHANT='BERO'
HOST='bero-host.de'
ORIGIN=accounts.ORIGIN
ORDERS_URL=ORIGIN+'/customer/products'
BALANCE_PAGE='/customer/money'
CHECK_STEP=1400
SUBMIT_STEP=1500
SUPPORTS_CHECKOUT=True
CURRENCY='EUR'

ANNIVERSARY_M_SPECS='CPU: 4 Kerne\nRAM: 12 GB\nNVME: 120 GB\nIPv4: 1 Adresse\nIPv6: 1 /64 Netz\nBackups: 3 Slots'
PRESETS={
 'bero':{'name':'BERO · Anniversary M','adapter':'bero','url':ORIGIN+'/server/prepaid-kvm-rootserver-mieten',
         'max_price':'49.00','currency':'EUR','coupon':'DISCORD10','interval':15,
         'package_id':109,'package_name':'Anniversary M','runtime_days':365,'specs':ANNIVERSARY_M_SPECS,
         'note':'4 核 / 12 GB / 120 GB · 365 天。已核实首次优惠价 €44.10，续费 €49。'},
}
# Filled into BERO tasks saved before package fields existed (they were all Anniversary M).
LEGACY_DEFAULTS={k:PRESETS['bero'][k] for k in ('package_id','package_name','runtime_days','specs')}

class PreflightError(ValueError):
    pass

ERRORS=(PreflightError,accounts.LoginIssue)
PURCHASE_TIMEOUT=15

# ---------------------------------------------------------------- configuration

def validate(result,data):
    """Merchant-specific task fields; `result` already holds the common fields."""
    url=urlsplit(result['url'])
    if url.hostname!=HOST or not url.path.startswith('/server/') or url.query:
        raise ValueError('BERO 任务的商品地址须为 https://bero-host.de/server/… 套餐页面')
    if result['currency']!=CURRENCY:
        raise ValueError('BERO 只支持 EUR')
    try:
        package_id=int(data.get('package_id'))
        runtime=int(data.get('runtime_days'))
    except (TypeError,ValueError):
        raise ValueError('请填写套餐编号和周期天数（整数）') from None
    if not 1<=package_id<=1_000_000 or not 1<=runtime<=3650:
        raise ValueError('套餐编号或周期天数超出范围')
    name=data.get('package_name','')
    specs=data.get('specs','')
    if not isinstance(name,str) or not name.strip() or len(name)>100:
        raise ValueError('请填写套餐名称（与页面上的名称完全一致）')
    if not isinstance(specs,str) or len(specs)>1000:
        raise ValueError('规格文字过长')
    lines=[line.strip() for line in specs.splitlines() if line.strip()]
    if len(lines)>20:
        raise ValueError('规格最多 20 行')
    if result['coupon'] and not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',result['coupon']):
        raise ValueError('优惠码只能包含字母、数字、下划线和短横线')
    return result|{'package_id':package_id,'package_name':name.strip(),'runtime_days':runtime,'specs':'\n'.join(lines)}

@dataclass(frozen=True)
class Target:
    store:str
    package_id:int
    name:str
    runtime:int
    coupon:str
    specs:tuple

    @property
    def select(self):
        return 'selectPackage(%d)'%self.package_id

def target(cfg):
    cfg=LEGACY_DEFAULTS|cfg
    specs=(cfg['package_name'],*[s for s in cfg['specs'].splitlines() if s.strip()],'Laufzeit: %d Tage'%int(cfg['runtime_days']))
    return Target(store=cfg['url'],package_id=int(cfg['package_id']),name=cfg['package_name'],
                  runtime=int(cfg['runtime_days']),coupon=cfg.get('coupon',''),specs=specs)

def page_text(view):
    """Visible text with the runtime label normalised: a logged-in account may use English."""
    return re.sub(r'\b(?:Runtime|Laufzeit)\s*:','Laufzeit:',view.get_text(' ',strip=True),flags=re.I)

# ---------------------------------------------------------------- public stock monitor

def inspect(soup,cfg):
    """Return (status, price, detail) for the configured package card on the store page."""
    t=target(cfg)
    matches=soup.select('[wire\\:click="%s"]'%t.select)
    # Ignore controls nested inside the card (e.g. an extra "select" button) and accept
    # duplicated cards (e.g. desktop/mobile copies) only if they all say exactly the same.
    cards=[m for m in matches if not any(p in matches for p in m.parents)]
    if not cards:
        raise ValueError('未找到 %s 套餐卡片'%t.name)
    readings=set()
    for card in cards:
        text=page_text(card)
        if any(x not in text for x in t.specs):
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
        raise ValueError('页面上多个 %s 卡片的库存或价格不一致'%t.name)
    sold,price=readings.pop()
    return ('out_of_stock' if sold else 'available',Decimal(price),
            '官网显示售罄' if sold else '页面售罄标记已消失，结账库存仍需确认')

def check_store_page(page,cfg):
    # The order state is kept per account; on a later step the package cards are not rendered.
    # Specs, price and coupon are then verified on the overview/CHECK pages instead.
    if target(cfg).select not in page:
        return None
    import adapters
    try:
        return adapters.inspect(page,cfg)
    except ValueError as error:
        # adapters only raises its own fixed messages, never page content or credentials
        raise PreflightError('商品页检查未通过：'+str(error)) from None

# ---------------------------------------------------------------- account session

def account_ready():
    account=accounts.public_status()
    return bool(account['session_saved'] and account['status']=='authenticated')

def account_usable():
    return accounts.usable()

def session():
    return accounts.checkout_session()

# ---------------------------------------------------------------- Livewire order component

def model(element):
    return next((value for name,value in element.attrs.items() if name=='wire:model' or name.startswith('wire:model.')),'')

def enabled(element):
    if element is None:
        return False
    for node in [element,*element.parents]:
        if (node.has_attr('disabled') or node.has_attr('hidden') or node.get('aria-disabled')=='true'
                or any(name in node.attrs for name in ('x-bind:disabled',':disabled'))
                or 'd-none' in node.get('class',[]) or re.search(r'display\s*:\s*none',node.get('style',''),re.I)):
            return False
    return True

def click_expression(element):
    return element.get('wire:click') or element.get('wire:click.prevent') or element.get('x-on:click.prevent','')

def controls(view):
    """Export identifiers only: no input values, labels, HTML, tokens or personal data."""
    result=[]
    for element in view.select('button,input,select,form[wire\\:submit]'):
        if element.get('type') in ('hidden','password','email'):
            continue
        expression=element.get('wire:submit','') if element.name=='form' else click_expression(element)
        action=re.match(r'(?:\$wire\.)?([A-Za-z_][A-Za-z0-9_]*)',expression)
        binding=model(element)
        if binding and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.]{0,79}',binding):
            binding=''
        result.append({'tag':element.name,'method':action[1] if action else '',
                       'model':binding,'enabled':enabled(element)})
    return result[:30]

def money(text):
    """Parse '44,10' / '44.10' / '1.044,10' into Decimal; reject anything else."""
    value=text.strip()
    if re.fullmatch(r'\d{1,3}(?:\.\d{3})+,\d{2}',value):
        value=value.replace('.','').replace(',','.')
    elif re.fullmatch(r'\d+[,.]\d{2}',value):
        value=value.replace(',','.')
    else:
        raise PreflightError('金额格式无法识别')
    try:
        amount=Decimal(value)
    except InvalidOperation:
        raise PreflightError('金额格式无法识别') from None
    if not amount.is_finite() or amount<0:
        raise PreflightError('金额格式无法识别')
    return amount

def read_balance(client):
    """Account balance (Guthaben) from the customer balance page, language independent."""
    html=client.request(BALANCE_PAGE)
    state,_=accounts.inspect_login(html)
    if state!='authenticated':
        raise PreflightError('读取余额时登录已失效')
    soup=BeautifulSoup(html,'html.parser')
    titles=soup.select('#guthabenModalTitle')
    values={m[1] for t in titles for m in [re.search(r':\s*(\d[\d.,]*)\s*€\s*$',t.get_text(' ',strip=True))] if m}
    if len(titles)<1 or len(values)!=1:
        raise PreflightError('余额页面结构已变化，无法确认账户余额')
    return money(values.pop())

class OrderView:
    def __init__(self,client,html,*,target,armed=False):
        self.client=client
        self.target=target
        self.armed=armed
        self.final_sent=False
        page=BeautifulSoup(html,'html.parser')
        roots=page.find_all(attrs={'wire:name':'public.order.order'})
        scripts=page.find_all('script',attrs={'data-update-uri':True})
        token=page.select_one('meta[name="csrf-token"]')
        if len(roots)!=1 or len(scripts)!=1 or not token or not token.get('content'):
            raise PreflightError('结账网页结构已变化，无法安全读取配置')
        self.endpoint=urljoin(target.store,scripts[0]['data-update-uri'])
        endpoint=urlsplit(self.endpoint)
        if (endpoint.scheme!='https' or endpoint.hostname!=HOST or endpoint.port not in (None,443)
                or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
                or not re.fullmatch(r'/livewire-[A-Za-z0-9]+/update',endpoint.path)):
            raise PreflightError('网站结账地址不符合已核实的正常流程')
        self.token=token['content']
        self.snapshot=roots[0].get('wire:snapshot','')
        self.view=roots[0]
        self._read()
        self.identifier=self.memo.get('id')

    def _read(self):
        if self.view.select_one('.g-recaptcha,[name="g-recaptcha-response"],[name="cf-turnstile-response"],[data-sitekey]'):
            raise PreflightError('结账需要人工安全验证，已停止')
        try:
            state=json.loads(self.snapshot)
            self.data,self.memo=state['data'],state['memo']
            if (self.memo.get('name')!='public.order.order' or not isinstance(self.data,dict) or not state.get('checksum')
                    or not isinstance(self.memo.get('id'),str) or not self.memo['id']
                    or type(self.data.get('step')) is not int or type(self.data.get('package')) not in (int,type(None))
                    or type(self.data.get('packageAvailable')) is not bool):
                raise ValueError()
        except (ValueError,KeyError,TypeError):
            raise PreflightError('结账会话快照格式不匹配') from None

    def submit_form(self):
        forms=[f for f in self.view.select('form') if f.get('wire:submit')]
        if len(forms)!=1 or forms[0]['wire:submit']!='jumpToStep(%d)'%SUBMIT_STEP:
            return None
        buttons=forms[0].select('button[type="submit"]')
        return forms[0] if len(buttons)==1 and enabled(buttons[0]) else None

    def guard(self,method,params,updates):
        t=self.target
        if updates and (method!='addCode' or not t.coupon or updates!={'enteredCode':t.coupon}):
            raise PreflightError('禁止修改未核实的结账属性')
        if method=='jumpToStep' and params==[SUBMIT_STEP]:
            # Final order submission: armed purchase only, on the verified CHECK page, terms accepted.
            if (not self.armed or self.data.get('step')!=CHECK_STEP or self.data.get('agbConfirmation') is not True
                    or self.submit_form() is None):
                raise PreflightError('未满足最终下单条件，禁止提交订单')
            return
        if method=='toggleAgbConfirmation' and params==[]:
            box=self.view.select('#bero-order-accept-terms-checkbox')
            if (not self.armed or self.data.get('step')!=CHECK_STEP or self.data.get('agbConfirmation') is not False
                    or len(box)!=1 or box[0].get('wire:click')!='toggleAgbConfirmation' or not enabled(box[0])):
                raise PreflightError('未满足确认条款的条件，已停止')
            return
        if method=='selectPackage' and params==[t.package_id]:
            selector=next((n for n in self.view.find_all(attrs={'wire:click':t.select}) if enabled(n)),None)
        elif method=='jumpToStep' and len(params)==1 and type(params[0]) is int:
            if not 500<=params[0]<=5000 or params[0]==SUBMIT_STEP:
                raise PreflightError('结账步骤超出正常导航范围')
            expression='jumpToStep('+str(params[0])+')'
            selector=next((n for n in self.view.select('button') if click_expression(n)==expression and enabled(n)),None)
        elif method=='backFromConfiguration' and params==[]:
            selector=next((n for n in self.view.select('button') if click_expression(n)=='backFromConfiguration()' and enabled(n)),None)
        elif method=='saveIndividualConfig' and params==[{'runtime':str(t.runtime)}]:
            expression='$wire.saveIndividualConfig(beroSliderConfigurationJson)'
            selector=next((n for n in self.view.select('button') if click_expression(n)==expression and enabled(n)),None)
            slider=self.view.select_one('#sliderruntime')
            if not slider or str(t.runtime) not in slider.get('data-berovalues','').split(','):
                selector=None
        elif method=='addCode' and params==[]:
            selector=next((n for n in self.view.select('button') if click_expression(n)=='addCode' and enabled(n)),None)
            if not any(model(n)=='enteredCode' for n in self.view.select('input')):
                selector=None
        else:
            raise PreflightError('禁止调用未核实的结账操作')
        if selector is None:
            raise PreflightError('对应的正常页面控件缺失或已禁用，已停止')

    def call(self,method,params=None,updates=None,require_package=True):
        params,updates=params or [],updates or {}
        self.guard(method,params,updates)
        final=method=='jumpToStep' and params==[SUBMIT_STEP]
        payload={'_token':self.token,'components':[{'snapshot':self.snapshot,'updates':updates,
                 'calls':[{'path':'','method':method,'params':params}]}]}
        if final:
            self.final_sent=True
        response=self.client.request(self.endpoint,json_payload=payload,referer=self.target.store)
        try:
            components=response['components']
            if len(components)!=1:
                raise ValueError()
            component=components[0]
            effects=component['effects']
            redirect=effects.get('redirect')
            if final:
                # After ORDER NOW the shop may redirect (same origin) or render a thank-you step.
                if redirect and urlsplit(urljoin(self.target.store,redirect)).hostname!=HOST:
                    raise ValueError()
                return {'redirect':bool(redirect),'step':(json.loads(component.get('snapshot') or '{}').get('data') or {}).get('step')}
            if redirect or not isinstance(effects.get('html'),str):
                raise ValueError()
            self.snapshot=component['snapshot']
            self.view=BeautifulSoup(effects['html'],'html.parser')
            self._read()
            if self.memo.get('id')!=self.identifier:
                raise ValueError()
        except (ValueError,KeyError,TypeError,AttributeError):
            if final:
                return {'redirect':None,'step':None,'unparsed':True}
            raise PreflightError('结账返回跳转或未知结构，已停止') from None
        if require_package and self.data.get('package')!=self.target.package_id:
            raise PreflightError('商家返回的实际套餐不是 %s，已停止'%self.target.name)

    def next_button(self):
        matches=[n for n in self.view.select('button') if re.fullmatch(
            r'(?:WEITER|NEXT|NEXT STEP|NÄCHSTER SCHRITT)',n.get_text(' ',strip=True),re.I)]
        if len(matches)>1:
            raise PreflightError('存在多个下一步控件，停止自动导航')
        return matches[0] if matches else None

def single_price(view):
    prices=[]
    for node in view.select('p.fw-bold'):
        match=re.fullmatch(r'(\d[\d.,]*)\s*€\s*(?:INKL\. MWST\.|INCL\. VAT\.)',node.get_text(' ',strip=True),re.I)
        if match:
            prices.append(money(match[1]))
    if len(prices)!=1:
        raise PreflightError('价格缺失、币种不明或存在多个价格')
    if prices[0]<=0:
        raise PreflightError('价格无效，已停止')
    return prices[0]

def overview_quote(view,t):
    text=page_text(view)
    if any(value not in text for value in t.specs):
        raise PreflightError('结账概览的套餐规格或周期不匹配')
    price=single_price(view)
    if t.coupon:
        coupon=view.select_one('#bero-order-code-value')
        accepted=bool(coupon and re.fullmatch(r'-\s*[\d.,]+\s*(?:%|€)\s*\('+re.escape(t.coupon)+r'\)',coupon.get_text(' ',strip=True),re.I))
    else:
        accepted=True
    renewal=re.search(r'(?:Preis für die Verlängerung beträgt dauerhaft|renewal price is permanently)\s*(\d+[,.]\d{2})\s*€',text,re.I)
    return {'overview_total_eur':str(price),'overview_includes_vat':True,'coupon_accepted':accepted,
            'runtime_days':t.runtime,'specifications_verified':True,
            'renewal_eur':renewal[1].replace(',','.') if renewal else None}

def check_page_quote(order):
    """The final CHECK step: same specs, one VAT-inclusive price, terms box and ORDER NOW form."""
    view=order.view
    text=page_text(view)
    if order.data.get('step')!=CHECK_STEP:
        raise PreflightError('未进入最终确认页（步骤 %s），可能需要登录或充值，已停止'%order.data.get('step'))
    if any(value not in text for value in order.target.specs):
        raise PreflightError('最终确认页的套餐规格或周期不匹配')
    if len(view.select('#bero-order-accept-terms-checkbox[wire\\:click="toggleAgbConfirmation"]'))!=1:
        raise PreflightError('最终确认页缺少条款确认控件')
    if order.submit_form() is None:
        raise PreflightError('最终确认页的下单按钮结构已变化')
    return single_price(view)

def configure(order,stage):
    """Steps shared by preflight and purchase: package, runtime, coupon, overview."""
    t=order.target
    # BERO keeps the order state per account; a previous run may leave it on a later step.
    for _ in range(4):
        if order.view.find(attrs={'wire:click':t.select}):
            break
        back=None
        for n in order.view.select('button'):
            expression=click_expression(n)
            match=re.fullmatch(r'jumpToStep\((\d+)\)',expression)
            if enabled(n) and (expression=='backFromConfiguration()' or (match and int(match[1])<int(order.data.get('step') or 0))):
                back=n;break
        if back is None:
            raise PreflightError('结账页停留在步骤 %s，找不到返回选择套餐的控件'%order.data.get('step'))
        expression=click_expression(back)
        if expression=='backFromConfiguration()':
            order.call('backFromConfiguration',require_package=False)
        else:
            order.call('jumpToStep',[int(re.search(r'\d+',expression)[0])],require_package=False)
        stage()
    order.call('selectPackage',[t.package_id]);stage()
    order.call('jumpToStep',[1000]);stage()
    order.call('saveIndividualConfig',[{'runtime':str(t.runtime)}]);stage()
    quote=overview_quote(order.view,t)
    if t.coupon:
        order.call('addCode',updates={'enteredCode':t.coupon});stage()
        quote=overview_quote(order.view,t)
    return quote

def base_report(t,stages,quote):
    return {'merchant':MERCHANT,'package_id':t.package_id,'name':t.name,'coupon':t.coupon,
            'expected_quantity':1,'account_session_verified':True,'stages':stages,**quote}

def coupon_label(t):
    return t.coupon or '优惠码'

def preflight(client,cfg,policy):
    """Dry run. Reaches at most the CHECK page; never toggles the terms or submits."""
    t=target(cfg)
    page=client.request(t.store)
    check_store_page(page,cfg)
    order=OrderView(client,page,target=t)
    stages=[]
    def stage():
        stages.append({'step':order.data.get('step'),'package_id':order.data.get('package'),
                       'stock_available':order.data.get('packageAvailable'),'controls':controls(order.view)})
    quote=configure(order,stage)
    report=base_report(t,stages,quote)|{'balance_verified':False,'final_checkout_verified':False,
            'automatic_purchase_enabled':bool(policy.get('automatic_purchase_enabled')),
            'order_submitted':False,'paid':False}
    if not quote['coupon_accepted']:
        return report|{'status':'coupon_rejected','message':'%s 未被接受，预检停止'%coupon_label(t)}
    if Decimal(quote['overview_total_eur'])>Decimal(policy['max_total']):
        return report|{'status':'price_exceeded','message':'概览价格 €%s 超过购买上限 €%s，预检停止'%(quote['overview_total_eur'],policy['max_total'])}
    try:
        balance=read_balance(client)
        report|={'balance_verified':True,'balance_sufficient':balance>=Decimal(quote['overview_total_eur'])}
    except (PreflightError,accounts.LoginIssue):
        pass
    next_button=order.next_button()
    if order.data.get('packageAvailable') is not True or not enabled(next_button):
        return report|{'status':'out_of_stock','message':'套餐、优惠和价格已核实；商家缺货，下一步禁用（可正常等待补货）'}
    match=re.fullmatch(r'jumpToStep\((\d+)\)',click_expression(next_button))
    if not match or int(match[1])<=int(order.data.get('step',0)):
        return report|{'status':'needs_checkout_adapter','message':'下一步控件与已核实流程不同，预检停止'}
    order.call('jumpToStep',[int(match[1])]);stage()
    try:
        total=check_page_quote(order)
    except PreflightError as error:
        return report|{'status':'needs_checkout_adapter','message':str(error),'last_step':order.data.get('step')}
    return report|{'status':'ready','final_checkout_verified':True,'final_total_eur':str(total),
                   'message':'已到达最终确认页，结构与已核实流程一致；预检未勾选条款、未下单'}

def purchase(client,cfg,policy,task_id):
    """Armed one-time purchase. Returns a report; raises nothing after the order may have been sent."""
    t=target(cfg)
    cap=Decimal(policy['max_total'])
    client.timeout=PURCHASE_TIMEOUT  # the shop is slowest exactly when stock returns
    balance_before=read_balance(client)
    if balance_before<cap:
        return {'status':'insufficient_balance','message':f'账户余额 €{balance_before} 低于购买上限 €{cap}，未下单；请充值',
                'order_submitted':False,'paid':False,'balance_before':str(balance_before)}
    page=client.request(t.store)
    check_store_page(page,cfg)
    order=OrderView(client,page,target=t,armed=True)
    stages=[]
    def stage():
        stages.append({'step':order.data.get('step'),'stock_available':order.data.get('packageAvailable')})
    quote=configure(order,stage)
    report=base_report(t,stages,quote)|{'order_submitted':False,'paid':False,'balance_before':str(balance_before)}
    if not quote['coupon_accepted']:
        return report|{'status':'coupon_rejected','message':'%s 未被接受，未下单'%coupon_label(t)}
    total=Decimal(quote['overview_total_eur'])
    if total>cap:
        return report|{'status':'price_exceeded','message':f'价格 €{total} 超过上限 €{cap}，未下单'}
    next_button=order.next_button()
    if order.data.get('packageAvailable') is not True or not enabled(next_button):
        return report|{'status':'out_of_stock','message':'结账时商家仍显示缺货，未下单；继续监控'}
    match=re.fullmatch(r'jumpToStep\((\d+)\)',click_expression(next_button))
    if not match or int(match[1])!=CHECK_STEP:
        return report|{'status':'unexpected_flow','message':'下一步不是已核实的确认页（可能需要登录/付款步骤），未下单'}
    order.call('jumpToStep',[CHECK_STEP]);stage()
    final_total=check_page_quote(order)
    if final_total!=total or final_total>cap or final_total>balance_before:
        return report|{'status':'price_exceeded','message':f'确认页价格 €{final_total} 与概览不一致、超过上限或超过余额，未下单'}
    attempt=purchases.reserve_attempt(task_id,{'package_id':t.package_id,'quantity':1,'runtime_days':t.runtime,'currency':CURRENCY,
        'coupon':t.coupon,'coupon_accepted':True,'final_total_verified':True,'total':str(final_total),
        'payment':'balance','sufficient_balance':balance_before>=final_total})
    try:
        order.call('toggleAgbConfirmation')
        if order.data.get('agbConfirmation') is not True or order.data.get('step')!=CHECK_STEP:
            raise PreflightError('条款确认未生效，未下单')
        outcome=order.call('jumpToStep',[SUBMIT_STEP])
    except Exception as error:
        if not order.final_sent:
            purchases.release_attempt(attempt)
            raise
        outcome={'unparsed':True,'error':type(error).__name__}
    # The order request has been sent. Never resend. Verify through the balance change.
    report|={'order_submitted':True,'final_total_eur':str(final_total),'submit_response':outcome}
    try:
        balance_after=read_balance(client)
    except Exception:
        balance_after=None
    if balance_after is not None and balance_before-balance_after>=final_total-Decimal('0.01'):
        purchases.record_outcome(attempt,'balance-%s'%int(time.time()))
        return report|{'status':'purchased','paid':True,'balance_after':str(balance_after),
                       'message':f'已下单并从余额扣款 €{balance_before-balance_after}（余额 €{balance_before} → €{balance_after}）'}
    purchases.record_outcome(attempt,None)
    return report|{'status':'uncertain','balance_after':str(balance_after) if balance_after is not None else None,
                   'message':'已提交下单请求，但未确认扣款。请立即登录 BERO 查看「我的产品」和余额；程序不会重复下单'}
