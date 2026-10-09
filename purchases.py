"""Purchase policy per task, explicit arming, and the durable one-attempt guard."""
import json
import time
from decimal import Decimal, InvalidOperation
from store import db

# Merchants whose order flow has been verified against the live site.
VERIFIED_CHECKOUT_ADAPTERS = frozenset({'BERO'})
OPEN_STATES=('submitting','success','uncertain')

class AttemptExists(ValueError):
    pass

def initialize():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS purchase_policies (
          task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
          policy TEXT NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS purchase_attempts (
          id INTEGER PRIMARY KEY, task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
          fingerprint TEXT NOT NULL UNIQUE, state TEXT NOT NULL,
          created REAL NOT NULL, order_id TEXT);
        CREATE INDEX IF NOT EXISTS idx_attempts_task ON purchase_attempts(task_id,id);
        ''')

initialize()

def _merchant(cfg):
    import merchants  # late import: merchants.bero imports this module
    return merchants.checkout_merchant(cfg)

def _task_config(c,task_id):
    r=c.execute('SELECT config FROM tasks WHERE id=?',(task_id,)).fetchone()
    if not r:
        raise ValueError('任务不存在')
    return json.loads(r[0])

def policy_for(task_id):
    with db() as c:
        r=c.execute('SELECT policy FROM purchase_policies WHERE task_id=?',(task_id,)).fetchone()
    return json.loads(r[0]) if r else None

def is_armed(policy):
    return bool(policy and policy.get('automatic_purchase_enabled') and policy.get('payment')=='balance'
                and policy.get('status')=='armed' and policy.get('merchant') in VERIFIED_CHECKOUT_ADAPTERS)

def attempt_for(task_id):
    """Latest unresolved attempt for this task (blocks re-arming until the user clears it)."""
    with db() as c:
        r=c.execute('SELECT * FROM purchase_attempts WHERE task_id=? AND state IN (%s) ORDER BY id DESC LIMIT 1'
                    %','.join('?'*len(OPEN_STATES)),(task_id,*OPEN_STATES)).fetchone()
    return dict(r) if r else None

def _write(c,task_id,policy):
    c.execute('INSERT INTO purchase_policies VALUES (?,?,?) ON CONFLICT(task_id) DO UPDATE SET policy=excluded.policy,updated=excluded.updated',
              (task_id,json.dumps(policy),time.time()))

def _amount(value):
    try:
        amount=Decimal(str(value).strip().replace(',','.'))
        if not amount.is_finite() or amount<=0 or amount>100000 or amount.as_tuple().exponent<-2:
            raise ValueError()
    except (InvalidOperation,ValueError):
        raise ValueError('购买上限须为正数，最多两位小数') from None
    return str(amount.quantize(Decimal('0.01')))

def save_policy(task_id,data):
    if not isinstance(data,dict) or data.get('payment') not in ('balance','manual'):
        raise ValueError('请选择余额付款或人工付款')
    if not isinstance(data.get('auto_preflight',False),bool):
        raise ValueError('自动预检设置格式错误')
    with db() as c:
        config=_task_config(c,task_id)
        merchant=_merchant(config)
        old=c.execute('SELECT policy FROM purchase_policies WHERE task_id=?',(task_id,)).fetchone()
        previous=json.loads(old[0]) if old else {}
        max_total=_amount(data.get('max_total') or previous.get('max_total') or config['max_price'])
        # Saving the limits always disarms; arming is a separate, explicit step.
        policy={'merchant':merchant.MERCHANT,'adapter':merchant.NAME,'package_id':config.get('package_id'),
                'package':config.get('package_name',config['name']),'quantity':1,
                'runtime_days':config.get('runtime_days'),'currency':merchant.CURRENCY,'max_total':max_total,
                'coupon':config.get('coupon',''),'coupon_required':bool(config.get('coupon')),'payment':data['payment'],
                'max_successful_purchases':1,'renewal_authorized':False,'status':'saved',
                'checkout_verified':merchant.MERCHANT in VERIFIED_CHECKOUT_ADAPTERS,
                'automatic_purchase_enabled':False,'auto_preflight':data.get('auto_preflight',False)}
        _write(c,task_id,policy)
    return policy

def arm(task_id,confirm_total):
    """Explicitly enable the one-time automatic purchase. The user re-types the cap to confirm."""
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        r=c.execute('SELECT policy FROM purchase_policies WHERE task_id=?',(task_id,)).fetchone()
        if not r:
            raise ValueError('请先保存购买设置')
        policy=json.loads(r[0])
        merchant=_merchant(_task_config(c,task_id))
        if policy['merchant'] not in VERIFIED_CHECKOUT_ADAPTERS:
            raise ValueError('该商家的结账流程尚未验证，不能自动付款')
        if policy['payment']!='balance':
            raise ValueError('自动付款只支持商家账户余额；请把付款方式改为余额并保存')
        try:
            confirmed=_amount(confirm_total)
        except ValueError:
            confirmed=None
        if confirmed!=policy['max_total']:
            raise ValueError('请输入 %s 确认购买上限'%policy['max_total'])
        if c.execute('SELECT 1 FROM purchase_attempts WHERE task_id=? AND state IN (%s)'%','.join('?'*len(OPEN_STATES)),
                     (task_id,*OPEN_STATES)).fetchone():
            raise ValueError('已有购买记录，请先在商家网站核实订单并清除记录')
    if not merchant.account_ready():
        raise ValueError('先到商家账户验证 %s 登录'%merchant.LABEL)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        current=c.execute('SELECT policy FROM purchase_policies WHERE task_id=?',(task_id,)).fetchone()
        if not current or json.loads(current[0])!=policy:
            raise ValueError('购买设置刚刚发生变化，请重试')
        policy|={'automatic_purchase_enabled':True,'status':'armed','armed_at':time.time()}
        _write(c,task_id,policy)
    return policy

def disarm(task_id,status='saved'):
    with db() as c:
        r=c.execute('SELECT policy FROM purchase_policies WHERE task_id=?',(task_id,)).fetchone()
        if not r:
            return None
        policy=json.loads(r[0])|{'automatic_purchase_enabled':False,'status':status}
        _write(c,task_id,policy)
    return policy

def finish(task_id,outcome):
    """After an order request was sent: disarm and pause monitoring of this task."""
    disarm(task_id,'purchased' if outcome=='purchased' else 'uncertain')
    with db() as c:
        c.execute('UPDATE tasks SET enabled=0 WHERE id=?',(task_id,))

def validate_quote(policy,quote):
    expected={'package_id':policy.get('package_id'),'quantity':1,'runtime_days':policy.get('runtime_days'),
              'currency':policy['currency'],'coupon':policy.get('coupon',''),'coupon_accepted':True}
    if any(quote.get(k)!=v for k,v in expected.items()):
        raise ValueError('结账套餐、周期、数量、币种或优惠不匹配')
    if quote.get('final_total_verified') is not True:
        raise ValueError('尚未核实含税及全部费用的最终总价')
    try:
        total=Decimal(str(quote['total']))
        if not total.is_finite() or total<=0 or total>Decimal(policy['max_total']):
            raise ValueError()
    except Exception:
        raise ValueError('结账总价超过授权范围或价格格式错误') from None
    if policy['payment']=='balance':
        if quote.get('payment')!='balance' or quote.get('sufficient_balance') is not True:
            raise ValueError('余额付款方式或余额不足')

def reserve_attempt(task_id,quote):
    """Durably claim the single purchase before anything irreversible is sent."""
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT policy FROM purchase_policies WHERE task_id=?',(task_id,)).fetchone()
        if not row:
            raise ValueError('购买限制尚未配置')
        policy=json.loads(row[0])
        if not is_armed(policy):
            raise ValueError('自动付款未启用')
        validate_quote(policy,quote)
        if c.execute('SELECT 1 FROM purchase_attempts WHERE task_id=? AND state IN (%s)'%','.join('?'*len(OPEN_STATES)),
                     (task_id,*OPEN_STATES)).fetchone():
            raise AttemptExists('已有购买尝试，请先核实订单；禁止重复提交')
        fingerprint='task:%d:armed:%s'%(task_id,policy.get('armed_at'))
        try:
            return c.execute('INSERT INTO purchase_attempts(task_id,fingerprint,state,created) VALUES (?,?,?,?)',
                             (task_id,fingerprint,'submitting',time.time())).lastrowid
        except Exception:
            raise AttemptExists('本次启用已经尝试过下单；请重新启用自动付款') from None

def release_attempt(attempt_id):
    """Only for failures that happened before the order request was sent."""
    with db() as c:
        c.execute("DELETE FROM purchase_attempts WHERE id=? AND state='submitting'",(attempt_id,))

def record_outcome(attempt_id,confirmed_order_id=None):
    with db() as c:
        c.execute('UPDATE purchase_attempts SET state=?,order_id=? WHERE id=?',
                  ('success' if confirmed_order_id else 'uncertain',confirmed_order_id,attempt_id))

def clear_attempts(task_id,confirm):
    """User confirmed on the merchant site what happened; allow a future purchase for this task."""
    if confirm!='已核实':
        raise ValueError('请输入「已核实」确认你已在商家网站检查过订单和余额')
    with db() as c:
        c.execute("UPDATE purchase_attempts SET state='cleared' WHERE task_id=? AND state IN (%s)"%','.join('?'*len(OPEN_STATES)),
                  (task_id,*OPEN_STATES))
