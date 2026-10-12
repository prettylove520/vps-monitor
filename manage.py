import argparse
import getpass
import json
import time
from werkzeug.security import generate_password_hash
from store import set_setting, setting, db
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
    # Kept so install.sh and old instructions still work. No example task is created any more.
    set_setting('seeded','1')
    print('任务列表保持为空，请在后台自行添加监控任务。')
