/* shared.js — nipissing.ca */

let ALL_DATA = [];
let BYLAWS = [];
let RESOLUTIONS = [];
let MEETINGS = [];
let LAST_UPDATED = '';

async function loadAllData() {
  const [blRes, resRes, cRes] = await Promise.all([
    fetch('bylaws-data.json').catch(()=>null),
    fetch('resolutions-data.json').catch(()=>null),
    fetch('council-data.json').catch(()=>null)
  ]);

  if (blRes && blRes.ok) {
    const d = await blRes.json();
    BYLAWS = (d.bylaws || []).map(b => ({...b, _type:'bylaw'}));
    LAST_UPDATED = d.last_updated || '';
  }
  if (resRes && resRes.ok) {
    const d = await resRes.json();
    RESOLUTIONS = (d.resolutions || []).map(r => {
      const ym = (r.number||'').match(/R(\d{4})/);
      return {...r, _type:'resolution', year: ym ? +ym[1] : null};
    });
  }
  if (cRes && cRes.ok) {
    const d = await cRes.json();
    MEETINGS = (d.meetings || []).map(m => ({...m, _type:'meeting'}));
  }
  ALL_DATA = [...BYLAWS, ...RESOLUTIONS, ...MEETINGS];
}

function scoreItem(item, q) {
  const ql = q.toLowerCase();
  const tk = ql.split(/\s+/).filter(Boolean);
  const n = (item.number || '').toLowerCase();
  const t = (item.title || item.motion_text || item.display_date || '').toLowerCase();
  const s = (item.ai_summary || item.motion_text || item.summary || '').toLowerCase();
  const v = (item.votes || `${item.mover||''} ${item.seconder||''}`).toLowerCase();
  let sc = 0;
  if (n === ql || n.includes(ql)) sc += 100;
  if (t.includes(ql)) sc += 60;
  for (const k of tk) {
    if (n.includes(k)) sc += 30;
    if (t.includes(k)) sc += 20;
    if (s.includes(k)) sc += 10;
    if (v.includes(k)) sc += 5;
  }
  return sc;
}

function hl(text, q) {
  if (!q || !text) return escHtml(text||'');
  const safe = escHtml(text);
  const tk = q.toLowerCase().split(/\s+/).filter(x => x.length > 1);
  let r = safe;
  for (const k of tk) {
    r = r.replace(new RegExp(`(${k.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')})`, 'gi'), '<span class="hl">$1</span>');
  }
  return r;
}

function escHtml(s) {
  return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function fmtDate(dateStr) {
  if (!dateStr) return '';
  try {
    return new Date(dateStr + 'T00:00:00').toLocaleDateString('en-CA', {year:'numeric',month:'short',day:'numeric'});
  } catch(e) { return dateStr; }
}

// ── Card renderers ──

function bylawCard(b, q) {
  const date = b.date_passed || b.meeting_date || '';
  const status = b.status || 'approved';
  return `<a href="bylaw-detail.html?id=${encodeURIComponent(b.number)}" class="result-card">
    <div class="result-icon ri-bylaw"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg></div>
    <div class="result-body">
      <div class="result-meta">
        <span class="result-type rt-bylaw">By-Law</span>
        <span class="result-num">${hl(b.number||'', q)}</span>
        ${date ? `<span class="result-date">${fmtDate(date)}</span>` : ''}
      </div>
      <div class="result-title">${hl(b.title||'(Untitled)', q)}</div>
      <div class="result-tags">
        <span class="status-pill status-${status}">${escHtml(status)}</span>
        ${b.votes ? `<span class="tag">${hl(b.votes, q)}</span>` : ''}
      </div>
    </div>
  </a>`;
}

function resolutionCard(r, q) {
  const date = r.date || r.meeting_date || '';
  const outcome = r.outcome || r.status || '';
  const title = r.title || (r.motion_text || '').substring(0, 110) + (r.motion_text && r.motion_text.length > 110 ? '…' : '');
  return `<a href="resolution-detail.html?id=${encodeURIComponent(r.number)}" class="result-card">
    <div class="result-icon ri-resolution"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4"/></svg></div>
    <div class="result-body">
      <div class="result-meta">
        <span class="result-type rt-resolution">Resolution</span>
        <span class="result-num">${hl(r.number||'', q)}</span>
        ${date ? `<span class="result-date">${fmtDate(date)}</span>` : ''}
      </div>
      <div class="result-title">${hl(title || '(No title)', q)}</div>
      <div class="result-tags">
        ${outcome ? `<span class="status-pill status-${outcome}">${escHtml(outcome)}</span>` : ''}
        ${r.mover ? `<span class="tag">Moved by ${hl(r.mover, q)}</span>` : ''}
        ${r.category ? `<span class="tag">${escHtml(r.category)}</span>` : ''}
      </div>
    </div>
  </a>`;
}

function meetingCard(m, q) {
  const docs = [m.agenda_url&&'Agenda', m.minutes_url&&'Minutes', m.package_url&&'Package', m.video_url&&'Video'].filter(Boolean);
  const title = m.title || `${m.meeting_type||'Regular'} Meeting`;
  return `<a href="meeting-detail.html?date=${encodeURIComponent(m.date)}" class="result-card">
    <div class="result-icon ri-meeting"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect width="18" height="18" x="3" y="4" rx="2" ry="2"/><line x1="16" x2="16" y1="2" y2="6"/><line x1="8" x2="8" y1="2" y2="6"/><line x1="3" x2="21" y1="10" y2="10"/></svg></div>
    <div class="result-body">
      <div class="result-meta">
        <span class="result-type rt-meeting">Council Meeting</span>
        <span class="result-num">${hl(m.meeting_type||'Regular', q)}</span>
        <span class="result-date">${escHtml(m.display_date||m.date||'')}</span>
      </div>
      <div class="result-title">${hl(title, q)}</div>
      ${docs.length ? `<div class="result-tags">${docs.map(d=>`<span class="tag">${d}</span>`).join('')}</div>` : ''}
    </div>
  </a>`;
}
