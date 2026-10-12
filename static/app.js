const $ = s => document.querySelector(s);
let csrf = '',
  data = null,
  filter = 'all',
  editing = null,
  currentView = 'tasks',
  refreshing = false;
const labels = {
  pending: '待检查',
  out_of_stock: '售罄',
  available: '可能有货',
  price_exceeded: '超过上限',
  error: '检查异常',
  blocked: '访问受限',
};
const esc = s =>
  String(s ?? '').replace(
    /[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c],
  );
const time = t => (t ? new Date(t * 1000).toLocaleString('zh-CN', { hour12: false }) : '尚未检查');
async function api(path, method = 'GET', body) {
  const r = await fetch('/api' + path, {
    method,
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let v;
  try {
    v = await r.json();
  } catch {
    throw new Error('服务暂时不可用，请稍后重试');
  }
  if (!r.ok) {
    if (r.status === 401 && path != '/login') {
      showLogin();
    }
    const error = new Error(v.error || '请求失败');
    error.data = v;
    throw error;
  }
  return v;
}
function toast(msg) {
  $('#toast').textContent = msg;
  $('#toast').hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => ($('#toast').hidden = true), 5000);
}
function showLogin() {
  $('#workspace').hidden = true;
  $('#loginView').hidden = false;
}
async function boot() {
  try {
    const s = await api('/session');
    csrf = s.csrf;
    if (s.authenticated) {
      $('#loginView').hidden = true;
      $('#workspace').hidden = false;
      await refresh();
    } else showLogin();
  } catch (e) {
    $('#loginError').textContent = e.message;
  }
}
$('#loginForm').addEventListener('submit', async e => {
  e.preventDefault();
  const b = e.submitter;
  b.disabled = true;
  try {
    const s = await api('/login', 'POST', { password: $('#password').value, code: $('#loginCode').value });
    csrf = s.csrf;
    $('#password').value = '';
    $('#loginCode').value = '';
    $('#codeField').hidden = true;
    $('#loginError').textContent = '';
    await boot();
  } catch (e) {
    $('#loginError').textContent = e.message;
    if (e.data?.totp_required) {
      $('#codeField').hidden = false;
      $('#loginCode').focus();
    }
  } finally {
    b.disabled = false;
  }
});
$('#logout').onclick = async () => {
  await api('/logout', 'POST', {});
  await boot();
};
async function refresh() {
  if (refreshing || $('#workspace').hidden) return;
  refreshing = true;
  try {
    data = await api('/dashboard');
    render();
    $('#updated').textContent = '最近同步 ' + new Date().toLocaleTimeString('zh-CN', { hour12: false });
  } catch (e) {
    toast(e.message);
  } finally {
    refreshing = false;
  }
}
function render() {
  const tasks = data.tasks;
  $('#navCount').textContent = tasks.length;
  $('#total').textContent = tasks.length;
  $('#active').textContent = tasks.filter(t => t.enabled).length;
  $('#available').textContent = tasks.filter(t => t.status === 'available').length;
  $('#errors').textContent = tasks.filter(t => ['blocked', 'error'].includes(t.status)).length;
  $('#workerStatus').textContent = data.worker_online ? '后台检查运行中' : '后台检查未运行';
  $('#appVersion').textContent = data.version ? 'v' + data.version : '';
  const update = data.update;
  $('#updateNotice').hidden = !update;
  $('#updateBanner').hidden = !update;
  if (update) {
    $('#updateNotice').textContent =
      '有新版本 v' + update.version + (data.update_can_apply ? ' · 点此更新' : ' · 查看');
    $('#updateBannerText').textContent = '有新版本 v' + update.version + ' 可用。';
  }
  $('#workerWarning').hidden = data.worker_online;
  $('#telegramWarning').hidden = data.notifications_configured;
  renderTasks();
  $('#logs').innerHTML = data.logs.length
    ? data.logs
        .map(
          l =>
            `<div class="logrow"><time>${esc(time(l.created))}</time><strong>${esc(l.task_name || '系统')}</strong><p class="${l.level === 'info' ? '' : 'warning'}">${esc(l.message)}</p></div>`,
        )
        .join('')
    : '<div class="empty"><p>还没有检查记录。添加任务后，点击「检查一次」。</p></div>';
}
function ago(t) {
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 60) return '刚刚';
  if (s < 3600) return Math.floor(s / 60) + ' 分钟前';
  if (s < 86400) return Math.floor(s / 3600) + ' 小时前';
  return Math.floor(s / 86400) + ' 天前';
}
function span(seconds) {
  const s = Math.round(seconds || 0);
  if (s < 60) return s + ' 秒';
  if (s < 3600) return Math.floor(s / 60) + ' 分钟';
  if (s < 86400)
    return Math.floor(s / 3600) + ' 小时' + (s % 3600 >= 60 ? ' ' + Math.floor((s % 3600) / 60) + ' 分' : '');
  return Math.floor(s / 86400) + ' 天';
}
function stockLine(t) {
  const s = t.stock;
  if (!s?.last_restock) return '';
  return `<span class="stockline">上次补货 ${esc(ago(s.last_restock))} · 30 天 ${esc(s.restocks_30d)} 次</span>`;
}
async function openStock(task) {
  if (!task) return;
  $('#stockTitle').textContent = '补货记录 · ' + task.config.name;
  $('#stockSummary').textContent = '正在加载…';
  $('#stockList').innerHTML = '';
  $('#stockDialog').showModal();
  try {
    const h = await api('/tasks/' + task.id + '/stock-history');
    const s = h.summary;
    $('#stockSummary').textContent = s.tracked_since
      ? `从 ${time(s.tracked_since)} 开始记录，共 ${s.restocks_total} 次补货，近 30 天 ${s.restocks_30d} 次` +
        (s.median_seconds ? `；有货时长中位数 ${span(s.median_seconds)}` : '') +
        (s.in_stock_now ? '；当前有货' : '')
      : '还没有记录。任务启用后，每次检查到「售罄」或「有货」都会记录状态变化。';
    $('#stockList').innerHTML = h.restocks.length
      ? h.restocks
          .map(
            r =>
              `<li><time>${esc(time(r.start))}</time><span class="tag ${r.end ? '' : 'available'}">${
                r.end ? '已售罄' : '有货中'
              }</span><strong>持续 ${esc(span(r.seconds))}</strong><span>${esc(r.price ? '页面价格 ' + r.price : '')}</span></li>`,
          )
          .join('')
      : '<li class="historyempty">还没有补货记录（第一次检查到的状态不算补货）</li>';
  } catch (e) {
    $('#stockSummary').textContent = e.message;
  }
}
let updateTimer = null;
const updateStates = {
  queued: '等待开始…',
  running: '正在更新…',
  done: '更新完成',
  failed: '更新失败',
  rolled_back: '已自动退回',
};
function showUpdate(u) {
  const latest = u.latest;
  $('#updateSummary').textContent = latest
    ? `当前版本 v${u.version}，最新版本 v${latest.version}。`
    : `当前版本 v${u.version}，已是最新版本。`;
  $('#updateNotes').href = latest ? latest.url : '#';
  const p = u.progress;
  const recent = p && (['queued', 'running'].includes(p.state) || Date.now() / 1000 - p.time < 600);
  $('#updateProgress').hidden = !recent;
  if (recent) {
    $('#updateProgress').textContent = (updateStates[p.state] || p.state) + (p.message ? '：' + p.message : '');
    $('#updateProgress').className = 'banner' + (p.state === 'done' ? ' ok' : '');
  }
  const working = recent && ['queued', 'running'].includes(p.state);
  $('#updateForm').hidden = !(latest && u.can_apply && !working);
  $('#updateManual').hidden = !(latest && !u.can_apply);
  $('#updateArmed').hidden = !(latest && u.armed);
  $('#updateArmed').textContent = u.armed
    ? `目前有 ${u.armed} 个任务开着自动付款。更新期间（约 1 分钟）这些任务不会下单，建议避开补货时段。`
    : '';
  $('#checkUpdate').disabled = working;
  return working;
}
async function pollUpdate(oldVersion) {
  clearTimeout(updateTimer);
  let u;
  try {
    u = await api('/update');
  } catch (e) {
    updateTimer = setTimeout(() => pollUpdate(oldVersion), 2000); // the service is restarting
    return;
  }
  const working = showUpdate(u);
  if (working) {
    updateTimer = setTimeout(() => pollUpdate(oldVersion), 2000);
  } else if (u.progress?.state === 'done' && u.version !== oldVersion) {
    $('#updateProgress').textContent = `已更新到 v${u.version}，正在刷新页面…`;
    setTimeout(() => location.reload(), 1500);
  }
}
async function openUpdate() {
  $('#updateError').textContent = '';
  $('#updateSummary').textContent = '正在读取…';
  $('#updateDialog').showModal();
  try {
    const u = await api('/update');
    if (showUpdate(u)) pollUpdate(u.version);
  } catch (e) {
    $('#updateSummary').textContent = e.message;
  }
}
$('#openUpdate').onclick = $('#updateNotice').onclick = $('#updateBannerButton').onclick = openUpdate;
$('#closeUpdate').onclick = () => {
  clearTimeout(updateTimer);
  $('#updateDialog').close();
};
$('#checkUpdate').onclick = async () => {
  $('#updateError').textContent = '';
  try {
    const u = await api('/update/check', 'POST', {});
    showUpdate(u);
    refresh();
  } catch (e) {
    $('#updateError').textContent = e.message;
  }
};
$('#updateForm').onsubmit = async e => {
  e.preventDefault();
  $('#updateError').textContent = '';
  if (!confirm('现在更新？更新约 1 分钟，期间页面会短暂断开。')) return;
  const before = data?.version;
  try {
    await api('/update/apply', 'POST', {});
    pollUpdate(before);
  } catch (err) {
    $('#updateError').textContent = err.message;
  }
};
$('#closeStock').onclick = () => $('#stockDialog').close();
$('#exportTasks').onclick = async () => {
  try {
    const body = await api('/tasks/export');
    const blob = new Blob([JSON.stringify(body, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'vps-watch-tasks-' + new Date().toISOString().slice(0, 10) + '.json';
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    toast(`已导出 ${body.tasks.length} 个任务；文件不含密码、推送密钥和购买设置`);
  } catch (e) {
    toast(e.message);
  }
};
$('#importTasks').onclick = () => $('#importFile').click();
$('#importFile').onchange = async e => {
  const file = e.target.files[0];
  e.target.value = '';
  if (!file) return;
  try {
    let body;
    try {
      body = JSON.parse(await file.text());
    } catch {
      throw new Error('文件不是有效的 JSON');
    }
    const r = await api('/tasks/import', 'POST', body);
    toast(`已导入 ${r.imported} 个任务（默认暂停）` + (r.skipped ? `，跳过 ${r.skipped} 个重复任务` : ''));
    await refresh();
  } catch (err) {
    toast(err.message);
  }
};
function renderTaskCards() {
  if (!data) return;
  const q = $('#search').value.toLowerCase();
  const tasks = data.tasks.filter(
    t =>
      (filter === 'all' || (filter === 'running' ? t.enabled : !t.enabled)) &&
      (t.config.name + ' ' + t.config.url).toLowerCase().includes(q),
  );
  $('#empty').hidden = data.tasks.length > 0;
  $('#taskList').innerHTML = tasks
    .map(t => {
      const c = t.config;
      const r = t.result;
      return `<article class="taskcard"><div class="taskmain"><div class="merchant">${esc({ bero: 'B', vmiss: 'V', whmcs: 'H' }[c.adapter] || 'W')}</div><div class="taskinfo"><div class="tasktop"><span class="tasktitle">${esc(c.name)}</span><span class="tag ${esc(t.status)}">${esc(labels[t.status] || t.status)}</span><span class="tag">${t.enabled ? '监控中' : '已暂停'}</span></div><a class="taskurl" target="_blank" rel="noopener noreferrer" href="${esc(c.url)}">${esc(new URL(c.url).hostname)}</a><p class="tasknote">${esc(r?.detail || c.note || '尚未检查；可以先运行单次检查。')}</p></div><div class="price"><strong>${esc(r?.price || '—')} <span>${esc(c.currency)}</span></strong><small>页面上限 ${esc(c.max_price || '—')}</small></div></div><div class="taskbottom"><div class="taskmeta"><span>间隔 ${esc(c.interval)} 秒</span><span>${esc(time(t.last_check))}</span>${stockLine(t)}${t.queued || t.lease > Date.now() / 1000 ? '<span>检查排队 / 执行中</span>' : ''}</div><div class="taskactions"><button data-action="check" data-id="${t.id}">检查一次</button><button data-action="${t.enabled ? 'pause' : 'start'}" data-id="${t.id}">${t.enabled ? '暂停' : '启用'}</button><button data-action="stock" data-id="${t.id}">补货记录</button><button data-action="edit" data-id="${t.id}">编辑</button><button data-action="delete" class="danger" data-id="${t.id}">删除</button></div></div></article>`;
    })
    .join('');
  if (!tasks.length && data.tasks.length)
    $('#taskList').innerHTML = '<div class="empty"><p>没有符合条件的任务。</p></div>';
}
$('#taskList').onclick = async e => {
  const b = e.target.closest('button[data-action]');
  if (!b) return;
  const id = Number(b.dataset.id),
    a = b.dataset.action;
  if (a === 'edit') {
    openTask(data.tasks.find(t => t.id === id));
    return;
  }
  if (a === 'stock') {
    openStock(data.tasks.find(t => t.id === id));
    return;
  }
  if (a === 'delete' && !confirm('删除这个监控任务？')) return;
  b.disabled = true;
  try {
    await api('/tasks/' + id + (a === 'delete' ? '' : '/' + a), a === 'delete' ? 'DELETE' : 'POST', {});
    toast(a === 'check' ? '已加入检查队列；后台会遵守访问间隔。' : a === 'delete' ? '任务已删除' : '任务状态已更新');
    await refresh();
  } catch (e) {
    toast(e.message);
  } finally {
    b.disabled = false;
  }
};
document.querySelectorAll('[data-filter]').forEach(
  b =>
    (b.onclick = () => {
      filter = b.dataset.filter;
      document.querySelectorAll('[data-filter]').forEach(x => x.classList.toggle('selected', x === b));
      renderTasks();
    }),
);
$('#search').oninput = renderTasks;
document.querySelectorAll('[data-view]').forEach(b => (b.onclick = () => switchView(b.dataset.view)));
let settingsGeneration = 0,
  barkSaved = false;
function settingsBusy(busy) {
  document
    .querySelectorAll('#settingsView input,#settingsView select,#settingsView button')
    .forEach(x => (x.disabled = busy));
  if (!busy) $('#testBark').disabled = !barkSaved;
}
async function switchView(view) {
  currentView = view;
  const generation = ++settingsGeneration;
  document.querySelectorAll('.view').forEach(x => (x.hidden = x.id !== view + 'View'));
  document.querySelectorAll('[data-view]').forEach(x => x.classList.toggle('selected', x.dataset.view === view));
  $('#pageName').textContent = {
    tasks: '监控任务',
    logs: '检查日志',
    accounts: '商家账户',
    settings: '通知设置',
    security: '安全',
  }[view];
  if (view === 'accounts') {
    await loadAccount(generation);
    return;
  }
  if (view === 'security') {
    await loadSecurity();
    return;
  }
  if (view !== 'settings') return;
  settingsBusy(true);
  $('#barkState').textContent = '正在加载设置…';
  $('#tokenState').textContent = '正在加载设置…';
  try {
    const [s, b, w] = await Promise.all([api('/settings'), api('/settings/bark'), api('/settings/wechat')]);
    if (generation !== settingsGeneration) return;
    renderWechat(w);
    $('#chatId').value = s.chat_id;
    $('#telegramEnabled').checked = s.enabled;
    $('#tokenState').textContent = s.token_saved
      ? 'Token 已保存 · ' + (s.enabled ? '通知已启用' : '通知已关闭')
      : '尚未配置';
    $('#token').value = '';
    barkSaved = b.key_saved;
    $('#barkServer').value = b.server_url;
    $('#barkKey').value = '';
    $('#barkEnabled').checked = b.url_saved ? b.enabled : true;
    $('#barkLevel').value = b.level;
    $('#barkVolume').value = b.volume;
    $('#barkRepeat').checked = b.repeat;
    $('#barkSound').value = b.sound;
    $('#barkState').textContent = b.url_saved
      ? 'Key 已保存 · ' + (b.enabled ? '通知已启用' : '通知已关闭')
      : '填写设备 Key，保存后测试强提醒';
  } catch (e) {
    if (generation === settingsGeneration) {
      toast(e.message);
      $('#barkState').textContent = '加载失败，请重新进入通知设置';
      $('#tokenState').textContent = '加载失败';
    }
  } finally {
    if (generation === settingsGeneration) settingsBusy(false);
  }
}
function fill(config) {
  const f = $('#taskForm');
  for (const [k, v] of Object.entries(config)) {
    if (f.elements.namedItem(k)) f.elements.namedItem(k).value = v;
  }
  const generic = config.adapter === 'generic',
    bero = config.adapter === 'bero',
    whmcs = config.adapter === 'whmcs';
  $('#genericFields').hidden = !generic;
  $('#beroFields').hidden = !bero;
  $('#whmcsFields').hidden = !whmcs;
  f.elements.max_price.required = !whmcs;
  f.elements.url.readOnly = config.adapter === 'vmiss';
  for (const n of ['scope', 'identity', 'price_selector', 'in_stock', 'out_stock'])
    f.elements.namedItem(n).required = generic;
  for (const n of ['package_id', 'package_name', 'runtime_days']) f.elements.namedItem(n).required = bero;
}
function openTask(task) {
  editing = task?.id || null;
  $('#taskForm').reset();
  $('#formError').textContent = '';
  $('#dialogTitle').textContent = editing ? '编辑监控任务' : '添加监控任务';
  fill(task?.config || data.presets.bero);
  $('#taskDialog').showModal();
}
$('#add').onclick = () => openTask();
$('#emptyAdd').onclick = () => openTask();
$('#closeDialog').onclick = () => $('#taskDialog').close();
$('#adapter').onchange = () => fill(data.presets[$('#adapter').value]);
$('#taskForm').onsubmit = async e => {
  e.preventDefault();
  const b = e.submitter;
  b.disabled = true;
  try {
    const cfg = Object.fromEntries(new FormData(e.target));
    cfg.interval = Number(cfg.interval);
    await api(editing ? '/tasks/' + editing : '/tasks', editing ? 'PUT' : 'POST', cfg);
    $('#taskDialog').close();
    toast('任务已保存，当前为暂停状态。');
    await refresh();
  } catch (e) {
    $('#formError').textContent = e.message;
  } finally {
    b.disabled = false;
  }
};
$('#settingsForm').onsubmit = async e => {
  e.preventDefault();
  const b = e.submitter;
  b.disabled = true;
  try {
    await api('/settings', 'PUT', {
      token: $('#token').value,
      chat_id: $('#chatId').value,
      enabled: $('#telegramEnabled').checked,
    });
    $('#token').value = '';
    toast('Telegram 设置已保存');
    await switchView('settings');
    await refresh();
  } catch (e) {
    toast(e.message);
  } finally {
    b.disabled = false;
  }
};
$('#testTelegram').onclick = async () => {
  const b = $('#testTelegram');
  b.disabled = true;
  try {
    await api('/settings/test', 'POST', {});
    toast('测试消息已发送，请检查 Telegram');
  } catch (e) {
    toast(e.message);
  } finally {
    b.disabled = false;
  }
};
$('#barkForm').onsubmit = async e => {
  e.preventDefault();
  const b = e.submitter;
  b.disabled = true;
  try {
    await api('/settings/bark', 'PUT', {
      server_url: $('#barkServer').value,
      device_key: $('#barkKey').value,
      enabled: $('#barkEnabled').checked,
      level: $('#barkLevel').value,
      volume: Number($('#barkVolume').value),
      sound: $('#barkSound').value,
      repeat: $('#barkRepeat').checked,
    });
    $('#barkKey').value = '';
    toast('Bark 设置已保存');
    await switchView('settings');
    await refresh();
  } catch (e) {
    toast(e.message);
  } finally {
    b.disabled = false;
  }
};
$('#testBark').onclick = async () => {
  const b = $('#testBark');
  b.disabled = true;
  try {
    await api('/settings/bark/test', 'POST', {});
    toast('Bark 服务端已接收测试，请检查手机响铃及重要警告权限');
  } catch (e) {
    toast(e.message);
  } finally {
    b.disabled = false;
  }
};
if (document.modelContext?.registerTool) {
  try {
    document.modelContext.registerTool({
      name: 'list_monitor_tasks',
      description: '读取当前已登录工作台的监控任务，不触发检查或购买',
      inputSchema: { type: 'object', properties: {}, additionalProperties: false },
      annotations: { readOnlyHint: true, untrustedContentHint: true },
      execute: async () => {
        await refresh();
        if ($('#workspace').hidden) throw new Error('请先登录');
        return data.tasks.map(t => ({ id: t.id, name: t.config.name, status: t.status, enabled: !!t.enabled }));
      },
    });
  } catch {}
}
function renderTasks() {
  renderTaskCards();
  document.querySelectorAll('.taskcard').forEach(card => {
    const id = Number(card.querySelector('[data-id]')?.dataset.id);
    const task = data.tasks.find(t => t.id === id);
    if (!task?.checkout_supported) return;
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = '购买设置';
    button.dataset.action = 'purchase';
    button.dataset.id = id;
    card.querySelector('.taskactions').append(button);
    const preflight = document.createElement('button');
    preflight.type = 'button';
    preflight.textContent = '结账预检';
    preflight.dataset.action = 'preflight';
    preflight.dataset.id = id;
    card.querySelector('.taskactions').append(preflight);
    if (task.purchase_policy) {
      const p = task.purchase_policy;
      const badge = document.createElement('span');
      badge.className = 'tag' + (p.status === 'armed' ? ' armed' : ['uncertain'].includes(p.status) ? ' warn' : '');
      badge.textContent =
        { armed: '自动付款已就绪 · ≤ €' + p.max_total, purchased: '已自动下单', uncertain: '下单结果待核实' }[
          p.status
        ] || '购买设置已保存 · 自动付款关闭';
      card.querySelector('.tasktop').append(badge);
    }
  });
}
let purchaseTask = null;
$('#taskList').addEventListener(
  'click',
  e => {
    const b = e.target.closest('[data-action="purchase"],[data-action="preflight"]');
    if (!b) return;
    e.stopImmediatePropagation();
    if (b.dataset.action === 'preflight') {
      openPreflight(Number(b.dataset.id));
      return;
    }
    purchaseTask = Number(b.dataset.id);
    $('#purchaseError').textContent = '';
    const policy = data.tasks.find(t => t.id === purchaseTask)?.purchase_policy;
    showPurchase();
    $('#purchaseDialog').showModal();
  },
  true,
);
$('#closePurchase').onclick = () => $('#purchaseDialog').close();
function purchaseTaskData() {
  return data.tasks.find(t => t.id === purchaseTask);
}
function showPurchase() {
  const task = purchaseTaskData(),
    c = task.config,
    policy = task.purchase_policy;
  $('#purchaseRules').textContent =
    `${c.package_name || c.name} · 1 台 · ${c.runtime_days || '?'} 天\n${c.coupon ? '必须应用优惠码 ' + c.coupon : '不使用优惠码'} · 含税总额不超过下方上限\n成功一次后自动停止，不授权续费。`;
  $('#purchasePayment').value = policy?.payment || 'balance';
  $('#purchaseAutoPreflight').checked = !!policy?.auto_preflight;
  $('#purchaseMaxTotal').value = policy?.max_total || c.max_price;
  showArm(policy);
}
function showArm(policy) {
  const task = purchaseTaskData(),
    c = task.config,
    a = task.purchase_attempt,
    cap = policy?.max_total;
  $('#armHelp').textContent =
    `启用后，监控发现补货会立刻用已登录的会话完成：选择 ${c.package_name || c.name} → ${c.runtime_days || '?'} 天${c.coupon ? ' → ' + c.coupon : ''} → 核对规格与价格（≤ €${cap || '上限'}）→ 核对余额 → 勾选条款 → 下单，订单从账户余额扣款。只会下单一次，结果不明时不会重试。`;
  $('#armConfirmLabel').textContent = cap ? `输入 ${cap} 确认购买上限` : '先保存购买设置';
  $('#armConfirm').placeholder = cap || '';
  $('#armError').textContent = '';
  $('#armConfirm').value = '';
  $('#attemptConfirm').value = '';
  const armed = policy?.status === 'armed';
  $('#armState').textContent = !policy
    ? '请先保存购买设置'
    : armed
      ? '已启用：补货时自动用余额下单一次（上限 €' + policy.max_total + '）'
      : policy.status === 'purchased'
        ? '已自动下单，自动付款已关闭'
        : policy.status === 'uncertain'
          ? '已提交下单但未确认扣款，请到商家网站核实'
          : policy.payment !== 'balance'
            ? '付款方式为人工确认，不会自动下单'
            : '未启用';
  $('#armState').classList.toggle('ok', armed);
  $('#armPurchase').disabled = !policy || armed || policy.payment !== 'balance' || !!a;
  $('#disarmPurchase').disabled = !armed;
  $('#armConfirm').disabled = $('#armPurchase').disabled;
  $('#attemptBox').hidden = !a;
  if (a)
    $('#attemptState').textContent =
      '购买记录：' +
      ({ success: '已下单并确认扣款', uncertain: '已提交，结果未确认', submitting: '正在提交' }[a.state] || a.state) +
      ' · ' +
      time(a.created) +
      (a.order_id ? ' · ' + a.order_id : '');
}
async function armAction(path, body, msg) {
  $('#armError').textContent = '';
  try {
    await api(path, 'POST', body);
    toast(msg);
    await refresh();
    showArm(purchaseTaskData()?.purchase_policy);
  } catch (e) {
    $('#armError').textContent = e.message;
  }
}
$('#armPurchase').onclick = () =>
  armAction(
    '/tasks/' + purchaseTask + '/purchase/arm',
    { confirm_total: $('#armConfirm').value },
    '自动付款已启用：补货时会用余额下单一次',
  );
$('#disarmPurchase').onclick = () => armAction('/tasks/' + purchaseTask + '/purchase/disarm', {}, '自动付款已关闭');
$('#clearAttempt').onclick = () =>
  armAction(
    '/tasks/' + purchaseTask + '/purchase-attempts/clear',
    { confirm: $('#attemptConfirm').value },
    '购买记录已清除',
  );
$('#purchaseForm').onsubmit = async e => {
  e.preventDefault();
  const b = e.submitter;
  b.disabled = true;
  try {
    await api('/tasks/' + purchaseTask + '/purchase-policy', 'PUT', {
      payment: $('#purchasePayment').value,
      auto_preflight: $('#purchaseAutoPreflight').checked,
      max_total: $('#purchaseMaxTotal').value,
    });
    toast('购买设置已保存；如需自动付款，请在下方启用。');
    await refresh();
    showPurchase();
    $('#purchaseDialog').showModal();
  } catch (e) {
    $('#purchaseError').textContent = e.message;
  } finally {
    b.disabled = false;
  }
};
let preflightTask = null,
  preflightGeneration = 0,
  preflightLoading = false;
function showPreflight(job) {
  const states = {
    queued: '已排队，等待后台检查',
    running: '正在商家网站核对',
    done: '已完成',
    error: '未完成',
    outdated: '任务已修改，结果作废',
  };
  $('#preflightState').textContent = job
    ? (job.mode === 'purchase' ? '自动下单 · ' : '结账预检 · ') + (states[job.state] || job.state)
    : '尚未运行预检';
  $('#preflightMessage').textContent =
    job?.result?.message || '先保存购买设置并验证 BERO 登录，再运行预检。此操作会打开正常配置流程，不提交订单或付款。';
  const r = job?.result;
  $('#preflightFacts').textContent = r?.specifications_verified
    ? `套餐：${r.name || ''} · ${r.runtime_days || ''} 天\n概览含税价格：€${r.overview_total_eur}\n优惠码${r.coupon ? ' ' + r.coupon : ''}：${r.coupon ? (r.coupon_accepted ? '已接受' : '未接受') : '未使用'}\n账户余额：${r.balance_verified || r.balance_before ? (r.balance_sufficient === false ? '不足，请充值' : '足够') : '未读取'}\n最终确认页：${r.final_checkout_verified || r.final_total_eur ? '已核实 €' + (r.final_total_eur || '') : '未到达（缺货时正常）'}\n订单：${r.paid ? '已下单并扣款' : r.order_submitted ? '已提交，待核实' : '未提交'}`
    : '';
  $('#preflightJson').value = job ? JSON.stringify(job, null, 2) : '';
  $('#runPreflight').disabled = ['queued', 'running'].includes(job?.state);
  $('#copyPreflight').disabled = !job;
}
async function loadPreflight() {
  if (preflightLoading || !$('#preflightDialog').open) return;
  preflightLoading = true;
  const generation = preflightGeneration,
    id = preflightTask;
  try {
    const [job, runs] = await Promise.all([
      api('/tasks/' + id + '/checkout-preflight'),
      api('/tasks/' + id + '/checkout-history'),
    ]);
    if (generation === preflightGeneration) {
      showPreflight(job);
      showHistory(runs);
    }
  } catch (e) {
    if (generation === preflightGeneration) $('#preflightError').textContent = e.message;
  } finally {
    preflightLoading = false;
  }
}
function showHistory(runs) {
  const statusText = {
    ready: '确认页已核实',
    out_of_stock: '缺货',
    purchased: '已下单',
    uncertain: '结果不确定',
    error: '失败',
    coupon_rejected: '优惠码未生效',
    price_exceeded: '价格超限',
    insufficient_balance: '余额不足',
    unexpected_flow: '流程变化',
    needs_checkout_adapter: '流程变化',
    outdated: '已作废',
  };
  $('#preflightHistory').innerHTML = runs.length
    ? runs
        .map(
          r =>
            `<li class="${r.order_submitted ? 'submitted' : ''}"><time>${esc(time(r.finished))}</time><span class="tag">${
              r.mode === 'purchase' ? '下单' : '预检'
            }</span><strong>${esc(statusText[r.status] || r.status || r.state)}</strong><span>${esc(r.message || '')}</span></li>`,
        )
        .join('')
    : '<li class="historyempty">还没有记录</li>';
}
async function openPreflight(id) {
  preflightTask = id;
  preflightGeneration++;
  $('#preflightError').textContent = '';
  showPreflight(data.tasks.find(t => t.id === id)?.checkout_preflight);
  $('#preflightDialog').showModal();
  await loadPreflight();
}
$('#closePreflight').onclick = () => {
  $('#preflightDialog').close();
  preflightGeneration++;
};
$('#runPreflight').onclick = async () => {
  const b = $('#runPreflight'),
    id = preflightTask,
    generation = preflightGeneration;
  b.disabled = true;
  $('#preflightError').textContent = '';
  try {
    await api('/tasks/' + id + '/checkout-preflight', 'POST', {});
    if (generation === preflightGeneration) {
      showPreflight({ state: 'queued' });
      await loadPreflight();
    }
  } catch (e) {
    if (generation === preflightGeneration) {
      $('#preflightError').textContent = e.message;
      b.disabled = false;
    }
  }
};
$('#copyPreflight').onclick = async () => {
  try {
    await navigator.clipboard.writeText($('#preflightJson').value);
    toast('预检结果已复制；不含密码或登录 Cookie');
  } catch {
    $('#preflightJson').focus();
    $('#preflightJson').select();
    toast('请复制已选中的预检结果');
  }
};
setInterval(() => {
  if (!document.hidden) loadPreflight();
}, 3000);
let beroPasswordSaved = false;
function accountBusy(busy) {
  document.querySelectorAll('#beroAccountForm input,#beroAccountForm button').forEach(x => (x.disabled = busy));
  if (!busy) {
    $('#connectBero').disabled = !beroPasswordSaved;
    $('#disconnectBero').disabled = !beroPasswordSaved;
  }
}
function showAccount(a) {
  beroPasswordSaved = a.password_saved;
  $('#beroEmail').value = a.email;
  $('#beroPassword').value = '';
  $('#beroAutoLogin').checked = a.password_saved ? a.auto_relogin : true;
  const labels = {
    not_configured: '尚未配置',
    credentials_saved: '已保存 · 尚未验证',
    authenticated: '已验证登录',
    expired: '会话已失效',
    login_failed: '登录失败',
    manual_required: '需要人工验证',
    blocked: '访问受限',
    error: '验证异常',
  };
  $('#beroAccountState').textContent = labels[a.status] || a.status;
  $('#beroAccountMessage').textContent = a.message;
  $('#beroVerifiedAt').textContent = a.last_verified
    ? '最近验证：' + time(a.last_verified) + ' · 每30分钟检查一次会话'
    : '尚未确认商家登录；保存凭证后请验证';
}
async function loadAccount(generation) {
  accountBusy(true);
  $('#beroAccountState').textContent = '正在加载账户设置…';
  try {
    const a = await api('/accounts/bero');
    if (generation === settingsGeneration) showAccount(a);
  } catch (e) {
    if (generation === settingsGeneration) toast(e.message);
  } finally {
    if (generation === settingsGeneration) accountBusy(false);
  }
}
$('#beroAccountForm').onsubmit = async e => {
  e.preventDefault();
  accountBusy(true);
  try {
    const a = await api('/accounts/bero', 'PUT', {
      email: $('#beroEmail').value,
      password: $('#beroPassword').value,
      auto_relogin: $('#beroAutoLogin').checked,
    });
    showAccount(a);
    toast('账户设置已保存，请验证登录；未下单或付款');
  } catch (e) {
    toast(e.message);
  } finally {
    accountBusy(false);
  }
};
$('#connectBero').onclick = async () => {
  accountBusy(true);
  $('#beroAccountMessage').textContent = '正在检查保存的会话，必要时使用已保存凭证登录…';
  try {
    const a = await api('/accounts/bero/connect', 'POST', {});
    showAccount(a);
    toast(a.message);
  } catch (e) {
    $('#beroAccountMessage').textContent = e.message;
    toast(e.message);
  } finally {
    accountBusy(false);
  }
};
$('#disconnectBero').onclick = async () => {
  if (!confirm('删除本程序保存的 BERO 密码和登录会话？其他设备不会被注销。')) return;
  accountBusy(true);
  try {
    await api('/accounts/bero', 'DELETE', {});
    showAccount(await api('/accounts/bero'));
    toast('已删除保存的凭证和会话');
  } catch (e) {
    toast(e.message);
  } finally {
    accountBusy(false);
  }
};
$('#purchaseAccountLink').onclick = async () => {
  $('#purchaseDialog').close();
  await switchView('accounts');
};
setInterval(() => {
  if (!document.hidden) refresh();
}, 10000);
boot();

function renderWechat(channels) {
  $('#wechatChannels').innerHTML = Object.entries(channels)
    .map(
      ([name, c]) => `<div class="wechatrow" data-channel="${esc(name)}">
        <div class="wechathead"><strong>${esc(c.label)}</strong><span class="tag ${c.enabled ? 'armed' : ''}">${
          c.saved ? (c.enabled ? '已启用' : '已保存 · 未启用') : '未配置'
        }</span></div>
        <label class="checklabel"><input type="checkbox" data-field="enabled" ${c.enabled || !c.saved ? 'checked' : ''}>启用</label>
        <label>${esc(c.hint)}<input type="password" data-field="token" autocomplete="new-password" maxlength="300" placeholder="${
          c.saved ? '已保存；留空保留' : '粘贴到这里'
        }"></label>
        <div class="actions"><button type="button" class="primary" data-wechat="save">保存</button><button type="button" class="secondary" data-wechat="test" ${
          c.saved ? '' : 'disabled'
        }>发送测试</button>${c.saved ? '<button type="button" class="secondary" data-wechat="delete">删除</button>' : ''}</div>
      </div>`,
    )
    .join('');
}
$('#wechatChannels').addEventListener('click', async e => {
  const b = e.target.closest('[data-wechat]');
  if (!b) return;
  const row = b.closest('[data-channel]'),
    channel = row.dataset.channel,
    action = b.dataset.wechat;
  b.disabled = true;
  try {
    if (action === 'save') {
      renderWechat(
        await api('/settings/wechat', 'PUT', {
          channel,
          token: row.querySelector('[data-field="token"]').value,
          enabled: row.querySelector('[data-field="enabled"]').checked,
        }),
      );
      toast('微信推送设置已保存');
      await refresh();
    } else if (action === 'test') {
      await api('/settings/wechat/test', 'POST', { channel });
      toast('测试消息已发送，请查看微信');
    } else if (action === 'delete' && confirm('删除这个渠道保存的密钥？')) {
      renderWechat(await api('/settings/wechat/' + channel, 'DELETE', {}));
      toast('已删除');
      await refresh();
    }
  } catch (err) {
    toast(err.message);
  } finally {
    b.disabled = false;
  }
});

async function loadSecurity() {
  $('#totpSetup').hidden = true;
  $('#securityError').textContent = '';
  try {
    const s = await api('/security');
    $('#totpState').textContent = s.totp_enabled ? '已启用：登录时需要输入手机验证器中的 6 位验证码' : '未启用';
    $('#totpState').classList.toggle('ok', s.totp_enabled);
    $('#totpStart').hidden = s.totp_enabled;
    $('#totpDisableForm').hidden = !s.totp_enabled;
  } catch (e) {
    $('#securityError').textContent = e.message;
  }
}
$('#totpStart').onclick = async () => {
  $('#securityError').textContent = '';
  try {
    const s = await api('/security/totp/setup', 'POST', {});
    $('#totpQr').src = s.qr;
    $('#totpSecret').textContent = s.secret.replace(/(.{4})/g, '$1 ').trim();
    $('#totpSetup').hidden = false;
    $('#totpStart').hidden = true;
    $('#totpCode').value = '';
    $('#totpCode').focus();
  } catch (e) {
    $('#securityError').textContent = e.message;
  }
};
$('#totpConfirm').onclick = async () => {
  $('#securityError').textContent = '';
  try {
    await api('/security/totp/enable', 'POST', { code: $('#totpCode').value });
    toast('两步验证已启用；下次登录需要验证码');
    await loadSecurity();
  } catch (e) {
    $('#securityError').textContent = e.message;
  }
};
$('#totpDisableForm').onsubmit = async e => {
  e.preventDefault();
  $('#securityError').textContent = '';
  try {
    await api('/security/totp/disable', 'POST', {
      password: $('#totpPassword').value,
      code: $('#totpDisableCode').value,
    });
    $('#totpPassword').value = '';
    $('#totpDisableCode').value = '';
    toast('两步验证已关闭');
    await loadSecurity();
  } catch (e) {
    $('#securityError').textContent = e.message;
  }
};
