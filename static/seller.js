/* 卖家中心：登录 / 详情展开 / 申诉 / 退出 */
document.addEventListener('DOMContentLoaded', () => {
  const form = document.getElementById('loginForm');
  if (form) {
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const sid = document.getElementById('l-sid').value.trim();
      if (!sid) { alert('请输入学号'); return; }
      const resp = await fetch('/api/session', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ seller_id: sid, seller_name: document.getElementById('l-sname').value.trim() })
      });
      const json = await resp.json();
      if (!json.ok) { alert(json.error || '登录失败'); return; }
      location.reload();
    });
  }
});

function toggleDetail(id) {
  const row = document.getElementById('detail-' + id);
  if (row) row.style.display = row.style.display === 'none' ? '' : 'none';
}

function filterStatus() {
  const v = document.getElementById('f-status').value;
  location.search = v ? '?status=' + encodeURIComponent(v) : '';
}

async function logout() {
  await fetch('/api/session', { method: 'DELETE' });
  location.href = '/seller';
}

async function appeal(itemId, title) {
  const reason = prompt(`对「${title}」发起申诉，请填写申诉理由（不少于 5 字）：`, '');
  if (reason === null) return;
  if (reason.trim().length < 5) { alert('申诉理由请不少于 5 个字'); return; }
  const resp = await fetch('/api/appeals', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ item_id: itemId, reason: reason.trim() })
  });
  const json = await resp.json();
  if (!json.ok) { alert(json.error || '申诉提交失败'); return; }
  alert('申诉已提交，管理员将尽快复核');
  location.reload();
}
