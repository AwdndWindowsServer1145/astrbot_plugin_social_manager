const bridge = window.AstrBotPluginPage;
const grid = document.getElementById('grid');
const subtitle = document.getElementById('subtitle');

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2500);
}

function statCard(title, items) {
  const card = document.createElement('div');
  card.className = 'card';
  card.innerHTML = `<h2>${title}</h2>${items.map(i => `<div class="stat"><span class="label">${i.label}</span><span class="value">${i.value}</span></div>`).join('')}`;
  return card;
}

function rankCard(title, items) {
  const card = document.createElement('div');
  card.className = 'card';
  card.innerHTML = `<h2>${title}</h2>${items.length ? items.map((i, idx) =>
    `<div class="rank-item"><span class="pos">#${idx+1}</span><span class="name">${i.uid || 'unknown'}</span><span class="val">${i.val}</span></div>`
  ).join('') : '<div style="color:#888;text-align:center;padding:12px">暂无数据</div>'}`;
  return card;
}

async function loadDashboard() {
  try {
    const ctx = await bridge.ready();
    subtitle.textContent = `插件: ${ctx.displayName || ctx.pluginName} | 页面: ${ctx.pageTitle || ctx.pageName}`;

    const resp = await bridge.apiGet('dashboard_data');
    const d = resp;

    grid.innerHTML = '';

    grid.appendChild(statCard('⚙️ 系统状态', [
      { label: '插件状态', value: `<span class="status-dot on"></span> 运行中` },
      { label: '觉醒群数', value: (d.awake_groups || []).length },
      { label: '持续唤醒群', value: (d.waking_groups || []).length },
      { label: '注册用户', value: d.total_users || 0 },
    ]));

    grid.appendChild(statCard('📊 概览', [
      { label: '好感度排行人数', value: (d.fav_ranking || []).length },
      { label: '金币排行人数', value: (d.bank_ranking || []).length },
      { label: '最高好感度', value: d.fav_ranking?.[0]?.favor ?? '-' },
      { label: '最高金币', value: d.bank_ranking?.[0]?.balance ?? '-' },
    ]));

    grid.appendChild(rankCard('❤️ 好感度排行', (d.fav_ranking || []).map(i => ({ uid: i.uid, val: i.favor + '点' }))));
    grid.appendChild(rankCard('💰 金币排行', (d.bank_ranking || []).map(i => ({ uid: i.uid, val: i.balance + '金币' }))));
  } catch (e) {
    grid.innerHTML = `<div class="card"><h2>❌ 加载失败</h2><p>${e.message || e}</p></div>`;
  }
}

loadDashboard();
