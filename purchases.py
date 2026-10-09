"""Purchase policy, explicit arming, and the durable one-attempt guard."""
import json
import time
from decimal import Decimal
from store import db

# Verified against the live BERO order flow on 2026-10-09 (see checkout.py docstring).
VERIFIED_CHECKOUT_ADAPTERS = frozenset({'BERO'})
FINGERPRINT='BERO:AnniversaryM:109:365:one-purchase'

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
        ''')

initialize()

def policy_for(task_id):
    with db() as c:
        r=c.execute('SELECT policy FROM purchase_policies WHERE task_id=?',(task_id,)).fetchone()
    return json.loads(r[0]) if r else None

def is_armed(policy):
    return bool(policy and policy.get('automatic_purchase_enabled') and policy.get('payment')=='balance'
                and policy.get('status')=='armed' and policy.get('merchant') in VERIFIED_CHECKOUT_ADAPTERS)

def last_attempt():
    with db() as c:
        r=c.execute('SELECT * FROM purchase_attempts WHERE fingerprint=?',(FINGERPRINT,)).fetchone()
    return dict(r) if r else None

def _write(c,task_id,policy):
    c.execute('INSERT INTO purchase_policies VALUES (?,?,?) ON CONFLICT(task_id) DO UPDATE SET policy=excluded.policy,updated=excluded.updated',
              (task_id,json.dumps(policy),time.time()))

def save_policy(task_id,data):
    if not isinstance(data,dict) or data.get('payment') not in ('balance','manual'):
        raise ValueError('请选择余额付款或人工付款')
    if not isinstance(data.get('auto_preflight',False),bool):
        raise ValueError('自动预检设置格式错误')
    with db() as c:
        r=c.execute('SELECT config FROM tasks WHERE id=?',(task_id,)).fetchone()
        if not r:
            raise ValueError('任务不存在')
        config=json.loads(r[0])
        if config['adapter']!='bero' or config['coupon']!='DISCORD10':
            raise ValueError('本次购买设置仅适用于 BERO Anniversary M，且任务优惠码须为 DISCORD10')
        # Saving the limits always disarms; arming is a separate, explicit step.
        policy={'merchant':'BERO','package_id':109,'package':'Anniversary M','quantity':1,
                'runtime_days':365,'currency':'EUR','max_total':'44.10','coupon':'DISCORD10',
                'coupon_required':True,'payment':data['payment'],'max_successful_purchases':1,
                'renewal_authorized':False,'status':'saved','checkout_verified':True,
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
        if policy['payment']!='balance':
            raise ValueError('自动付款只支持 BERO 账户余额；请把付款方式改为余额并保存')
        if not isinstance(confirm_total,str) or confirm_total.strip().replace(',','.')!=policy['max_total']:
            raise ValueError('请输入 %s 确认购买上限'%policy['max_total'])
        if c.execute('SELECT 1 FROM purchase_attempts WHERE fingerprint=?',(FINGERPRINT,)).fetchone():
            raise ValueError('已有购买记录，请先在 BERO 核实订单并清除记录')
        account=c.execute("SELECT status,session FROM merchant_accounts WHERE merchant='bero'").fetchone()
        if not account or account['status']!='authenticated' or not account['session']:
            raise ValueError('先到商家账户验证 BERO 登录')
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
    expected={'package_id':109,'quantity':1,'runtime_days':365,'currency':'EUR','coupon':'DISCORD10','coupon_accepted':True}
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
        if c.execute('SELECT 1 FROM purchase_attempts WHERE fingerprint=?',(FINGERPRINT,)).fetchone():
            raise AttemptExists('已有购买尝试，请先核实订单；禁止重复提交')
        return c.execute('INSERT INTO purchase_attempts(task_id,fingerprint,state,created) VALUES (?,?,?,?)',
                         (task_id,FINGERPRINT,'submitting',time.time())).lastrowid

def release_attempt(attempt_id):
    """Only for failures that happened before the order request was sent."""
    with db() as c:
        c.execute("DELETE FROM purchase_attempts WHERE id=? AND state='submitting'",(attempt_id,))

def record_outcome(attempt_id,confirmed_order_id=None):
    with db() as c:
        c.execute('UPDATE purchase_attempts SET state=?,order_id=? WHERE id=?',
                  ('success' if confirmed_order_id else 'uncertain',confirmed_order_id,attempt_id))

def clear_attempts(confirm):
    """User confirmed in BERO what happened; allow a future purchase again."""
    if confirm!='已核实':
        raise ValueError('请输入「已核实」确认你已在 BERO 检查过订单和余额')
    with db() as c:
        c.execute('DELETE FROM purchase_attempts WHERE fingerprint=?',(FINGERPRINT,))
