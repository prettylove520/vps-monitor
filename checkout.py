"""BERO configuration/checkout: dry-run preflight and the armed one-time purchase executor.

Verified live order flow (2026-10-09, logged-in account with prepaid balance):
  500 select package -> 1000 configuration -> 1100 overview (+coupon) -> NEXT jumpToStep(1400)
  -> 1400 CHECK page: checkbox wire:click="toggleAgbConfirmation" and
     <form wire:submit="jumpToStep(1500)"> "ORDER NOW" / "JETZT BESTELLEN".
BERO is prepaid: there is no payment-method step for a logged-in account; the order is paid
from the account balance (Guthaben). LOGIN (1200) / PAYMENT (1300) steps mean we are not in
that state and the executor stops.

The preflight never toggles the terms or submits. Only an explicitly armed purchase job may,
and only once (durable fingerprint in purchases.purchase_attempts).
"""
import json
import re
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit, urljoin
from bs4 import BeautifulSoup
import accounts
import adapters
import purchases
from store import db, log

STORE=accounts.ORIGIN+'/server/prepaid-kvm-rootserver-mieten'
BALANCE_PAGE='/customer/money'
CHECK_STEP=1400
SUBMIT_STEP=1500
REQUIRED_SPECS=('Anniversary M','CPU: 4 Kerne','RAM: 12 GB','NVME: 120 GB',
                'IPv4: 1 Adresse','IPv6: 1 /64 Netz','Backups: 3 Slots','Laufzeit: 365 Tage')

with db() as c:
    c.execute('''CREATE TABLE IF NOT EXISTS checkout_preflights (
        task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
        task_revision INTEGER NOT NULL, state TEXT NOT NULL, result TEXT,
        created REAL NOT NULL, started REAL, finished REAL)''')
    columns={row[1] for row in c.execute('PRAGMA table_info(checkout_preflights)')}
    if 'mode' not in columns:
        c.execute("ALTER TABLE checkout_preflights ADD COLUMN mode TEXT NOT NULL DEFAULT 'preflight'")

class PreflightError(ValueError):
    pass

def page_text(view):
    """Visible text with the runtime label normalised: a logged-in account may use English."""
    return re.sub(r'\b(?:Runtime|Laufzeit)\s*:','Laufzeit:',view.get_text(' ',strip=True),flags=re.I)

def check_store_page(page):
    # The order state is kept per account; on a later step the package cards are not rendered.
    # Specs, price and coupon are then verified on the overview/CHECK pages instead.
    if 'selectPackage(109)' not in page:
        return None
    try:
        return adapters.inspect(page,adapters.PRESETS['bero'])
    except ValueError as error:
        # adapters only raises its own fixed messages, never page content or credentials
        raise PreflightError('商品页检查未通过：'+str(error)) from None

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
    def __init__(self,client,html,*,armed=False):
        self.client=client
        self.armed=armed
        self.final_sent=False
        page=BeautifulSoup(html,'html.parser')
        roots=page.find_all(attrs={'wire:name':'public.order.order'})
        scripts=page.find_all('script',attrs={'data-update-uri':True})
        token=page.select_one('meta[name="csrf-token"]')
        if len(roots)!=1 or len(scripts)!=1 or not token or not token.get('content'):
            raise PreflightError('结账网页结构已变化，无法安全读取配置')
        self.endpoint=urljoin(STORE,scripts[0]['data-update-uri'])
        endpoint=urlsplit(self.endpoint)
        if (endpoint.scheme!='https' or endpoint.hostname!='bero-host.de' or endpoint.port not in (None,443)
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
        if updates and (method!='addCode' or updates!={'enteredCode':'DISCORD10'}):
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
        if method=='selectPackage' and params==[109]:
            selector=next((n for n in self.view.find_all(attrs={'wire:click':'selectPackage(109)'}) if enabled(n)),None)
        elif method=='jumpToStep' and len(params)==1 and type(params[0]) is int:
            if not 500<=params[0]<=5000 or params[0]==SUBMIT_STEP:
                raise PreflightError('结账步骤超出正常导航范围')
            expression='jumpToStep('+str(params[0])+')'
            selector=next((n for n in self.view.select('button') if click_expression(n)==expression and enabled(n)),None)
        elif method=='backFromConfiguration' and params==[]:
            selector=next((n for n in self.view.select('button') if click_expression(n)=='backFromConfiguration()' and enabled(n)),None)
        elif method=='saveIndividualConfig' and params==[{'runtime':'365'}]:
            expression='$wire.saveIndividualConfig(beroSliderConfigurationJson)'
            selector=next((n for n in self.view.select('button') if click_expression(n)==expression and enabled(n)),None)
            slider=self.view.select_one('#sliderruntime')
            if not slider or '365' not in slider.get('data-berovalues','').split(','):
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
        response=self.client.request(self.endpoint,json_payload=payload,referer=STORE)
        try:
            components=response['components']
            if len(components)!=1:
                raise ValueError()
            component=components[0]
            effects=component['effects']
            redirect=effects.get('redirect')
            if final:
                # After ORDER NOW the shop may redirect (same origin) or render a thank-you step.
                if redirect and urlsplit(urljoin(STORE,redirect)).hostname!='bero-host.de':
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
        if require_package and self.data.get('package')!=109:
            raise PreflightError('商家返回的实际套餐不是 Anniversary M，已停止')

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

def overview_quote(view):
    text=page_text(view)
    if any(value not in text for value in REQUIRED_SPECS):
        raise PreflightError('结账概览的套餐规格或365天周期不匹配')
    price=single_price(view)
    coupon=view.select_one('#bero-order-code-value')
    accepted=bool(coupon and re.fullmatch(r'-\s*10(?:[.,]00)?\s*%\s*\(DISCORD10\)',coupon.get_text(' ',strip=True),re.I))
    renewal=re.search(r'(?:Preis für die Verlängerung beträgt dauerhaft|renewal price is permanently)\s*(\d+[,.]\d{2})\s*€',text,re.I)
    return {'overview_total_eur':str(price),'overview_includes_vat':True,'coupon_accepted':accepted,
            'runtime_days':365,'specifications_verified':True,
            'renewal_eur':renewal[1].replace(',','.') if renewal else None}

def check_page_quote(order):
    """The final CHECK step: same specs, one VAT-inclusive price, terms box and ORDER NOW form."""
    view=order.view
    text=page_text(view)
    if order.data.get('step')!=CHECK_STEP:
        raise PreflightError('未进入最终确认页（步骤 %s），可能需要登录或充值，已停止'%order.data.get('step'))
    if any(value not in text for value in REQUIRED_SPECS):
        raise PreflightError('最终确认页的套餐规格或周期不匹配')
    if len(view.select('#bero-order-accept-terms-checkbox[wire\\:click="toggleAgbConfirmation"]'))!=1:
        raise PreflightError('最终确认页缺少条款确认控件')
    if order.submit_form() is None:
        raise PreflightError('最终确认页的下单按钮结构已变化')
    return single_price(view)

def configure(order,policy,stage):
    """Steps shared by preflight and purchase: package 109, 365 days, DISCORD10, overview."""
    # BERO keeps the order state per account; a previous run may leave it on a later step.
    for _ in range(4):
        if order.view.find(attrs={'wire:click':'selectPackage(109)'}):
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
    order.call('selectPackage',[109]);stage()
    order.call('jumpToStep',[1000]);stage()
    order.call('saveIndividualConfig',[{'runtime':'365'}]);stage()
    overview_quote(order.view)
    order.call('addCode',updates={'enteredCode':'DISCORD10'});stage()
    return overview_quote(order.view)

def base_report(stages,quote):
    return {'merchant':'BERO','package_id':109,'name':'Anniversary M','coupon':'DISCORD10',
            'expected_quantity':1,'account_session_verified':True,'stages':stages,**quote}

def inspect_checkout(client,policy):
    """Dry run. Reaches at most the CHECK page; never toggles the terms or submits."""
    page=client.request(STORE)
    check_store_page(page)
    order=OrderView(client,page)
    stages=[]
    def stage():
        stages.append({'step':order.data.get('step'),'package_id':order.data.get('package'),
                       'stock_available':order.data.get('packageAvailable'),'controls':controls(order.view)})
    quote=configure(order,policy,stage)
    report=base_report(stages,quote)|{'balance_verified':False,'final_checkout_verified':False,
            'automatic_purchase_enabled':bool(policy.get('automatic_purchase_enabled')),
            'order_submitted':False,'paid':False}
    if not quote['coupon_accepted']:
        return report|{'status':'coupon_rejected','message':'DISCORD10 未被接受，预检停止'}
    if Decimal(quote['overview_total_eur'])>Decimal(policy['max_total']):
        return report|{'status':'price_exceeded','message':'概览价格超过购买上限，预检停止'}
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

def execute_purchase(client,policy,task_id):
    """Armed one-time purchase. Returns a report; raises nothing after the order may have been sent."""
    cap=Decimal(policy['max_total'])
    balance_before=read_balance(client)
    if balance_before<cap:
        return {'status':'insufficient_balance','message':f'账户余额 €{balance_before} 低于购买上限 €{cap}，未下单；请充值',
                'order_submitted':False,'paid':False,'balance_before':str(balance_before)}
    page=client.request(STORE)
    check_store_page(page)
    order=OrderView(client,page,armed=True)
    stages=[]
    def stage():
        stages.append({'step':order.data.get('step'),'stock_available':order.data.get('packageAvailable')})
    quote=configure(order,policy,stage)
    report=base_report(stages,quote)|{'order_submitted':False,'paid':False,'balance_before':str(balance_before)}
    if not quote['coupon_accepted']:
        return report|{'status':'coupon_rejected','message':'DISCORD10 未被接受，未下单'}
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
    attempt=purchases.reserve_attempt(task_id,{'package_id':109,'quantity':1,'runtime_days':365,'currency':'EUR',
        'coupon':'DISCORD10','coupon_accepted':True,'final_total_verified':True,'total':str(final_total),
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

def latest(task_id):
    with db() as c:
        row=c.execute('SELECT * FROM checkout_preflights WHERE task_id=?',(task_id,)).fetchone()
    if not row:
        return None
    return {'state':row['state'],'mode':row['mode'],'created':row['created'],'started':row['started'],'finished':row['finished'],
            'result':json.loads(row['result']) if row['result'] else None}

def enqueue(task_id,mode='preflight'):
    if mode not in ('preflight','purchase'):
        raise ValueError('未知结账操作')
    account=accounts.public_status()
    if not account['session_saved'] or account['status']!='authenticated':
        raise ValueError('先到商家账户验证 BERO 登录')
    policy=purchases.policy_for(task_id)
    if not policy:
        raise ValueError('请先保存此任务的购买限制')
    if mode=='purchase' and not purchases.is_armed(policy):
        raise ValueError('自动付款未启用')
    now=time.time()
    cooldown=15 if mode=='purchase' else 60
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        task=c.execute('SELECT revision,config FROM tasks WHERE id=?',(task_id,)).fetchone()
        if not task or json.loads(task['config'])['adapter']!='bero':
            raise ValueError('仅支持 BERO Anniversary M 任务')
        row=c.execute('SELECT * FROM checkout_preflights WHERE task_id=?',(task_id,)).fetchone()
        if row and row['state'] in ('queued','running'):
            if mode=='purchase' and row['state']=='queued' and row['mode']=='preflight':
                pass  # upgrade a waiting dry run to the real purchase
            else:
                raise ValueError('结账任务已排队或正在运行')
        elif row and now-row['created']<cooldown+(15 if mode=='purchase' and row['mode']=='purchase'
                and (json.loads(row['result'] or '{}').get('status')=='out_of_stock') else 0):
            raise ValueError('结账操作至少间隔%d秒，请稍后重试'%cooldown)
        c.execute('''INSERT INTO checkout_preflights(task_id,task_revision,state,created,mode)
            VALUES (?,?,'queued',?,?) ON CONFLICT(task_id) DO UPDATE SET task_revision=excluded.task_revision,
            state='queued',result=NULL,created=excluded.created,started=NULL,finished=NULL,mode=excluded.mode''',
            (task_id,task['revision'],now,mode))
    log(task_id,'info','自动下单已排队（余额付款，上限 €%s）'%policy['max_total'] if mode=='purchase'
        else '结账预检已排队；只配置和检查，不勾选条款、不下单')

def run_next(notify=None):
    now=time.time()
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        # A crashed job is never replayed automatically.
        c.execute("UPDATE checkout_preflights SET state='error',result=?,finished=? WHERE state='running' AND started<?",
                  (json.dumps({'status':'interrupted','message':'上次结账任务中断，未自动重试；如为下单请到 BERO 核实订单','order_submitted':None,'paid':None},ensure_ascii=False),now,now-300))
        row=c.execute("SELECT * FROM checkout_preflights WHERE state='queued' ORDER BY mode='purchase' DESC,created LIMIT 1").fetchone()
        if not row:
            return False
        row=dict(row)
        task=c.execute('SELECT revision FROM tasks WHERE id=?',(row['task_id'],)).fetchone()
        if not task or task['revision']!=row['task_revision']:
            c.execute("UPDATE checkout_preflights SET state='outdated',finished=? WHERE task_id=?",(now,row['task_id']))
            return True
        host=c.execute("SELECT next_check FROM hosts WHERE host='bero-host.de'").fetchone()
        # A purchase is time critical and runs immediately; dry runs respect the shared host spacing.
        if row['mode']!='purchase' and host and host[0]>now:
            return False
        c.execute("INSERT INTO hosts VALUES ('bero-host.de',?) ON CONFLICT(host) DO UPDATE SET next_check=MAX(next_check,excluded.next_check)",(now+15,))
        c.execute("UPDATE checkout_preflights SET state='running',started=? WHERE task_id=?",(now,row['task_id']))
    purchase=row['mode']=='purchase'
    state='done'
    try:
        policy=purchases.policy_for(row['task_id'])
        if not policy or (purchase and not purchases.is_armed(policy)):
            raise PreflightError('购买设置已变化或自动付款已关闭，未执行')
        with accounts.checkout_session() as client:
            result=execute_purchase(client,policy,row['task_id']) if purchase else inspect_checkout(client,policy)
    except (PreflightError,accounts.LoginIssue,purchases.AttemptExists) as error:
        state='error'
        result={'status':'error','message':str(error),'order_submitted':False,'paid':False}
    except Exception as error:
        state='error'
        result={'status':'error','message':'结账遇到网络或页面异常（%s），请查看登录状态后重试；未下单'%type(error).__name__,
                'order_submitted':False,'paid':False}
        # Location only (no exception text, which could contain page content): journalctl -u vps-watch-worker
        import sys,traceback
        frames=traceback.extract_tb(error.__traceback__)[-4:]
        print('checkout error',type(error).__name__,' <- '.join(f'{f.filename.rsplit("/",1)[-1]}:{f.lineno}' for f in reversed(frames)),file=sys.stderr,flush=True)
    if purchase and result.get('status') in ('purchased','uncertain'):
        purchases.finish(row['task_id'],result['status'])
    with db() as c:
        current=c.execute('SELECT revision FROM tasks WHERE id=?',(row['task_id'],)).fetchone()
        updated=0
        if current:
            if current[0]!=row['task_revision'] and not result.get('order_submitted'):
                state='outdated'
                result={'status':'outdated','message':'任务在执行期间发生变化，本次结果作废','order_submitted':False,'paid':False}
            updated=c.execute("UPDATE checkout_preflights SET state=?,result=?,finished=? WHERE task_id=? AND created=? AND state='running'",
                      (state,json.dumps(result,ensure_ascii=False),time.time(),row['task_id'],row['created'])).rowcount
        c.execute("UPDATE hosts SET next_check=MAX(next_check,?) WHERE host='bero-host.de'",(time.time()+15,))
    if updated or result.get('order_submitted'):
        level='info' if result.get('status') in ('ready','out_of_stock','purchased') else 'warning'
        log(row['task_id'],level,('自动下单：' if purchase else '结账预检：')+result['message'])
    if notify:
        notify(row['task_id'],row['mode'],result)
    return True
