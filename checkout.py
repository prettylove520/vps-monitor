"""Checkout job queue: dry-run preflights and armed one-time purchases, run by the worker.

Merchant specifics (pages, flow, safety checks) live in merchants/<name>.py; this module only
queues jobs, lends the merchant's logged-in session, records results and never replays a job.
"""
import json
import sys
import time
import traceback
from urllib.parse import urlsplit
import merchants
import purchases
from store import db, log

with db() as c:
    c.execute('''CREATE TABLE IF NOT EXISTS checkout_preflights (
        task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
        task_revision INTEGER NOT NULL, state TEXT NOT NULL, result TEXT,
        created REAL NOT NULL, started REAL, finished REAL)''')
    columns={row[1] for row in c.execute('PRAGMA table_info(checkout_preflights)')}
    if 'mode' not in columns:
        c.execute("ALTER TABLE checkout_preflights ADD COLUMN mode TEXT NOT NULL DEFAULT 'preflight'")

class JobError(ValueError):
    pass

def task_config(task_id):
    with db() as c:
        r=c.execute('SELECT config FROM tasks WHERE id=?',(task_id,)).fetchone()
    return json.loads(r[0]) if r else None

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
    cfg=task_config(task_id)
    if not cfg:
        raise ValueError('任务不存在')
    merchant=merchants.checkout_merchant(cfg)
    if not merchant.account_ready():
        raise ValueError('先到商家账户验证 %s 登录'%merchant.LABEL)
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
        if not task or json.loads(task['config'])['adapter']!=merchant.NAME:
            raise ValueError('任务的商家已变化，请刷新后重试')
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
    log(task_id,'info','自动下单已排队（余额付款，上限 %s %s）'%(policy['max_total'],policy.get('currency','EUR')) if mode=='purchase'
        else '结账预检已排队；只配置和检查，不勾选条款、不下单')

def run_next(notify=None):
    now=time.time()
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        # A crashed job is never replayed automatically.
        c.execute("UPDATE checkout_preflights SET state='error',result=?,finished=? WHERE state='running' AND started<?",
                  (json.dumps({'status':'interrupted','message':'上次结账任务中断，未自动重试；如为下单请到商家网站核实订单','order_submitted':None,'paid':None},ensure_ascii=False),now,now-300))
        row=c.execute("SELECT * FROM checkout_preflights WHERE state='queued' ORDER BY mode='purchase' DESC,created LIMIT 1").fetchone()
        if not row:
            return False
        row=dict(row)
        task=c.execute('SELECT revision,config FROM tasks WHERE id=?',(row['task_id'],)).fetchone()
        if not task or task['revision']!=row['task_revision']:
            c.execute("UPDATE checkout_preflights SET state='outdated',finished=? WHERE task_id=?",(now,row['task_id']))
            return True
        cfg=json.loads(task['config'])
        hostname=urlsplit(cfg['url']).hostname
        host=c.execute("SELECT next_check FROM hosts WHERE host=?",(hostname,)).fetchone()
        # A purchase is time critical and runs immediately; dry runs respect the shared host spacing.
        if row['mode']!='purchase' and host and host[0]>now:
            return False
        c.execute("INSERT INTO hosts VALUES (?,?) ON CONFLICT(host) DO UPDATE SET next_check=MAX(next_check,excluded.next_check)",(hostname,now+15))
        c.execute("UPDATE checkout_preflights SET state='running',started=? WHERE task_id=?",(now,row['task_id']))
    purchase=row['mode']=='purchase'
    state='done'
    try:
        merchant=merchants.checkout_merchant(cfg)
        policy=purchases.policy_for(row['task_id'])
        if not policy or (purchase and not purchases.is_armed(policy)):
            raise JobError('购买设置已变化或自动付款已关闭，未执行')
        with merchant.session() as client:
            result=merchant.purchase(client,cfg,policy,row['task_id']) if purchase else merchant.preflight(client,cfg,policy)
    except (JobError,purchases.AttemptExists,*getattr(merchants.REGISTRY.get(cfg.get('adapter')),'ERRORS',())) as error:
        state='error'
        result={'status':'error','message':str(error),'order_submitted':False,'paid':False}
    except Exception as error:
        state='error'
        result={'status':'error','message':'结账遇到网络或页面异常（%s），请查看登录状态后重试；未下单'%type(error).__name__,
                'order_submitted':False,'paid':False}
        # Location only (no exception text, which could contain page content): journalctl -u vps-watch-worker
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
        c.execute("UPDATE hosts SET next_check=MAX(next_check,?) WHERE host=?",(time.time()+15,hostname))
    if updated or result.get('order_submitted'):
        level='info' if result.get('status') in ('ready','out_of_stock','purchased') else 'warning'
        log(row['task_id'],level,('自动下单：' if purchase else '结账预检：')+result['message'])
    if notify:
        notify(row['task_id'],row['mode'],result)
    return True
