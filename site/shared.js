let LAST_UPDATED = '';
let BYLAWS = [];
let RESOLUTIONS = [];
let MEETINGS = [];       // council only
let BOARDS = [];         // raw boards structure
let BOARD_MEETINGS = []; // flattened board/committee meetings
let ALL_MEETINGS = [];   // council + boards
let ALL_DATA = [];

const BAD_SUMMARY_PHRASES = [
  "i'm unable to read",
  "i apologize, but i'm unable",
  "corrupted or",
  "compressed format",
  "unreadable",
  "improperly encoded",
  "cannot decode",
  "cannot decompress",
  "not able to read",
  "unable to extract",
  "i would need",
];

function escHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function fmtDate(dateStr) {
  if (!dateStr) return '';
  const d = new Date(`${dateStr}T00:00:00`);
  if (Number.isNaN(d.getTime())) return dateStr;
  return d.toLocaleDateString('en-CA', {
    year: 'numeric',
    month: 'long',
    day: 'numeric',
  });
}

async function fetchJson(url, fallback) {
  try {
    const r = await fetch(url, { cache: 'no-store' });
    if (!r.ok) return fallback;
    return await r.json();
  } catch {
    return fallback;
  }
}

function normalizeMeetingsPayload(payload) {
  if (Array.isArray(payload)) return payload;
  return payload?.meetings || [];
}

function normalizeBylawsPayload(payload) {
  if (Array.isArray(payload)) return payload;
  return payload?.bylaws || [];
}

function normalizeResolutionsPayload(payload) {
  if (Array.isArray(payload)) return payload;
  return payload?.resolutions || [];
}

function normalizeBoardsPayload(payload) {
  if (Array.isArray(payload)) return payload;
  return payload?.boards || [];
}

function hasBadSummary(text) {
  const t = String(text || '').toLowerCase();
  if (!t.trim()) return true;
  return BAD_SUMMARY_PHRASES.some(p => t.includes(p));
}

function boardTypeLabel(boardName) {
  const n = String(boardName || '').toLowerCase();
  if (n.includes('committee')) return 'Committee';
  if (n.includes('board')) return 'Board';
  return 'Board';
}

function primaryMeetingUrl(m) {
  if (m._type !== 'meeting') return '#';

  const params = new URLSearchParams();
  params.set('date', m.date || '');
  params.set('body', m.body_id || 'council');

  return `meeting-detail.html?${params.toString()}`;
}

function flattenBoards(boards) {
  const out = [];

  for (const board of boards) {
    const boardId = board.id || '';
    const boardName = board.name || board.board_name || boardId || 'Board';

    for (const m of (board.meetings || [])) {
      out.push({
        ...m,
        _type: 'meeting',
        source_kind: 'board',
        body: boardName,
        body_id: boardId,
        meeting_type: boardTypeLabel(boardName),
        title: `${boardName} — ${m.display_date || m.date || ''}`.trim(),
        summary: hasBadSummary(m.summary) ? null : m.summary,
        video_url: null,
      });
    }
  }

  return out.sort((a, b) => String(b.date || '').localeCompare(String(a.date || '')));
}

function normalizeCouncilMeetings(meetings) {
  return meetings.map(m => ({
    ...m,
    _type: 'meeting',
    source_kind: 'council',
    body: 'Council',
    body_id: 'council',
    title: `${m.display_date || m.date || ''} Council Meeting`.trim(),
  }));
}

function computeLastUpdated(...payloads) {
  const values = payloads
    .map(p => p?.last_updated)
    .filter(Boolean)
    .sort()
    .reverse();
  return values[0] || '';
}

async function loadAllData() {
  const [meetingsPayload, bylawsPayload, resolutionsPayload, boardsPayload] = await Promise.all([
    fetchJson('council-data.json', { meetings: [] }),
    fetchJson('bylaws-data.json', { bylaws: [] }),
    fetchJson('resolutions-data.json', { resolutions: [] }),
    fetchJson('boards-data.json', { boards: [] }),
  ]);

  LAST_UPDATED = computeLastUpdated(meetingsPayload, bylawsPayload, resolutionsPayload, boardsPayload);

  MEETINGS = normalizeCouncilMeetings(normalizeMeetingsPayload(meetingsPayload));
  BYLAWS = normalizeBylawsPayload(bylawsPayload).map(b => ({ ...b, _type: 'bylaw' }));
  RESOLUTIONS = normalizeResolutionsPayload(resolutionsPayload).map(r => ({ ...r, _type: 'resolution' }));
  BOARDS = normalizeBoardsPayload(boardsPayload);
  BOARD_MEETINGS = flattenBoards(BOARDS);

  ALL_MEETINGS = [...MEETINGS, ...BOARD_MEETINGS].sort((a, b) =>
    String(b.date || '').localeCompare(String(a.date || ''))
  );

  ALL_DATA = [
    ...BYLAWS,
    ...RESOLUTIONS,
    ...ALL_MEETINGS,
  ];
}

function highlightText(text, q) {
  const source = String(text ?? '');
  if (!q || !q.trim()) return escHtml(source);

  const escaped = q.trim().replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const re = new RegExp(`(${escaped})`, 'ig');

  return escHtml(source).replace(re, '<span class="hl">$1</span>');
}

function scoreItem(item, q) {
  const needle = q.trim().toLowerCase();
  if (!needle) return 0;

  const fields = [
    item.number,
    item.title,
    item.motion_text,
    item.category,
    item.date,
    item.display_date,
    item.body,
    item.body_id,
    item.meeting_type,
    item.summary,
    item.board_name,
  ].filter(Boolean).map(v => String(v).toLowerCase());

  let score = 0;
  for (const f of fields) {
    if (f === needle) score += 100;
    if (f.includes(needle)) score += 20;
    if (f.startsWith(needle)) score += 10;
  }

  if (item._type === 'meeting' && item.body && String(item.body).toLowerCase().includes(needle)) score += 10;
  if (item._type === 'bylaw' && String(item.number || '').toLowerCase() === needle) score += 120;
  if (item._type === 'resolution' && String(item.number || '').toLowerCase() === needle) score += 120;

  return score;
}

function resultTags(tags) {
  const clean = tags.filter(Boolean);
  if (!clean.length) return '';
  return `<div class="result-tags">${clean.map(t => `<span class="tag">${escHtml(t)}</span>`).join('')}</div>`;
}

function bylawCard(b, q = '') {
  const date = b.date_passed || b.meeting_date || '';
  const status = b.status || 'approved';
  const excerpt = b.ai_summary || b.title || '';
  const tags = [
    b.year ? String(b.year) : '',
    b.source === 'bylaws_page' ? 'Township by-laws' : '',
    b.pdf_url ? 'PDF' : '',
    b.page_url ? 'Township page' : '',
  ];

  return `
    <a class="result-card" href="bylaw-detail.html?id=${encodeURIComponent(b.number || '')}">
      <div class="result-icon ri-bylaw">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
          <polyline points="14 2 14 8 20 8"/>
        </svg>
      </div>
      <div class="result-body">
        <div class="result-meta">
          <span class="result-type rt-bylaw">By-Law</span>
          <span class="result-num">${escHtml(b.number || '')}</span>
          ${date ? `<span class="result-date">${escHtml(fmtDate(date))}</span>` : ''}
        </div>
        <div class="result-title">${highlightText(b.title || '(Untitled)', q)}</div>
        <div class="result-excerpt">${highlightText(excerpt, q)}</div>
        ${resultTags([
          `<span class="status-pill status-${escHtml(status)}">${escHtml(status)}</span>`,
          ...tags
        ])}
      </div>
    </a>
  `;
}

function resolutionCard(r, q = '') {
  const date = r.date || r.meeting_date || '';
  const outcome = (r.outcome || r.status || '').toLowerCase();
  const excerpt = r.motion_text || r.title || '';
  const tags = [
    r.category || '',
    r.mover ? `Moved by ${r.mover}` : '',
    r.seconder ? `Seconded by ${r.seconder}` : '',
  ];

  return `
    <a class="result-card" href="resolution-detail.html?id=${encodeURIComponent(r.number || '')}">
      <div class="result-icon ri-resolution">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2"/>
          <path d="M9 5a2 2 0 0 0 2 2h2a2 2 0 0 0 2-2"/>
          <path d="M9 5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2"/>
        </svg>
      </div>
      <div class="result-body">
        <div class="result-meta">
          <span class="result-type rt-resolution">Resolution</span>
          <span class="result-num">${escHtml(r.number || '')}</span>
          ${date ? `<span class="result-date">${escHtml(fmtDate(date))}</span>` : ''}
        </div>
        <div class="result-title">${highlightText(r.title || r.motion_text || '(Untitled)', q)}</div>
        <div class="result-excerpt">${highlightText(excerpt, q)}</div>
        ${resultTags([
          outcome ? `<span class="status-pill status-${escHtml(outcome)}">${escHtml(outcome)}</span>` : '',
          ...tags
        ])}
      </div>
    </a>
  `;
}

function meetingCard(m, q = '') {
  const href = primaryMeetingUrl(m);
  const title = m.source_kind === 'council'
    ? `${m.display_date || m.date || ''} Council Meeting`
    : `${m.body || 'Board'} — ${m.display_date || m.date || ''}`;

  const excerpt = m.summary || [
    m.cancelled ? 'Cancelled meeting.' : '',
    m.agenda_url ? 'Agenda posted.' : '',
    m.minutes_url ? 'Minutes posted.' : '',
    m.package_url ? 'Package posted.' : '',
    m.video_url ? 'Video recording available.' : '',
  ].filter(Boolean).join(' ');

  const tags = [
    m.body || '',
    m.meeting_type || '',
    m.cancelled ? 'Cancelled' : '',
    m.postponed ? 'Postponed' : '',
    m.rescheduled ? 'Rescheduled' : '',
    m.video_url ? 'Video' : '',
    m.minutes_url ? 'Minutes' : '',
    m.package_url ? 'Package' : '',
  ];

  const inner = `
      <div class="result-icon ri-meeting">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <rect width="18" height="18" x="3" y="4" rx="2" ry="2"/>
          <line x1="16" x2="16" y1="2" y2="6"/>
          <line x1="8" x2="8" y1="2" y2="6"/>
          <line x1="3" x2="21" y1="10" y2="10"/>
        </svg>
      </div>
      <div class="result-body">
        <div class="result-meta">
          <span class="result-type rt-meeting">${escHtml(m.body || 'Meeting')}</span>
          <span class="result-num">${escHtml(m.meeting_type || '')}</span>
          ${m.date ? `<span class="result-date">${escHtml(fmtDate(m.date))}</span>` : ''}
        </div>
        <div class="result-title">${highlightText(title, q)}</div>
        <div class="result-excerpt">${highlightText(excerpt, q)}</div>
        ${resultTags(tags)}
      </div>
  `;

  if (href && href !== '#') {
    return `<a class="result-card" href="${escHtml(href)}"${m.source_kind === 'board' ? ' target="_blank" rel="noopener"' : ''}>${inner}</a>`;
  }

  return `<div class="result-card">${inner}</div>`;
}
