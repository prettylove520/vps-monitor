import argparse
import getpass
import json
import time
from werkzeug.security import generate_password_hash
from store import set_setting, db
from adapters import PRESETS, validate

p=argparse.ArgumentParser()
p.add_argument('action',choices=['password','seed','disable-2fa'])
args=p.parse_args()
if args.action=='password':
    password=getpass.getpass('管理员密码（至少 12 位）：')
    if len(password)<12 or password!=getpass.getpass('再次输入：'):
        raise SystemExit('密码太短或两次输入不一致')
    set_setting('admin_password',generate_password_hash(password))
    print('管理员密码已保存。')
elif args.action=='disable-2fa':
    import twofactor
    twofactor.force_disable()
    print('已关闭管理员两步验证。重新登录后可在「安全」页面重新绑定。')
else:
    with db() as c:
        if not c.execute('SELECT 1 FROM tasks LIMIT 1').fetchone():
            c.execute('INSERT INTO tasks(config,created) VALUES (?,?)',(json.dumps(validate(PRESETS['bero']),ensure_ascii=False),time.time()))
    print('已准备 BERO 示例任务（暂停状态）。')
