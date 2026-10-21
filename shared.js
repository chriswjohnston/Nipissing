let BYLAWS = [];
let RESOLUTIONS = [];
let MEETINGS = [];
let BOARDS = [];
let ALL_MEETINGS = [];
let ALL_DATA = [];
let LAST_UPDATED = '';

// --- Display policy -----------------------------------------------------------
// Cutoffs and the archived-fallback resolver live here so every page (the
// meetings list, the calendar, the detail page, search) behaves identically.
const SITE_MIN_YEAR = 2022;    // hide pre-PDF-era meetings entirely
const AGENDA_MIN_YEAR = 2026;  // only surface agenda links from 2026 on

// In-repo archived files (minutes/agendas/extra docs) are committed under
// docs/, but the site is served from the repo root, so they resolve at
// docs/files/archive/... . Packages are stored as full GitHub Releases URLs.
function archivedUrl(path) {
  if (!path) return null;
  if (/^https?:\/\//i.test(path)) return path;       // Releases URL, use as-is
  return 'docs/' + String(path).replace(/^\/+/, ''); // in-repo copy under docs/
}

// Prefer the preserved archived copy (always reachable) and fall back to the
// township's live URL only when nothing has been archived.
function docUrl(meeting, kind) {
  if (!meeting) return null;
  const archived = meeting[kind + '_archived'];
  if (archived) return archivedUrl(archived);
  return meeting[kind + '_url'] || null;
}

function extraDocUrl(d) {
  if (!d) return null;
  if (d.archived) return archivedUrl(d.archived);
  return d.url || null;
}

// Agendas are only shown from 2026 on, and only when a working link exists.
function showAgenda(meeting) {
  // all_agendas: boards where the agenda is the main record (Committee of
  // Adjustment, joint boards) show agendas from SITE_MIN_YEAR on.
  const minYear = meeting && meeting.all_agendas ? SITE_MIN_YEAR : AGENDA_MIN_YEAR;
  return Number(meeting && meeting.year) >= minYear && !!docUrl(meeting, 'agenda');
}

function escHtml(str) {
  return String(str ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
  }[ch]));
}

function normText(str) {
  return String(str ?? '')
    .toLowerCase()
    .replace(/\s+/g, ' ')
    .trim();
}

function fmtDate(isoDate) {
  if (!isoDate) return '';
  const d = new Date(`${isoDate}T00:00:00`);
  if (Number.isNaN(d.getTime())) return isoDate;
  return d.toLocaleDateString('en-CA', {
    month: 'short',
    day: 'numeric',
    year: 'numeric'
  });
}

function highlight(text, q) {
  const raw = String(text ?? '');
  const query = String(q ?? '').trim();
  if (!query) return escHtml(raw);

  const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  try {
    const re = new RegExp(`(${escaped})`, 'ig');
    return escHtml(raw).replace(re, '<mark>$1</mark>');
  } catch {
    return escHtml(raw);
  }
}

function primaryMeetingUrl(meeting) {
  const date = encodeURIComponent(meeting?.date || '');
  const body = encodeURIComponent(meeting?.body_id || meeting?.board_id || 'council');
  // A special and a regular meeting of one body can share a date (DNSSAB).
  const type = meeting?.source_kind === 'board' && meeting?.meeting_type === 'Special'
    ? '&type=Special' : '';
  return `meeting-detail.html?date=${date}&body=${body}${type}`;
}

function meetingBodyLabel(meeting) {
  if (meeting?.body) return meeting.body;
  if (meeting?.board_name) return meeting.board_name;
  if (meeting?.board_id === 'recreation') return 'Recreation';
  if (meeting?.board_id === 'museum') return 'Museum Board';
  if (meeting?.board_id === 'cemetery') return 'Cemetery';
  if (meeting?.board_id === 'library') return 'Library Board';
  if (meeting?.board_id === 'pnc') return 'Police Board';
  if (meeting?.board_id === 'adjustment') return 'Committee of Adjustment';
  return 'Council';
}

function meetingTypeLabel(meeting) {
  if (meeting?.source_kind === 'board') return meetingBodyLabel(meeting);
  return meeting?.meeting_type || 'Regular';
}

function meetingTagClass(meeting) {
  const bodyId = String(meeting?.body_id || meeting?.board_id || 'council').toLowerCase();
  if (bodyId === 'council') return 'tag tag-gold';
  if (bodyId === 'recreation') return 'tag tag-teal';
  if (bodyId === 'museum') return 'tag tag-blue';
  if (bodyId === 'cemetery') return 'tag tag-purple';
  if (bodyId === 'library') return 'tag tag-library';
  if (bodyId === 'pnc') return 'tag tag-pnc';
  if (bodyId === 'adjustment') return 'tag tag-adjustment';
  if (bodyId.startsWith('healthunit')) return 'tag tag-healthunit';
  if (bodyId.startsWith('dnssab')) return 'tag tag-dnssab';
  return 'tag';
}

function meetingDocPills(meeting) {
  const pills = [];
  if (showAgenda(meeting)) {
    pills.push(`<a href="${escHtml(docUrl(meeting, 'agenda'))}" target="_blank" rel="noopener" class="tag" onclick="event.stopPropagation()">Agenda</a>`);
  }
  const mu = docUrl(meeting, 'minutes');
  if (mu) pills.push(`<a href="${escHtml(mu)}" target="_blank" rel="noopener" class="tag" onclick="event.stopPropagation()">Minutes</a>`);
  const pu = docUrl(meeting, 'package');
  if (pu) pills.push(`<a href="${escHtml(pu)}" target="_blank" rel="noopener" class="tag" onclick="event.stopPropagation()">Package</a>`);
  if (meeting?.video_url) pills.push(`<a href="${escHtml(meeting.video_url)}" target="_blank" rel="noopener" class="tag" onclick="event.stopPropagation()">Video</a>`);
  return pills.join('');
}

// Official names, used as each meeting card's title and on the detail page.
const OFFICIAL_NAMES = {
  council: 'Township of Nipissing Council',
  recreation: 'Recreation Committee',
  museum: 'Museum Board',
  cemetery: 'Cemetery Committee',
  adjustment: 'Committee of Adjustment',
  pnc: 'Powassan Nipissing Callander OPP Detachment Board',
  library: 'Powassan & District Union Public Library Board',
  'healthunit': 'North Bay Parry Sound District Health Unit — Board of Health',
  'healthunit-finance': 'North Bay Parry Sound District Health Unit — Finance and Property Committee',
  'healthunit-personnel': 'North Bay Parry Sound District Health Unit — Personnel Policy, Employee/Labour Relations Committee',
  dnssab: 'District of Nipissing Social Services Administration Board',
  'dnssab-ndhc': 'Nipissing District Housing Corporation Board',
};

function officialName(meeting) {
  const id = String(meeting?.body_id || meeting?.board_id || 'council').toLowerCase();
  if (OFFICIAL_NAMES[id]) return OFFICIAL_NAMES[id];
  // Other DNSSAB committees: "DNSSAB Community Services Committee" etc.
  if (id.startsWith('dnssab-')) {
    const name = String(meeting?.board_name || meeting?.body || '')
      .replace(/^DNSSAB:?\s*/i, '').replace(/\s+Meeting$/i, '');
    return `District of Nipissing Social Services Administration Board — ${name}`;
  }
  return meeting?.board_name || meetingBodyLabel(meeting);
}

// Badge only when it adds information.
function meetingBadge(meeting) {
  if (meeting?.cancelled) return { text: 'Cancelled', cls: 'mb-cancelled' };
  if (meeting?.postponed) return { text: 'Postponed', cls: 'mb-cancelled' };
  if (meeting?.meeting_type === 'Special') return { text: 'Special', cls: 'mb-special' };
  return null;
}

function bodyColorClass(meeting) {
  const id = String(meeting?.group || meeting?.body_id || meeting?.board_id || 'council').toLowerCase();
  return 'body-' + id.replace(/[^a-z0-9-]/g, '');
}

function meetingCard(meeting, q = '') {
  const href = primaryMeetingUrl(meeting);
  const badge = meetingBadge(meeting);

  return `
  <article class="result-card meeting-card" onclick="window.location='${escHtml(href)}'" style="cursor:pointer;">

    <div class="result-icon ri-meeting">
      📅
    </div>

    <div class="result-body">

      <div class="result-meta">
        <span class="result-title meeting-name ${bodyColorClass(meeting)}">${highlight(officialName(meeting), q)}</span>
        ${badge ? `<span class="meeting-badge ${badge.cls}">${badge.text}</span>` : ''}
        <span class="result-date">${escHtml(meeting.display_date || fmtDate(meeting.date))}</span>
      </div>

      <div class="result-tags">
        ${meetingDocPills(meeting)}
      </div>

    </div>
  </article>
`;
}

function bylawStatusClass(status) {
  const s = String(status || '').toLowerCase();
  if (s === 'approved' || s === 'carried') return 'status-pill status-approved';
  if (s === 'defeated') return 'status-pill status-defeated';
  return 'status-pill status-pending';
}

function bylawCard(bylaw, q = '') {
  const href = `bylaw-detail.html?id=${encodeURIComponent(bylaw?.number || '')}`;
  const title = bylaw?.title || `By-Law ${bylaw?.number || ''}`;
  const dateText = bylaw?.date_passed
    ? fmtDate(bylaw.date_passed)
    : (bylaw?.meeting_date ? fmtDate(bylaw.meeting_date) : (bylaw?.year || ''));

  return `
  <article class="result-card bylaw-card" onclick="window.location='${escHtml(href)}'" style="cursor:pointer;">
    
    <div class="result-icon ri-bylaw">📄</div>

    <div class="result-body">

      <div class="result-meta">
        <span class="result-type rt-bylaw">BY-LAW</span>
        <span class="result-num">${escHtml(bylaw.number)}</span>
        <span class="result-date">${escHtml(dateText)}</span>
      </div>

      <div class="result-title">
        ${highlight(title, q)}
      </div>

      <div class="result-tags">
        <span class="${bylawStatusClass(bylaw.status)}">${escHtml(bylaw.status)}</span>
      </div>

    </div>
  </article>
`;
}

function resolutionCard(resolution, q = '') {
  const href = `resolution-detail.html?id=${encodeURIComponent(resolution?.number || '')}`;
  const title = resolution?.title || resolution?.motion_text || resolution?.number || 'Resolution';
  const dateText = resolution?.meeting_date ? fmtDate(resolution.meeting_date) : '';

  return `
  <article class="result-card resolution-card" onclick="window.location='${escHtml(href)}'" style="cursor:pointer;">
    
    <div class="result-icon ri-resolution">🧾</div>

    <div class="result-body">

      <div class="result-meta">
        <span class="result-type rt-resolution">RESOLUTION</span>
        <span class="result-num">${escHtml(resolution?.number || 'Resolution')}</span>
        <span class="result-date">
          ${
            dateText
              ? `<a class="result-date-link" href="meeting-detail.html?date=${encodeURIComponent(resolution.meeting_date || '')}&body=council" onclick="event.stopPropagation()">${escHtml(dateText)}</a>`
              : ''
          }
        </span>
      </div>

      <div class="result-title">
        ${highlight(title, q)}
      </div>

      <div class="result-tags">
        <span class="${bylawStatusClass(resolution?.status)}">${escHtml(resolution?.status || 'unknown')}</span>
        ${resolution?.bylaw_number ? `<span class="result-linkage">By-Law ${escHtml(resolution.bylaw_number)}</span>` : ''}
      </div>

    </div>
  </article>
`;
}

function scoreItem(item, q) {
  const query = normText(q);
  if (!query) return 0;

  const fields = [];
  if (item?._type === 'meeting') {
    fields.push(
      item.display_date,
      item.date,
      item.title,
      item.meeting_type,
      item.body,
      item.board_name,
      item.summary
    );
  } else if (item?._type === 'bylaw') {
    fields.push(
      item.number,
      item.title,
      item.summary,
      item.status,
      item.date_passed,
      item.meeting_date
    );
  } else if (item?._type === 'resolution') {
    fields.push(
      item.number,
      item.title,
      item.motion_text,
      item.category,
      item.status,
      item.meeting_date,
      item.bylaw_number
    );
  }

  const haystack = normText(fields.filter(Boolean).join(' | '));
  if (!haystack) return 0;

  let score = 0;
  if (haystack.includes(query)) score += 100;

  const words = query.split(/\s+/).filter(Boolean);
  for (const word of words) {
    if (haystack.includes(word)) score += 10;
  }

  return score;
}

async function fetchJsonCandidates(paths) {
  for (const path of paths) {
    try {
      const res = await fetch(path, { cache: 'no-store' });
      if (!res.ok) continue;
      return await res.json();
    } catch {}
  }
  return null;
}

function flattenBoards(boardsPayload) {
  const boards = Array.isArray(boardsPayload?.boards) ? boardsPayload.boards : [];
  const flat = [];

  for (const board of boards) {
    const boardId = board?.id || '';
    const boardName = board?.name || meetingBodyLabel({ board_id: boardId });

    for (const meeting of Array.isArray(board?.meetings) ? board.meetings : []) {
      flat.push({
        ...meeting,
        body: meeting?.body || boardName,
        body_id: meeting?.body_id || boardId,
        board_id: boardId,
        board_name: boardName,
        source_kind: 'board',
        _type: 'meeting'
      });
    }
  }

  return { boards, meetings: flat };
}

async function loadAllData() {
  const meetingsPayload = await fetchJsonCandidates([
    'data/canonical/meetings.json',
    'docs/data/meetings.json',
    'council-data.json'
  ]) || { meetings: [] };

  const bylawsPayload = await fetchJsonCandidates([
    'data/canonical/bylaws.json',
    'docs/data/bylaws.json',
    'bylaws-data.json'
  ]) || { bylaws: [] };

  const resolutionsPayload = await fetchJsonCandidates([
    'data/canonical/resolutions.json',
    'docs/data/resolutions.json',
    'resolutions-data.json'
  ]) || { resolutions: [] };

  const boardsPayload = await fetchJsonCandidates([
    'data/canonical/boards.json',
    'docs/data/boards.json',
    'boards-data.json'
  ]) || { boards: [] };

  MEETINGS = Array.isArray(meetingsPayload?.meetings)
    ? meetingsPayload.meetings.map(m => ({
        ...m,
        body: m?.body || 'Council',
        body_id: m?.body_id || 'council',
        source_kind: 'council',
        _type: 'meeting'
      }))
    : [];

  BYLAWS = Array.isArray(bylawsPayload?.bylaws)
    ? bylawsPayload.bylaws.map(b => ({ ...b, _type: 'bylaw' }))
    : [];

  RESOLUTIONS = Array.isArray(resolutionsPayload?.resolutions)
    ? resolutionsPayload.resolutions.map(r => ({ ...r, _type: 'resolution' }))
    : [];

  const flatBoards = flattenBoards(boardsPayload);
  BOARDS = flatBoards.boards;

  // Apply the PDF-era cutoff once, centrally — every list, the calendar, the
  // detail lookup, and search all read from ALL_MEETINGS / ALL_DATA.
  ALL_MEETINGS = [...MEETINGS, ...flatBoards.meetings]
    .filter(m => Number(m.year) >= SITE_MIN_YEAR)
    .sort((a, b) => String(b.date || '').localeCompare(String(a.date || '')));

  ALL_DATA = [...BYLAWS, ...RESOLUTIONS, ...ALL_MEETINGS];

  LAST_UPDATED =
    meetingsPayload?.last_updated ||
    bylawsPayload?.last_updated ||
    resolutionsPayload?.last_updated ||
    boardsPayload?.last_updated ||
    '';
}
