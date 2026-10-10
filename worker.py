import json
from concurrent.futures import ThreadPoolExecutor
import random
import time
from urllib.parse import urlsplit
import adapters
import notifications
import accounts
import merchants
import checkout
import purchases
import stock
import updates
from network import get_html, FetchError
from store import db, log, set_setting, DATA

def claim():
    now=time.time()
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        rows=c.execute('SELECT * FROM tasks WHERE (enabled=1 OR queued=1) AND next_check<=? AND lease<=? ORDER BY next_check,id',(now,now)).fetchall()
        for row in rows:
            cfg=json.loads(row['config'])
            host=urlsplit(cfg['url']).hostname
            h=c.execute('SELECT next_check FROM hosts WHERE host=?',(host,)).fetchone()
            if h and h[0]>now:
                continue
            c.execute('INSERT INTO hosts VALUES (?,?) ON CONFLICT(host) DO UPDATE SET next_check=excluded.next_check',(host,now+15))
            c.execute('UPDATE tasks SET lease=?,queued=0 WHERE id=?',(now+180,row['id']))
            return dict(row),cfg

def perform(row,cfg):
    now=time.time()
    enabled=row['enabled']
    failures=row['failures']
    notified=row['notified']
    delay=cfg['interval']+random.uniform(0,1.5)
    result={}
    try:
        if hasattr(merchants.REGISTRY.get(cfg['adapter']),'check'):
            result=adapters.check(cfg,get_html)
        else:
            result=adapters.inspect(get_html(cfg['url']),cfg)
        status=result['status']
        detail=result['detail']
        failures=0
        if status in ('out_of_stock','price_exceeded'):
            notified=0
        # Check that the user did not edit/pause/delete the task during the fetch.
        with db() as c:
            current=c.execute('SELECT revision FROM tasks WHERE id=?',(row['id'],)).fetchone()
        if not current or current[0]!=row['revision']:
            return
        stock.record(row['id'],status,result.get('price'))
        if status in ('out_of_stock','price_exceeded'):
            with db() as c:
                c.execute('DELETE FROM task_notifications WHERE task_id=? AND revision=?',(row['id'],row['revision']))
        policy=purchases.policy_for(row['id']) if status=='available' else None
        armed=purchases.is_armed(policy)
        purchase_queued=False
        if armed:
            # Time critical: queue the one-time purchase before spending seconds on notifications.
            # The worker runs it immediately on the next loop iteration (no host spacing).
            try:
                checkout.enqueue(row['id'],'purchase')
                purchase_queued=True
                detail+='；已排队自动下单（余额付款），下单结果单独通知'
            except ValueError as error:
                detail+='；自动下单未排队：'+str(error)
        # A queued purchase sends its own urgent notice; the plain restock alert follows on a later check.
        if status=='available' and not notified and not purchase_queued:
            try:
                with db() as c:
                    delivery=c.execute('SELECT channels FROM task_notifications WHERE task_id=? AND revision=?',(row['id'],row['revision'])).fetchone()
                link=getattr(merchants.REGISTRY.get(cfg['adapter']),'order_url',lambda c:c['url'])(cfg)
                price=f"{result['price']} {result['currency']}" if result.get('price') else '页面未提供'
                history=stock.alert_line(row['id'])
                message=f"补货提示：{cfg['name']}\n页面价格：{price}\n{detail}\n"
                if history:
                    message+=history+'\n'
                message+=f"优惠码：{cfg['coupon'] or '未设置'}\n{link}\n"+('已启用自动下单，结果另行通知。' if armed else '未下单或付款。')
                report=notifications.send(message,
                    delivered=json.loads(delivery['channels']) if delivery else [],url=link)
                with db() as c:
                    c.execute('INSERT INTO task_notifications(task_id,revision,channels) SELECT id,revision,? FROM tasks WHERE id=? AND revision=? ON CONFLICT(task_id) DO UPDATE SET revision=excluded.revision,channels=excluded.channels',
                              (json.dumps(report['sent']),row['id'],row['revision']))
                notified=int(report['complete'])
                if report['sent']:
                    detail+='；'+ ' / '.join(notifications.CHANNEL_LABELS[c] for c in report['sent'])+' 已发送'
                if report['failed']:
                    detail+='；'+'；'.join(report['failed'].values())
                    delay=max(delay,60)
            except ValueError as error:
                detail+='；'+str(error)
                delay=max(delay,60)
        if not armed and status=='available' and row['status'] in ('pending','out_of_stock','price_exceeded'):
            if policy and policy.get('auto_preflight'):
                try:
                    checkout.enqueue(row['id'])
                    detail+='；补货结账预检已排队（未下单或付款）'
                except ValueError as error:
                    detail+='；自动预检未排队：'+str(error)
        if armed:
            delay=min(delay,15)
        result['detail']=detail
    except FetchError as error:
        failures+=1
        enabled=0 if error.blocked else enabled
        status='blocked' if error.blocked else 'error'
        detail=str(error)
        delay=max(min(3600,300*2**min(failures-1,4)),error.retry_after)
        if error.retry_after:
            with db() as c:
                c.execute('UPDATE hosts SET next_check=MAX(next_check,?) WHERE host=?',(time.time()+delay,urlsplit(cfg['url']).hostname))
        result={'detail':detail}
    except Exception as error:
        failures+=1
        status='error'
        detail=str(error) if isinstance(error,ValueError) else '检查失败：'+type(error).__name__
        result={'detail':detail}
        delay=min(3600,300*2**min(failures-1,4))
    with db() as c:
        updated=c.execute('UPDATE tasks SET status=?,result=?,last_check=?,next_check=?,lease=0,enabled=?,failures=?,notified=? WHERE id=? AND revision=?',(
            status,json.dumps(result,ensure_ascii=False),time.time(),time.time()+delay,enabled,failures,notified,row['id'],row['revision'])).rowcount
    if updated:
        log(row['id'],'warning' if status in ('blocked','error') else 'info',detail)
        if status=='blocked' or failures==3:
            background(plain_notice,row['id'],'异常通知',f"监控异常：{cfg['name']}\n{detail}\n"+('任务已暂停。' if status=='blocked' else '正在退避重试。'),url=cfg['url'])

NOTICES={
 'purchased':('✅ 已自动下单：{name}',True),
 'uncertain':('⚠️ 下单结果不确定：{name}',True),
 'insufficient_balance':('⚠️ 余额不足，未下单：{name}',True),
 'coupon_rejected':('⚠️ 优惠码未生效，未下单：{name}',True),
 'price_exceeded':('⚠️ 价格超过上限，未下单：{name}',True),
 'unexpected_flow':('⚠️ 结账流程变化，未下单：{name}',True),
 'error':('⚠️ 自动下单失败：{name}',True),
}
HEALTH_INTERVAL=6*3600

NOTIFIER=ThreadPoolExecutor(max_workers=2,thread_name_prefix='notify')

def background(fn,*args,**kwargs):
    """Send a notice without holding up stock checks or the next purchase job."""
    def run():
        try:
            fn(*args,**kwargs)
        except Exception:
            log(None,'warning','后台通知发送失败')
    return NOTIFIER.submit(run)

def plain_notice(task_id,label,message,url=''):
    try:
        report=notifications.send(message,urgent=False,url=url)
        if report['failed']:
            log(task_id,'warning',label+'：'+'；'.join(report['failed'].values()))
    except ValueError:
        pass

def checkout_notice(task_id,mode,result):
    background(_checkout_notice,task_id,mode,result)

def _checkout_notice(task_id,mode,result):
    """Push the outcome of purchases, and of health preflights that found a broken flow."""
    status=result.get('status')
    if mode=='purchase':
        if status not in NOTICES:
            return
        title,urgent=NOTICES[status]
    elif status in ('error','needs_checkout_adapter','coupon_rejected') or result.get('balance_sufficient') is False:
        title,urgent='结账预检发现问题：{name}',False
    else:
        return
    with db() as c:
        row=c.execute('SELECT config FROM tasks WHERE id=?',(task_id,)).fetchone()
    cfg=json.loads(row[0]) if row else {}
    name=cfg.get('name','任务 %s'%task_id)
    orders=getattr(merchants.REGISTRY.get(cfg.get('adapter')),'ORDERS_URL','')
    lines=[title.format(name=name),result.get('message','')]
    if result.get('final_total_eur'):
        lines.append('金额：%s EUR'%result['final_total_eur'])
    if result.get('balance_sufficient') is False:
        lines.append('账户余额不足以支付，请提前充值。')
    if status in ('purchased','uncertain'):
        lines.append('请到 %s 核实。自动下单已关闭，不会重复下单。'%(orders or '商家网站'))
    try:
        report=notifications.send('\n'.join(lines),urgent=urgent,url=orders)
        if report['failed']:
            log(task_id,'warning','结账通知：'+'；'.join(report['failed'].values()))
    except ValueError:
        pass

def health_check():
    """While automatic purchase is armed, dry-run the checkout every few hours so a site change
    is noticed before a restock, not during it."""
    from store import setting
    now=time.time()
    if now-float(setting('checkout_health_last','0'))<HEALTH_INTERVAL:
        return
    set_setting('checkout_health_last',str(now))
    with db() as c:
        ids=[r[0] for r in c.execute('SELECT task_id FROM purchase_policies')]
    for task_id in ids:
        if purchases.is_armed(purchases.policy_for(task_id)):
            try:
                checkout.enqueue(task_id,'preflight')
            except ValueError as error:
                log(task_id,'warning','结账健康检查未排队：'+str(error))

def main():
    import fcntl
    lock=(DATA/'worker.lock').open('a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    while True:
        set_setting('worker_heartbeat',str(time.time()))
        try:
            message=accounts.maintain()
            if message:
                log(None,'warning','BERO 登录：'+message)
                background(plain_notice,None,'BERO 登录异常通知','BERO 登录需要处理：\n'+message+'\n补货监控继续运行；未下单或付款。')
        except Exception:
            log(None,'warning','BERO 会话检查暂时失败')
        try:
            health_check()
        except Exception:
            log(None,'warning','结账健康检查排队失败')
        try:
            updates.maybe_check()
        except Exception:
            pass
        if checkout.run_next(notify=checkout_notice):
            continue
        job=claim()
        if job:
            try:
                perform(*job)
            except Exception as error:
                log(None,'error','后台检查异常：'+type(error).__name__)
        else:
            time.sleep(1)

if __name__=='__main__':
    main()
