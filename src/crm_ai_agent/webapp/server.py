"""Local, single-user console: a stdlib-only HTTP server (no Flask/FastAPI —
consistent with this project's no-unnecessary-dependencies rule) that wires
together everything already built:

- CrmSessionManager (webapp/crm_session.py) — ONE persistent CRM browser
  session kept alive for the server's whole lifetime (see that module's
  docstring for why: reusing a saved-cookie session in a fresh browser per
  request was found, 2026-09-20, to die far faster than a real user's ~20
  minute session in one continuously-open tab).
- WebitelApiActions.build_plan (real, read-only) to show what a
  provisioning action WOULD do.
- WebitelApiActions.execute_plan (real, WRITE) — the only endpoint in this
  entire project that changes anything in Webitel, and only reachable
  through the UI's explicit two-step "show plan, then confirm" flow.

Runs only on localhost, only for the one operator running it on their own
machine (see docs/open_questions.md's "Обновление после реальной
разведки" — Telegram/multi-user approval was explicitly dropped in favor
of this). Nothing here is meant to be exposed beyond 127.0.0.1.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from crm_ai_agent.webapp.crm_session import CrmSessionManager

ROOT = Path(__file__).resolve().parents[3]
INFO_ENV_PATH = ROOT / "info.env"
WEBITEL_STORAGE_STATE_PATH = ROOT / ".auth" / "webitel_almaty_storage_state.json"


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


_crm_session: CrmSessionManager | None = None


def _get_crm_session() -> CrmSessionManager:
    global _crm_session
    if _crm_session is None:
        env = _load_env_file(INFO_ENV_PATH)
        base_url = env.get("CRM_BASE_URL")
        login = env.get("CRM_LOGIN")
        password = env.get("CRM_PASSWORD")
        if not base_url or not login or not password:
            raise RuntimeError("Missing CRM_BASE_URL / CRM_LOGIN / CRM_PASSWORD in info.env")
        _crm_session = CrmSessionManager(base_url=base_url, login=login, password=password)
    return _crm_session


_INDEX_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Консоль оператора</title>
<style>
  :root{
    --bg:#f7f7f8; --surface:#ffffff; --surface-2:#f7f7f8; --border:#e6e6e9;
    --text:#1c1c1f; --text-soft:#68686f; --text-faint:#9d9da3;
    --accent:#1c1c1f; --accent-dark:#000000; --accent-soft:#f0f0f1; --accent-ink:#ffffff;
    --good:#158548; --good-soft:#e5f6ec; --warn:#b45309; --warn-soft:#fdf1e0;
    --danger:#c0392b; --danger-dark:#a53024; --danger-soft:#fbe9e7;
    --shadow:0 1px 2px rgba(20,20,25,.03), 0 6px 16px -8px rgba(20,20,25,.06);
    --radius:14px;
  }
  *{box-sizing:border-box;}
  html{-webkit-font-smoothing:antialiased;}
  body{
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,system-ui,sans-serif;
    background:var(--bg);color:var(--text);margin:0;padding:32px 24px 64px;font-size:14px;line-height:1.5;
  }
  .page{max-width:760px;margin:0 auto;}
  header.top{margin-bottom:28px;}
  header.top h1{font-size:21px;font-weight:650;margin:0 0 6px;letter-spacing:-.01em;}
  header.top .sub{color:var(--text-soft);font-size:13px;max-width:56ch;}
  .steps{display:flex;flex-direction:column;gap:16px;}
  .card{
    background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
    padding:20px 22px;box-shadow:var(--shadow);
  }
  .card-head{display:flex;align-items:center;gap:10px;margin-bottom:16px;}
  .step-badge{
    display:flex;align-items:center;justify-content:center;flex-shrink:0;
    width:24px;height:24px;border-radius:50%;background:var(--accent-soft);color:var(--accent-dark);
    font-size:12px;font-weight:700;
  }
  .card-head h2{font-size:14.5px;font-weight:600;margin:0;color:var(--text);letter-spacing:0;text-transform:none;}
  .card-body{padding-left:34px;}
  label.field-label{display:block;font-size:12px;font-weight:600;color:var(--text-soft);margin:0 0 5px;}
  .row{display:flex;gap:10px;align-items:flex-end;margin-bottom:12px;flex-wrap:wrap;}
  .row:last-child{margin-bottom:0;}
  .field-group{display:flex;flex-direction:column;flex:1;min-width:200px;}
  .field-group.narrow{flex:none;min-width:64px;width:64px;}
  input[type=text]{
    padding:9px 11px;border:1px solid var(--border);border-radius:8px;font:inherit;
    background:var(--surface);color:var(--text);transition:border-color .12s,box-shadow .12s;
  }
  input[type=text]::placeholder{color:var(--text-faint);}
  input[type=text]:focus{outline:none;border-color:var(--text-soft);box-shadow:0 0 0 3px rgba(28,28,31,.07);}
  textarea{
    padding:9px 11px;border:1px solid var(--border);border-radius:8px;font:inherit;resize:vertical;
    background:var(--surface);color:var(--text);transition:border-color .12s,box-shadow .12s;
  }
  textarea::placeholder{color:var(--text-faint);font-style:italic;}
  textarea:focus{outline:none;border-color:var(--text-soft);box-shadow:0 0 0 3px rgba(28,28,31,.07);}
  .subsection{padding:14px 16px;background:var(--surface-2);border:1px solid var(--border);border-radius:10px;margin-bottom:14px;}
  .subsection:last-of-type{margin-bottom:0;}
  .subsection-title{font-size:11.5px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--text-faint);margin-bottom:12px;}
  .tabs{display:flex;gap:4px;background:var(--surface-2);border:1px solid var(--border);border-radius:9px;padding:3px;margin-bottom:16px;width:fit-content;}
  .tab-btn{background:transparent;color:var(--text-soft);font-weight:600;font-size:13px;padding:6px 14px;border-radius:7px;}
  .tab-btn:hover{background:transparent;color:var(--text);}
  .tab-btn.active{background:var(--surface);color:var(--text);box-shadow:0 1px 2px rgba(20,20,25,.08);}
  .tab-btn.active:hover{background:var(--surface);}
  .hint{font-size:12px;color:var(--text-faint);margin-top:6px;}
  code{background:var(--accent-soft);border-radius:4px;padding:1px 5px;font-size:11.5px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;}
  .checkbox-row{display:flex;align-items:center;gap:7px;font-size:12.5px;color:var(--text-soft);margin-bottom:12px;cursor:pointer;user-select:none;}
  .checkbox-row input[type=checkbox]{width:15px;height:15px;accent-color:var(--accent);cursor:pointer;}
  .list .field.list-error{background:var(--danger-soft);border-color:var(--danger-soft);}
  .list .field.list-error .k{color:var(--danger-dark);}
  .list .field.list-ok .status{color:var(--good);font-weight:700;}
  button{
    padding:9px 16px;border:none;border-radius:8px;background:var(--accent);color:var(--accent-ink);
    font:inherit;font-weight:600;cursor:pointer;transition:background-color .12s,transform .05s;white-space:nowrap;
  }
  button:hover{background:var(--accent-dark);}
  button:active{transform:translateY(1px);}
  button:disabled{background:var(--border);color:var(--text-faint);cursor:not-allowed;transform:none;}
  button.danger{background:var(--danger);}
  button.danger:hover{background:var(--danger-dark);}
  button.ghost{background:transparent;color:var(--accent-dark);border:1px solid var(--border);padding:6px 12px;font-size:12.5px;}
  button.ghost:hover{background:var(--accent-soft);border-color:var(--accent-soft);}
  .pill{display:inline-flex;align-items:center;gap:5px;font-size:11.5px;font-weight:700;padding:4px 10px;border-radius:100px;letter-spacing:.01em;}
  .pill.good{background:var(--good-soft);color:var(--good);}
  .pill.warn{background:var(--warn-soft);color:var(--warn);}
  .pill.critical{background:var(--danger-soft);color:var(--danger-dark);}
  .pill.neutral{background:var(--surface-2);color:var(--text-soft);border:1px solid var(--border);}
  .result-card{background:var(--surface-2);border:1px solid var(--border);border-radius:10px;padding:4px 14px;margin-top:12px;}
  .field{display:flex;justify-content:space-between;gap:12px;padding:9px 0;border-bottom:1px solid var(--border);font-size:13px;}
  .field:last-child{border-bottom:none;}
  .field .k{color:var(--text-soft);flex-shrink:0;}
  .field .v{font-weight:600;text-align:right;}
  .list{display:flex;flex-direction:column;gap:6px;margin-top:10px;}
  .list .field{background:var(--surface-2);border:1px solid var(--border);border-radius:8px;padding:9px 12px;align-items:center;}
  .list .field .k{font-weight:500;color:var(--text);}
  pre{background:var(--surface-2);border:1px solid var(--border);border-radius:8px;padding:11px 13px;font-size:12px;overflow-x:auto;white-space:pre-wrap;word-break:break-word;}
  .msg{font-size:12.5px;margin-top:10px;color:var(--text-soft);}
  .msg:empty{margin-top:0;}
  .msg.error{color:var(--danger-dark);}
  .msg.info{color:var(--text-soft);}
  [hidden]{display:none!important;}
</style>
</head>
<body>
<div class="page">
  <header class="top">
    <h1>Консоль оператора</h1>
    <div class="sub">Локальный инструмент. Только на этом компьютере. Каждое реальное изменение требует отдельного подтверждения.</div>
  </header>

  <div class="steps">

  <div class="card">
    <div class="card-head"><span class="step-badge">1</span><h2>Вход в CRM</h2></div>
    <div class="card-body">
      <div class="row">
        <button id="btnLogin">Войти в CRM</button>
        <span id="loginStatus" class="pill neutral">не проверено</span>
      </div>
      <div id="loginMsg" class="msg info">Откроется окно браузера — войдите там как обычно (логин/пароль подставятся сами, код подтверждения — вручную).</div>
    </div>
  </div>

  <div class="card">
    <div class="card-head"><span class="step-badge">2</span><h2>Заявка</h2></div>
    <div class="card-body">
      <div class="row">
        <button id="btnPendingTickets" class="ghost">Обновить список ожидающих</button>
      </div>
      <div id="pendingMsg" class="msg info"></div>
      <div id="pendingList" class="list"></div>
      <div class="row">
        <div class="field-group">
          <label class="field-label" for="ticketUrl">Ссылка на заявку (CRM)</label>
          <input type="text" id="ticketUrl" placeholder="https://crm.astana-motors.kz/...">
        </div>
        <button id="btnReadTicket">Открыть</button>
      </div>
      <div id="ticketMsg" class="msg info"></div>
      <div id="ticketCard" class="result-card" hidden></div>
    </div>
  </div>

  <div class="card">
    <div class="card-head"><span class="step-badge">3</span><h2>Webitel: копирование ролей/лицензии/группы по образцу</h2></div>
    <div class="card-body">
      <div class="row">
        <div class="field-group">
          <label class="field-label" for="templateExt">Логин пользователя-образца</label>
          <input type="text" id="templateExt" placeholder="напр. 10078">
        </div>
        <div class="field-group">
          <label class="field-label" for="targetExt">Логин(ы) назначения (можно несколько через запятую)</label>
          <input type="text" id="targetExt" placeholder="напр. 10079, 10080">
        </div>
        <button id="btnPlan">Показать план</button>
      </div>
      <label class="checkbox-row">
        <input type="checkbox" id="resetPasswordCheck">
        Также сбросить пароль (сгенерировать новый)
      </label>
      <div id="planMsg" class="msg info"></div>
      <div id="planList" class="list"></div>
      <div class="row" id="confirmRow" hidden>
        <button id="btnExecute" class="danger">Подтвердить и применить все</button>
      </div>
      <div id="executeMsg" class="msg info"></div>
    </div>
  </div>

  <div class="card">
    <div class="card-head"><span class="step-badge">4</span><h2>Webitel: создание нового пользователя</h2></div>
    <div class="card-body">

      <div class="tabs">
        <button type="button" class="tab-btn active" data-tab="single">Один пользователь</button>
        <button type="button" class="tab-btn" data-tab="bulk">Массово</button>
      </div>

      <div class="tab-panel" data-panel="single">
        <div class="subsection">
          <div class="subsection-title">Найти образец по соседним номерам (необязательно)</div>
          <div class="row">
            <div class="field-group">
              <label class="field-label" for="createNewLogin">Номер, рядом с которым искать</label>
              <input type="text" id="createNewLogin" placeholder="напр. 10087">
            </div>
            <div class="field-group narrow">
              <label class="field-label" for="neighborsRadius">±</label>
              <input type="text" id="neighborsRadius" value="2">
            </div>
            <button id="btnNeighbors" class="ghost">Показать соседей</button>
          </div>
          <div id="neighborsMsg" class="msg info"></div>
          <div id="neighborsList" class="list"></div>
        </div>

        <div class="row">
          <div class="field-group">
            <label class="field-label" for="singleTemplateExt">Логин пользователя-образца</label>
            <input type="text" id="singleTemplateExt" placeholder="напр. 10078">
          </div>
          <div class="field-group">
            <label class="field-label" for="singleNewLogin">Логин нового пользователя</label>
            <input type="text" id="singleNewLogin" placeholder="напр. 10087">
          </div>
        </div>
        <div class="row">
          <div class="field-group">
            <label class="field-label" for="singleNewName">ФИО нового пользователя</label>
            <input type="text" id="singleNewName" placeholder="Имя Фамилия">
          </div>
          <button id="btnCreatePlanSingle">Показать план</button>
        </div>
        <div id="createPlanMsgSingle" class="msg info"></div>
        <div id="createPlanCardSingle" class="result-card" hidden></div>
        <div class="row" id="createConfirmRowSingle" hidden>
          <button id="btnCreateExecuteSingle" class="danger">Подтвердить и создать</button>
        </div>
        <div id="createExecuteMsgSingle" class="msg info"></div>
      </div>

      <div class="tab-panel" data-panel="bulk" hidden>
        <div class="row">
          <div class="field-group">
            <label class="field-label" for="createTemplateExt">Логин пользователя-образца (общий для всех ниже)</label>
            <input type="text" id="createTemplateExt" placeholder="напр. 10078">
          </div>
        </div>
        <div class="row">
          <div class="field-group">
            <label class="field-label" for="createBulkList">Новые пользователи (по одному на строку)</label>
            <textarea id="createBulkList" rows="4" placeholder="10087,Иван Иванов&#10;10097,Мария Петрова"></textarea>
            <div class="hint">Формат строки: <code>логин,ФИО</code></div>
          </div>
        </div>
        <div class="row">
          <button id="btnCreatePlan">Показать план для всех</button>
        </div>
        <div id="createPlanMsg" class="msg info"></div>
        <div id="createPlanList" class="list"></div>
        <div class="row" id="createConfirmRow" hidden>
          <button id="btnCreateExecute" class="danger">Подтвердить и создать все</button>
        </div>
        <div id="createExecuteMsg" class="msg info"></div>
      </div>

    </div>
  </div>

  <div class="card">
    <div class="card-head"><span class="step-badge">5</span><h2>Webitel: удаление пользователей</h2></div>
    <div class="card-body">
      <div class="row">
        <div class="field-group">
          <label class="field-label" for="deleteExt">Логин/добавочный (можно несколько через запятую)</label>
          <input type="text" id="deleteExt" placeholder="напр. 10087, 10097">
        </div>
        <button id="btnDeletePlan">Показать план</button>
      </div>
      <div id="deletePlanMsg" class="msg info"></div>
      <div id="deletePlanList" class="list"></div>
      <div class="row" id="deleteConfirmRow" hidden>
        <button id="btnDeleteExecute" class="danger">Подтвердить и удалить все</button>
      </div>
      <div id="deleteExecuteMsg" class="msg info"></div>
    </div>
  </div>

  </div>
</div>

<script>
async function api(path, body) {
  const res = await fetch(path, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
  return data;
}

const loginStatusEl = document.getElementById('loginStatus');
const loginMsgEl = document.getElementById('loginMsg');

document.getElementById('btnLogin').addEventListener('click', async () => {
  loginStatusEl.textContent = 'запускаю...';
  loginStatusEl.className = 'pill warn';
  loginMsgEl.textContent = 'Открывается окно браузера...';
  try {
    await api('/api/crm/login', {});
    pollLoginStatus();
  } catch (e) {
    loginMsgEl.textContent = 'Ошибка: ' + e.message;
    loginMsgEl.className = 'msg error';
  }
});

async function pollLoginStatus() {
  for (let i = 0; i < 300; i++) {
    await new Promise(r => setTimeout(r, 2000));
    try {
      const data = await api('/api/crm/login/status', {});
      if (data.ready) {
        loginStatusEl.textContent = 'вход выполнен';
        loginStatusEl.className = 'pill good';
        loginMsgEl.textContent = '';
        return;
      }
    } catch (e) { /* keep polling */ }
  }
  loginStatusEl.textContent = 'не подтверждено';
  loginStatusEl.className = 'pill warn';
}

document.getElementById('btnPendingTickets').addEventListener('click', async () => {
  const msgEl = document.getElementById('pendingMsg');
  const listEl = document.getElementById('pendingList');
  listEl.innerHTML = '';
  msgEl.className = 'msg info';
  msgEl.textContent = 'Загружаю список (только чтение)...';
  try {
    const data = await api('/api/crm/pending_tickets', {});
    if (data.tickets.length === 0) {
      msgEl.textContent = 'Ожидающих заявок "Доступ к ИС" нет.';
      return;
    }
    msgEl.textContent = 'Найдено: ' + data.tickets.length + ' — нажмите, чтобы открыть:';
    listEl.innerHTML = data.tickets.map(t =>
      `<div class="field"><span class="k">${t.name}</span>` +
      `<button type="button" class="open-pending ghost" data-url="${t.url}">Открыть</button></div>`
    ).join('');
    listEl.querySelectorAll('.open-pending').forEach(btn => {
      btn.addEventListener('click', () => {
        document.getElementById('ticketUrl').value = btn.dataset.url;
        document.getElementById('btnReadTicket').click();
      });
    });
  } catch (e) {
    msgEl.className = 'msg error';
    msgEl.textContent = 'Ошибка: ' + e.message;
  }
});

document.getElementById('btnReadTicket').addEventListener('click', async () => {
  const url = document.getElementById('ticketUrl').value.trim();
  const msgEl = document.getElementById('ticketMsg');
  const cardEl = document.getElementById('ticketCard');
  cardEl.hidden = true;
  msgEl.className = 'msg info';
  msgEl.textContent = 'Читаю заявку...';
  try {
    const data = await api('/api/crm/ticket', {url});
    msgEl.textContent = '';
    cardEl.innerHTML = `
      <div class="field"><span class="k">Статус</span><span class="v">${data.status}</span></div>
      <div class="field"><span class="k">ФИО сотрудника</span><span class="v">${data.employee_full_name}</span></div>
      <div class="field"><span class="k">Учётная запись</span><span class="v">${data.employee_account}</span></div>
      <div class="field"><span class="k">Информационная система</span><span class="v">${data.information_system}</span></div>
      <div class="field"><span class="k">Комментарии</span><span class="v">${data.comment || '—'}</span></div>
    `;
    cardEl.hidden = false;
    if (data.employee_account) {
      document.getElementById('targetExt').value = data.employee_account;
    }
  } catch (e) {
    msgEl.textContent = 'Ошибка: ' + e.message;
    msgEl.className = 'msg error';
  }
});

let currentPlans = [];  // [{ok: true, plan} | {ok: false, target, error}]

document.getElementById('btnPlan').addEventListener('click', async () => {
  const template_extension = document.getElementById('templateExt').value.trim();
  const targets = document.getElementById('targetExt').value
    .split(',')
    .map(x => x.trim())
    .filter(x => x.length > 0);
  const msgEl = document.getElementById('planMsg');
  const listEl = document.getElementById('planList');
  const confirmRow = document.getElementById('confirmRow');
  listEl.innerHTML = '';
  confirmRow.hidden = true;
  currentPlans = [];

  if (!template_extension) {
    msgEl.className = 'msg error';
    msgEl.textContent = 'Укажите логин пользователя-образца';
    return;
  }
  if (targets.length === 0) {
    msgEl.className = 'msg error';
    msgEl.textContent = 'Укажите хотя бы один логин назначения';
    return;
  }

  msgEl.className = 'msg info';
  msgEl.textContent = `Строю план для ${targets.length} — только чтение...`;

  for (const target_extension of targets) {
    try {
      const plan = await api('/api/webitel/plan', {template_extension, target_extension});
      currentPlans.push({ok: true, plan});
    } catch (e) {
      currentPlans.push({ok: false, target: target_extension, error: e.message});
    }
  }

  const okCount = currentPlans.filter(r => r.ok).length;
  msgEl.textContent = `Готово: ${okCount} из ${targets.length} — план построен. Ошибки ниже, если есть.`;
  listEl.innerHTML = currentPlans.map(r => {
    if (r.ok) {
      const roleNames = r.plan.roles.map(x => x.name).join(', ') || '—';
      const licenseNames = r.plan.license.map(x => x.prod || x.id).join(', ') || '—';
      return `<div class="field list-ok"><span class="k">${r.plan.target_name} (${r.plan.target_extension})</span>` +
        `<span class="v status">✓ роли: ${roleNames} · лицензии: ${licenseNames} · группа: ${r.plan.group || '—'}</span></div>`;
    }
    return `<div class="field list-error"><span class="k">${r.target}</span><span class="v">${r.error}</span></div>`;
  }).join('');

  if (okCount > 0) confirmRow.hidden = false;
});

document.getElementById('btnExecute').addEventListener('click', async () => {
  const toApply = currentPlans.filter(r => r.ok);
  if (toApply.length === 0) return;
  const msgEl = document.getElementById('executeMsg');
  const btn = document.getElementById('btnExecute');
  const names = toApply.map(r => r.plan.target_name + ' (' + r.plan.target_extension + ')').join(', ');
  if (!confirm(`Точно применить план к ${toApply.length} пользователям? ${names}`)) return;
  btn.disabled = true;
  msgEl.className = 'msg info';
  msgEl.textContent = 'Применяю...';
  const results = [];
  for (const r of toApply) {
    try {
      await api('/api/webitel/execute', {plan: r.plan});
      results.push(`<div class="field list-ok"><span class="k">${r.plan.target_name} (${r.plan.target_extension})</span>` +
        `<span class="v status">✓ обновлён</span></div>`);
    } catch (e) {
      results.push(`<div class="field list-error"><span class="k">${r.plan.target_name} (${r.plan.target_extension})</span>` +
        `<span class="v">${e.message}</span></div>`);
    }
  }
  msgEl.className = 'msg info';
  msgEl.innerHTML = 'Готово.<div class="list">' + results.join('') + '</div>';
  btn.disabled = false;
});

let currentDeletePlans = [];  // [{ok: true, plan} | {ok: false, extension, error}]

document.getElementById('btnDeletePlan').addEventListener('click', async () => {
  const extensions = document.getElementById('deleteExt').value
    .split(',')
    .map(x => x.trim())
    .filter(x => x.length > 0);
  const msgEl = document.getElementById('deletePlanMsg');
  const listEl = document.getElementById('deletePlanList');
  const confirmRow = document.getElementById('deleteConfirmRow');
  listEl.innerHTML = '';
  confirmRow.hidden = true;
  currentDeletePlans = [];

  if (extensions.length === 0) {
    msgEl.className = 'msg error';
    msgEl.textContent = 'Укажите хотя бы один логин/добавочный';
    return;
  }

  msgEl.className = 'msg info';
  msgEl.textContent = `Ищу ${extensions.length} — только чтение...`;

  for (const extension of extensions) {
    try {
      const plan = await api('/api/webitel/delete_plan', {extension});
      currentDeletePlans.push({ok: true, plan});
    } catch (e) {
      currentDeletePlans.push({ok: false, extension, error: e.message});
    }
  }

  const okCount = currentDeletePlans.filter(r => r.ok).length;
  msgEl.textContent = `Найдено: ${okCount} из ${extensions.length}. Ошибки ниже, если есть.`;
  listEl.innerHTML = currentDeletePlans.map(r => {
    if (r.ok) {
      const deviceIds = r.plan.device_ids || [];
      const deviceNote = deviceIds.length > 0 ? `, устройств: ${deviceIds.length}` : '';
      const presenceNote = r.plan.presence_status ? `, активная сессия (будет завершена)` : '';
      return `<div class="field list-ok"><span class="k">${r.plan.name} (${r.plan.username || r.plan.extension})</span>` +
        `<span class="v status">✓ группа: ${r.plan.group || '—'}${deviceNote}${presenceNote}</span></div>`;
    }
    return `<div class="field list-error"><span class="k">${r.extension}</span><span class="v">${r.error}</span></div>`;
  }).join('');

  if (okCount > 0) confirmRow.hidden = false;
});

document.getElementById('btnDeleteExecute').addEventListener('click', async () => {
  const toDelete = currentDeletePlans.filter(r => r.ok);
  if (toDelete.length === 0) return;
  const msgEl = document.getElementById('deleteExecuteMsg');
  const btn = document.getElementById('btnDeleteExecute');
  const names = toDelete.map(r => r.plan.name + ' (' + r.plan.extension + ')').join(', ');
  if (!confirm(`УДАЛИТЬ НАВСЕГДА ${toDelete.length} пользователей? ${names}`)) return;
  btn.disabled = true;
  msgEl.className = 'msg info';
  msgEl.textContent = 'Удаляю...';
  const results = [];
  for (const r of toDelete) {
    try {
      const result = await api('/api/webitel/delete_execute', {plan: r.plan});
      const deviceNote = result.deleted_device_ids.length > 0 ? ` (устройств удалено: ${result.deleted_device_ids.length})` : '';
      results.push(`<div class="field list-ok"><span class="k">${result.name} (${result.extension})</span>` +
        `<span class="v status">✓ удалён${deviceNote}</span></div>`);
    } catch (e) {
      results.push(`<div class="field list-error"><span class="k">${r.plan.name} (${r.plan.extension})</span>` +
        `<span class="v">${e.message}</span></div>`);
    }
  }
  msgEl.className = 'msg info';
  msgEl.innerHTML = 'Готово.<div class="list">' + results.join('') + '</div>';
  btn.disabled = false;
});

document.querySelectorAll('.tab-btn').forEach(tabBtn => {
  tabBtn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    tabBtn.classList.add('active');
    const target = tabBtn.dataset.tab;
    document.querySelectorAll('.tab-panel').forEach(panel => {
      panel.hidden = panel.dataset.panel !== target;
    });
  });
});

let currentCreatePlanSingle = null;

document.getElementById('btnCreatePlanSingle').addEventListener('click', async () => {
  const template_extension = document.getElementById('singleTemplateExt').value.trim();
  const new_username = document.getElementById('singleNewLogin').value.trim();
  const new_name = document.getElementById('singleNewName').value.trim();
  const new_extension = new_username;
  const msgEl = document.getElementById('createPlanMsgSingle');
  const cardEl = document.getElementById('createPlanCardSingle');
  const confirmRow = document.getElementById('createConfirmRowSingle');
  cardEl.hidden = true;
  confirmRow.hidden = true;
  currentCreatePlanSingle = null;
  msgEl.className = 'msg info';
  msgEl.textContent = 'Строю план (только чтение)...';
  try {
    const plan = await api('/api/webitel/create_plan', {template_extension, new_username, new_name, new_extension});
    currentCreatePlanSingle = plan;
    msgEl.textContent = '';
    const roleNames = plan.roles.map(r => r.name).join(', ') || '—';
    const licenseIds = plan.license.map(l => l.id).join(', ') || '—';
    cardEl.innerHTML = `
      <div class="field"><span class="k">Образец</span><span class="v">${plan.template_name} (${plan.template_extension})</span></div>
      <div class="field"><span class="k">Новый пользователь</span><span class="v">${plan.new_name} (${plan.new_username})</span></div>
      <div class="field"><span class="k">Roles → будет</span><span class="v">${roleNames}</span></div>
      <div class="field"><span class="k">License → будет</span><span class="v">${licenseIds}</span></div>
      <div class="field"><span class="k">Group → будет</span><span class="v">${plan.group || '—'}</span></div>
      <div class="field"><span class="k">Устройство (SIP)</span><span class="v">${plan.new_extension} — создастся вместе с пользователем</span></div>
      <div class="field"><span class="k">Пароль</span><span class="v">сгенерируется при создании, постоянный (без принудительной смены)</span></div>
    `;
    cardEl.hidden = false;
    confirmRow.hidden = false;
  } catch (e) {
    msgEl.textContent = 'Ошибка: ' + e.message;
    msgEl.className = 'msg error';
  }
});

document.getElementById('btnCreateExecuteSingle').addEventListener('click', async () => {
  if (!currentCreatePlanSingle) return;
  const msgEl = document.getElementById('createExecuteMsgSingle');
  const btn = document.getElementById('btnCreateExecuteSingle');
  if (!confirm('Точно создать нового пользователя ' + currentCreatePlanSingle.new_name + ' (' + currentCreatePlanSingle.new_username + ')?')) return;
  btn.disabled = true;
  msgEl.className = 'msg info';
  msgEl.textContent = 'Создаю...';
  try {
    const result = await api('/api/webitel/create_execute', {plan: currentCreatePlanSingle});
    msgEl.className = 'msg info';
    msgEl.innerHTML = `Готово. ${result.name} (${result.extension}) создан. Временный пароль: <b>${result.temporary_password}</b>`;
  } catch (e) {
    msgEl.textContent = 'Ошибка: ' + e.message;
    msgEl.className = 'msg error';
  } finally {
    btn.disabled = false;
  }
});

document.getElementById('btnNeighbors').addEventListener('click', async () => {
  const extension = document.getElementById('createNewLogin').value.trim();
  const msgEl = document.getElementById('neighborsMsg');
  const listEl = document.getElementById('neighborsList');
  listEl.innerHTML = '';
  if (!extension) {
    msgEl.className = 'msg error';
    msgEl.textContent = 'Сначала укажите логин/добавочный нового пользователя';
    return;
  }
  const radius = parseInt(document.getElementById('neighborsRadius').value, 10) || 2;
  msgEl.className = 'msg info';
  msgEl.textContent = 'Ищу соседние номера (только чтение)...';
  try {
    const data = await api('/api/webitel/neighbors', {extension, radius});
    if (data.neighbors.length === 0) {
      msgEl.textContent = 'Соседних номеров не найдено — выберите образец вручную ниже.';
      return;
    }
    msgEl.textContent = 'Найдено — нажмите, чтобы использовать как образец (номер комнаты/неподходящего пропустите):';
    listEl.innerHTML = data.neighbors.map(n =>
      `<div class="field"><span class="k">${n.extension} — ${n.name}${n.group ? ' (' + n.group + ')' : ''}</span>` +
      `<button type="button" class="pick-neighbor ghost" data-ext="${n.extension}">Взять как образец</button></div>`
    ).join('');
    listEl.querySelectorAll('.pick-neighbor').forEach(btn => {
      btn.addEventListener('click', () => {
        document.getElementById('singleTemplateExt').value = btn.dataset.ext;
      });
    });
  } catch (e) {
    msgEl.className = 'msg error';
    msgEl.textContent = 'Ошибка: ' + e.message;
  }
});

let currentCreatePlans = [];  // [{ok: true, plan} | {ok: false, login, error}]

function parseBulkList() {
  const raw = document.getElementById('createBulkList').value;
  return raw.split('\\n')
    .map(line => line.trim())
    .filter(line => line.length > 0)
    .map(line => {
      const [login, ...rest] = line.split(',');
      return {login: (login || '').trim(), name: rest.join(',').trim()};
    });
}

document.getElementById('btnCreatePlan').addEventListener('click', async () => {
  const template_extension = document.getElementById('createTemplateExt').value.trim();
  const entries = parseBulkList();
  const msgEl = document.getElementById('createPlanMsg');
  const listEl = document.getElementById('createPlanList');
  const confirmRow = document.getElementById('createConfirmRow');
  listEl.innerHTML = '';
  confirmRow.hidden = true;
  currentCreatePlans = [];

  if (!template_extension) {
    msgEl.className = 'msg error';
    msgEl.textContent = 'Укажите логин пользователя-образца';
    return;
  }
  if (entries.length === 0) {
    msgEl.className = 'msg error';
    msgEl.textContent = 'Добавьте хотя бы одного пользователя (логин,ФИО на строку)';
    return;
  }

  msgEl.className = 'msg info';
  msgEl.textContent = `Строю план для ${entries.length} — только чтение...`;

  for (const entry of entries) {
    try {
      const plan = await api('/api/webitel/create_plan', {
        template_extension, new_username: entry.login, new_name: entry.name, new_extension: entry.login,
      });
      currentCreatePlans.push({ok: true, plan});
    } catch (e) {
      currentCreatePlans.push({ok: false, login: entry.login, error: e.message});
    }
  }

  const okCount = currentCreatePlans.filter(r => r.ok).length;
  msgEl.textContent = `Готово: ${okCount} из ${entries.length} — план построен. Ошибки ниже, если есть.`;
  listEl.innerHTML = currentCreatePlans.map(r => {
    if (r.ok) {
      const roleNames = r.plan.roles.map(x => x.name).join(', ') || '—';
      const licenseNames = r.plan.license.map(x => x.prod || x.id).join(', ') || '—';
      return `<div class="field list-ok">` +
        `<span class="k">${r.plan.new_name} (${r.plan.new_username})</span>` +
        `<span class="v status">✓ роли: ${roleNames} · лицензии: ${licenseNames} · группа: ${r.plan.group || '—'}</span></div>`;
    }
    return `<div class="field list-error"><span class="k">${r.login}</span><span class="v">${r.error}</span></div>`;
  }).join('');

  if (okCount > 0) confirmRow.hidden = false;
});

document.getElementById('btnCreateExecute').addEventListener('click', async () => {
  const toCreate = currentCreatePlans.filter(r => r.ok);
  if (toCreate.length === 0) return;
  const msgEl = document.getElementById('createExecuteMsg');
  const btn = document.getElementById('btnCreateExecute');
  const names = toCreate.map(r => r.plan.new_name + ' (' + r.plan.new_username + ')').join(', ');
  if (!confirm(`Точно создать ${toCreate.length} пользователей? ${names}`)) return;
  btn.disabled = true;
  msgEl.className = 'msg info';
  msgEl.textContent = 'Создаю...';
  const results = [];
  for (const r of toCreate) {
    try {
      const result = await api('/api/webitel/create_execute', {plan: r.plan});
      results.push(`<div class="field list-ok"><span class="k">${r.plan.new_name} (${r.plan.new_username})</span>` +
        `<span class="v status">✓ пароль: ${result.temporary_password}</span></div>`);
    } catch (e) {
      results.push(`<div class="field list-error"><span class="k">${r.plan.new_name} (${r.plan.new_username})</span>` +
        `<span class="v">${e.message}</span></div>`);
    }
  }
  msgEl.className = 'msg info';
  msgEl.innerHTML = 'Готово.<div class="list">' + results.join('') + '</div>';
  btn.disabled = false;
});
</script>
</body>
</html>
"""


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length == 0:
        return {}
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    return json.loads(raw)


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.0 (this class's default) plus rapid successive local requests
    # triggered occasional connection resets in testing on Windows;
    # HTTP/1.1 with the explicit Content-Length every response already
    # sends is the standard fix for that class of http.server flakiness.
    protocol_version = "HTTP/1.1"

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # keep stdout free of PII-bearing request logs; see artifacts.py's same posture

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/":
            self._send_html(_INDEX_HTML)
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        # Always drain the request body first, regardless of which path
        # branch below actually needs it. Responding without reading a
        # POST body the client already sent leaves unread bytes in the
        # socket's receive buffer, which on Windows can turn the next
        # connection close into a TCP RST instead of a clean FIN —
        # confirmed via test_webapp_server.py's intermittent
        # ConnectionResetError on exactly the two branches that used to
        # skip this (login/status and the unknown-path fallback).
        try:
            body = _read_json_body(self)
        except Exception as exc:  # noqa: BLE001
            self._send_json(400, {"error": f"invalid request body: {exc}"})
            return

        try:
            if self.path == "/api/crm/login":
                _get_crm_session().start_login()
                self._send_json(200, {"started": True})
                return

            if self.path == "/api/crm/login/status":
                self._send_json(200, {"ready": _get_crm_session().is_logged_in()})
                return

            if self.path == "/api/crm/pending_tickets":
                env = _load_env_file(INFO_ENV_PATH)
                visa_owner_id = env.get("CRM_VISA_OWNER_ID")
                if not visa_owner_id:
                    self._send_json(400, {"error": "Missing CRM_VISA_OWNER_ID in info.env"})
                    return
                tickets = _get_crm_session().list_pending_tickets(visa_owner_id)
                self._send_json(200, {"tickets": tickets})
                return

            if self.path == "/api/crm/ticket":
                url = body.get("url", "").strip()
                if not url:
                    self._send_json(400, {"error": "url is required"})
                    return
                ticket = _get_crm_session().read_ticket(url)
                self._send_json(200, ticket)
                return

            if self.path == "/api/webitel/plan":
                template_extension = body.get("template_extension", "").strip()
                target_extension = body.get("target_extension", "").strip()
                if not template_extension or not target_extension:
                    self._send_json(400, {"error": "template_extension and target_extension are required"})
                    return
                from crm_ai_agent.adapters.webitel_api.actions import WebitelApiActions

                actions = WebitelApiActions.from_info_env(INFO_ENV_PATH, WEBITEL_STORAGE_STATE_PATH)
                plan = actions.build_plan(template_extension=template_extension, target_extension=target_extension)
                self._send_json(200, plan)
                return

            if self.path == "/api/webitel/execute":
                plan = body.get("plan")
                if not plan:
                    self._send_json(400, {"error": "plan is required"})
                    return
                from crm_ai_agent.adapters.webitel_api.actions import WebitelApiActions

                actions = WebitelApiActions.from_info_env(INFO_ENV_PATH, WEBITEL_STORAGE_STATE_PATH)
                result = actions.execute_plan(plan)
                self._send_json(200, result)
                return

            if self.path == "/api/webitel/delete_plan":
                extension = body.get("extension", "").strip()
                if not extension:
                    self._send_json(400, {"error": "extension is required"})
                    return
                from crm_ai_agent.adapters.webitel_api.actions import WebitelApiActions

                actions = WebitelApiActions.from_info_env(INFO_ENV_PATH, WEBITEL_STORAGE_STATE_PATH)
                plan = actions.build_delete_plan(extension)
                self._send_json(200, plan)
                return

            if self.path == "/api/webitel/delete_execute":
                plan = body.get("plan")
                if not plan:
                    self._send_json(400, {"error": "plan is required"})
                    return
                from crm_ai_agent.adapters.webitel_api.actions import WebitelApiActions

                actions = WebitelApiActions.from_info_env(INFO_ENV_PATH, WEBITEL_STORAGE_STATE_PATH)
                result = actions.execute_delete_plan(plan)
                self._send_json(200, result)
                return

            if self.path == "/api/webitel/neighbors":
                extension = body.get("extension", "").strip()
                if not extension:
                    self._send_json(400, {"error": "extension is required"})
                    return
                try:
                    radius = int(body.get("radius", 2))
                except (TypeError, ValueError):
                    self._send_json(400, {"error": "radius must be an integer"})
                    return
                if radius < 1 or radius > 50:
                    self._send_json(400, {"error": "radius must be between 1 and 50"})
                    return
                from crm_ai_agent.adapters.webitel_api.actions import WebitelApiActions

                actions = WebitelApiActions.from_info_env(INFO_ENV_PATH, WEBITEL_STORAGE_STATE_PATH)
                neighbors = actions.find_neighbors(extension, radius=radius)
                self._send_json(200, {"neighbors": neighbors})
                return

            if self.path == "/api/webitel/create_plan":
                template_extension = body.get("template_extension", "").strip()
                new_username = body.get("new_username", "").strip()
                new_name = body.get("new_name", "").strip()
                new_extension = body.get("new_extension", "").strip()
                if not template_extension or not new_username or not new_name or not new_extension:
                    self._send_json(400, {"error": "template_extension, new_username, new_name and new_extension are required"})
                    return
                from crm_ai_agent.adapters.webitel_api.actions import WebitelApiActions

                actions = WebitelApiActions.from_info_env(INFO_ENV_PATH, WEBITEL_STORAGE_STATE_PATH)
                plan = actions.build_create_plan(
                    template_extension=template_extension,
                    new_username=new_username,
                    new_name=new_name,
                    new_extension=new_extension,
                )
                self._send_json(200, plan)
                return

            if self.path == "/api/webitel/create_execute":
                plan = body.get("plan")
                if not plan:
                    self._send_json(400, {"error": "plan is required"})
                    return
                from crm_ai_agent.adapters.webitel_api.actions import WebitelApiActions

                actions = WebitelApiActions.from_info_env(INFO_ENV_PATH, WEBITEL_STORAGE_STATE_PATH)
                result = actions.execute_create_plan(plan)
                self._send_json(200, result)
                return

            self._send_json(404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001 — surface to the operator's browser, not a crash
            self._send_json(500, {"error": str(exc)})


def run(port: int = 8765) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Console running at http://127.0.0.1:{port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
