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

// Links to a meeting's documents for list rows. Plain text links; the
// "Copy kept" stamp is shown on detail pages where there is room for it.
function meetingDocPills(meeting) {
  const pills = [];
  const link = (label, url) => `<a href="${escHtml(url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${label}</a>`;
  if (showAgenda(meeting)) pills.push(link('Agenda', docUrl(meeting, 'agenda')));
  const mu = docUrl(meeting, 'minutes');
  if (mu) pills.push(link('Minutes', mu));
  const pu = docUrl(meeting, 'package');
  if (pu) pills.push(link('Package', pu));
  if (meeting?.video_url) pills.push(link('Video', meeting.video_url));
  return pills.join('');
}

// A link resolves to our own copy when it points into the repo (docs/...)
// or at a GitHub Release asset; anything else is the township's original.
function isKeptUrl(url) {
  const u = String(url || '');
  return /^docs\//.test(u) || /^https?:\/\/github\.com\/[^/]+\/[^/]+\/releases\//i.test(u);
}

const ICON_DOC = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>';
const ICON_VIDEO = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="m10 9 5 3-5 3z"/></svg>';
const ICON_PAGE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M4 5h16v14H4z"/><path d="M8 9h8M8 13h8M8 17h4"/></svg>';
const ICON_CHECK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" aria-hidden="true"><path d="m5 12 5 5 9-10"/></svg>';

function keptStamp() {
  return `<span class="stamp" title="A copy is kept in this archive, so the link keeps working if the township removes the original.">${ICON_CHECK}Copy kept</span>`;
}

// One document link for detail pages. internal = a page on this site.
function docLinkHtml(label, href, opts = {}) {
  if (!href) return '';
  const internal = !!opts.internal;
  const video = /youtu\.?be/i.test(href);
  let note = '';
  if (!internal) {
    if (isKeptUrl(href)) note = keptStamp();
    else if (video) note = '<span class="source-note">YouTube</span>';
    else if (/nipissingtownship\.com/i.test(href)) note = '<span class="source-note">Township site</span>';
  }
  const icon = internal ? ICON_PAGE : (video ? ICON_VIDEO : ICON_DOC);
  const target = internal ? '' : ' target="_blank" rel="noopener"';
  return `<a href="${escHtml(href)}"${target} class="doc-link">${icon}<span class="doc-label">${escHtml(label)}</span>${note}</a>`;
}

// Lead decisions from an AI summary: the bullets under "Key Decisions".
function keyDecisions(summary, max = 5) {
  const lines = String(summary || '').replace(/\r\n/g, '\n').split('\n');
  const out = [];
  let inSection = false;
  for (const raw of lines) {
    const line = raw.trim();
    if (/^#{1,4}\s+/.test(line)) {
      if (inSection && out.length) break;
      inSection = /key decisions/i.test(line);
      continue;
    }
    if (inSection && /^[-*•]\s+/.test(line)) out.push(line.replace(/^[-*•]\s+/, '').replace(/\*\*(.+?)\*\*/g, '$1'));
    if (out.length >= max) break;
  }
  return out;
}

// Official names, used as each meeting card's title and on the detail page.
const OFFICIAL_NAMES = {
  council: 'Township of Nipissing Council',
  recreation: 'Township of Nipissing Recreation Committee',
  museum: 'Township of Nipissing Museum Board',
  cemetery: 'Township of Nipissing Cemetery Committee',
  adjustment: 'Township of Nipissing Committee of Adjustment',
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
  const docs = meetingDocPills(meeting);
  return `
  <article class="record-row meeting-card" onclick="window.location='${escHtml(href)}'">
    <span class="record-date">${escHtml(meeting.display_date || fmtDate(meeting.date))}</span>
    <div class="record-main">
      <a class="record-title ${bodyColorClass(meeting)}" href="${escHtml(href)}">${highlight(officialName(meeting), q)}</a>${badge ? `<span class="meeting-badge ${badge.cls}">${badge.text}</span>` : ''}
      ${docs ? `<div class="record-docs">${docs}</div>` : (badge ? '' : '<div class="record-docs"><span class="waiting">No documents posted yet</span></div>')}
    </div>
  </article>`;
}

function bylawStatusClass(status) {
  const s = String(status || '').toLowerCase();
  if (s === 'approved' || s === 'carried') return 'status-pill status-approved';
  if (s === 'defeated') return 'status-pill status-defeated';
  if (s === 'repealed') return 'status-pill status-repealed';
  return 'status-pill status-pending';
}

function sentenceCase(t) {
  const s = String(t || '');
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : '';
}

function bylawCard(bylaw, q = '') {
  const href = `bylaw-detail.html?id=${encodeURIComponent(bylaw?.number || '')}`;
  const title = bylaw?.common_name || sentenceCase(bylaw?.title) || '';
  const dateText = bylaw?.date_passed
    ? fmtDate(bylaw.date_passed)
    : (bylaw?.meeting_date ? fmtDate(bylaw.meeting_date) : (bylaw?.year || ''));
  const status = String(bylaw?.status || '').toLowerCase();
  // "Approved" is the normal case; only flag the exceptions.
  const pill = status && status !== 'approved' ? `<span class="${bylawStatusClass(status)}">${escHtml(status)}</span>` : '';
  return `
  <article class="record-row bylaw-card" onclick="window.location='${escHtml(href)}'">
    <span class="record-date">${escHtml(dateText)}</span>
    <div class="record-main">
      <a class="record-title num" href="${escHtml(href)}">By-law ${highlight(bylaw.number, q)}</a>${pill}
      ${title ? `<span class="record-text">${highlight(title, q)}</span>` : ''}
    </div>
  </article>`;
}

function resolutionCard(resolution, q = '') {
  const href = `resolution-detail.html?id=${encodeURIComponent(resolution?.number || '')}`;
  const title = resolution?.title || resolution?.motion_text || '';
  const dateText = resolution?.meeting_date ? fmtDate(resolution.meeting_date) : '';
  const meetingHref = `meeting-detail.html?date=${encodeURIComponent(resolution?.meeting_date || '')}&body=council`;
  return `
  <article class="record-row resolution-card" onclick="window.location='${escHtml(href)}'">
    <span class="record-date">${dateText ? `<a href="${escHtml(meetingHref)}" onclick="event.stopPropagation()">${escHtml(dateText)}</a>` : ''}</span>
    <div class="record-main">
      <a class="record-title num" href="${escHtml(href)}">Resolution ${highlight(resolution?.number || '', q)}</a><span class="${bylawStatusClass(resolution?.status)}">${escHtml(resolution?.status || 'unknown')}</span>
      ${title ? `<span class="record-text record-text-clamp">${highlight(title, q)}</span>` : ''}
      ${resolution?.bylaw_number ? `<div class="record-meta"><a href="bylaw-detail.html?id=${encodeURIComponent(resolution.bylaw_number)}" onclick="event.stopPropagation()">By-law ${escHtml(resolution.bylaw_number)}</a></div>` : ''}
    </div>
  </article>`;
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


// ── Site chrome ────────────────────────────────────────────────────────
// Mobile menu toggle for the shared header.
document.addEventListener('click', e => {
  const btn = e.target.closest && e.target.closest('.nav-toggle');
  if (!btn) return;
  const nav = document.getElementById(btn.getAttribute('aria-controls'));
  if (!nav) return;
  const open = nav.classList.toggle('open');
  btn.setAttribute('aria-expanded', open ? 'true' : 'false');
});

// Laws assembled with their amendments (consolidations.json), newest change first.
async function loadLaws() {
  try {
    const res = await fetch('data/canonical/consolidations.json', { cache: 'no-cache' });
    if (!res.ok) return [];
    const data = await res.json();
    return Object.entries(data.consolidations || {}).map(([id, law]) => ({ id, ...law }));
  } catch { return []; }
}

function lawCardHtml(law) {
  const byNum = Object.fromEntries(BYLAWS.map(b => [String(b.number), b]));
  const amends = law.amendments || [];
  const years = [law.principal, ...amends]
    .map(n => byNum[n]?.date_passed || byNum[n]?.meeting_date || '')
    .filter(Boolean).sort();
  const last = years.length ? years[years.length - 1].slice(0, 4) : '';
  const basis = `By-law ${law.principal}` + (amends.length
    ? `, amended by ${amends.length === 1 ? amends[0] : amends.slice(0, -1).join(', ') + ' and ' + amends[amends.length - 1]}`
    : '');
  const note = !(law.consolidated_markdown || (law.contents || []).length)
    ? 'Being assembled; the original documents are linked'
    : law.mode === 'outline' ? 'What it does and what’s in it'
    : amends.length ? `Unofficial consolidation${last ? `, last amended ${last}` : ''}` : 'Full text';
  return `<a class="law-card" href="law.html?id=${encodeURIComponent(law.id)}">
    <span class="law-card-name">${escHtml(law.name || law.id)}</span>
    <span class="law-card-basis">${escHtml(basis)}</span>
    <span class="law-card-note">${escHtml(note)}</span>
  </a>`;
}

// ── Decisions: resolutions and by-laws as one record ───────────────────
// Rules work on council's own wording; no AI. Order matters: first match wins.

// The operative part of a motion: what follows "NOW THEREFORE" / "BE IT
// RESOLVED", without a leading "mover, seconder:" prefix.
function operativeText(text) {
  let t = String(text || '').replace(/\s+/g, ' ').trim();
  t = t.replace(/^[A-Z]\.\s*[A-Z][\w'’-]+,\s*[A-Z]\.\s*[A-Z][\w'’-]+:\s*/, '');
  const parts = t.split(/\b(?:NOW\s+THEREFORE(?:\s+BE\s+IT\s+RESOLVED)?|BE\s+IT\s+RESOLVED)\b[,:]?\s*/i);
  let op = parts.length > 1 ? parts[parts.length - 1] : t;
  op = op.replace(/^(?:that|THAT)\s+/, '').trim();
  // The outcome and any note after it ("Defeated. Ed Veldboom left the meeting.")
  op = op.replace(/\s+(?:Carried|Defeated|Deferred|Lost|Withdrawn|CARRIED|DEFEATED|DEFERRED)(?:\.|\s*$)[\s\S]*$/, '').trim();
  return op ? op.charAt(0).toUpperCase() + op.slice(1) : t;
}

const ROUTINE_RULES = [
  /\bminutes\b[^.]{0,160}\b(?:be\s+)?(?:adopted|approved)\b/i,
  /\badopt(?:s|ed)?\s+the\s+minutes\b/i,
  /\bagenda\b[^.]{0,80}\b(?:be\s+)?(?:adopted|approved)\b/i,
  /\b(?:meeting|council)\s+(?:be\s+|is\s+)?adjourn/i,
  /^(?:that\s+)?(?:we|council)\s+receives?\s+(?:the\s+)?[\w\s,.-]{0,80}(?:report|correspondence|minutes|presentation|delegation|letter|update)s?\b[^.]{0,40}(?:as\s+presented|for\s+information)/i,
  /^(?:that\s+)?(?:we|council)\s+receives?\s+the\s+correspondence/i,
  /^(?:that\s+)?(?:we|council)\s+(?:receives?|accepts?)\s+the\s+correspondence/i,
  /\bconfirm\s+the\s+proceedings\s+of\s+council\b/i,
];

function isRoutineText(text) {
  const t = String(text || '');
  return ROUTINE_RULES.some(re => re.test(t));
}

const KIND_RULES = [
  ['Bills paid', /statement\s+of\s+accounts|accounts\s+payable|\baccounts\b[^.]{0,80}\bbe\s+approved/i],
  ['Closed sessions', /closed\s+to\s+the\s+public|closed\s+session|in[-\s]camera|resume\s+to\s+an?\s+open|reconvene|rise\s+from|section\s+239/i],
  ['Elections', /\belection\b|\bvoting\b|\bballots?\b|advance\s+poll|\bpolling\b|compliance\s+audit/i],
  ['Scheduling', /meeting\s+schedule|holiday\s+hours|\boffice\s+(?:to\s+)?close|\bclose\s+(?:the\s+)?(?:township\s+)?(?:administrative\s+)?office|reschedul|changes?\s+the\s+date\s+of|town\s+hall\s+meeting\s+to\s+be\s+held/i],
  ['Support for municipalities', /\bsupports?\s+(?:the\s+)?(?:[\w’'.,&\s-]{0,120})?(?:resolution|motion|submission|appeal|requests?|nomination|correspondence|investigation)\b|\bsupports?\s+(?:the\s+)?(?:city|town|township|municipality|county|district|association|AMO|FONOM|MPP|ROMA)\b|\bendorses?\b|\b(?:request|requests|urge|urges)\s+(?:that\s+)?(?:the\s+)?(?:province|provincial|government|ministry|federal|minister|premier)\b|\bletter\s+(?:requesting|of\s+support)|resolution\s+of\s+support|recommendations\s+provided\s+to\s+the\s+province|\bsupport\s+the\s+nomination|\bnominat/i],
  ['Newsletters', /\bnewsletter/i],
  ['Taxes and budget', /\btransfer\b[^.]{0,200}\breserves?\b/i],
  ['Landfill and waste', /\blandfills?\b|\bwaste\b|tipping\s+fees?|\brecycl|blue\s+box|bag\s+limit|transfer\s+station|\bfill\s+site\b/i],
  ['Zoning and land', /\bzoning\b|\brezon|holding\s*\(?h\)?\s*symbol|stop\s+up|road\s+allowance|sale\s+of\s+land|\bconvey|\bseverance|minor\s+variance|consent\s+application|part\s+of\s+lot|\bplan\s+42r|\bdeeming\b|site\s+plan|acknowledgment\s+and\s+direction/i],
  ['Spending and contracts', /\btender\b|\bquotes?\b|\bquotation\b|\bRFP\b|\bpurchase|\baward|\bbid\b|\bestimate\s+provided|\bdonat|\bdedicat\w*\s+(?:the\s+)?(?:remainder|funds?|allotment)|\bproceed\s+with\b|estimated\s+cost|\bwaives?\b[^.]{0,40}\bfees?\b/i],
  ['Conference attendance', /\b(?:attend|attendance|registration)\b[^.]{0,200}\b(?:conference|convention|meeting|association|symposium|workshop|seminar|summit|AGM)\b|\bconferences?\b|\bconventions?\b/i],
  ['Grants', /\bgrant\b|\bfunding\s+(?:application|program|stream)|\bapplication\s+(?:to|for)\s+(?:the\s+)?[\w\s,&-]{0,60}(?:fund|program|stream)\b/i],
  ['Appointments', /\bappoint|\bresignation\b|\bhire\b|\bhiring\b|\broster\b|\bintegrity\s+commissioner\b|\bremuneration\b|\bposition\s+description|\bletter\s+of\s+interest/i],
  ['Agreements', /\bagreement\b|memorandum\s+of\s+understanding|\bMOU\b|\benter\s+into\b|\bcontract\b/i],
  ['Taxes and budget', /\btax\s+(?:rates?|ratios?|levy|arrears|sale|registration)|\binterim\s+(?:tax\s+)?levy|\blevy\b|\bbudget\b|\bestimates\b|\bborrow|\bdebenture|\breserves?\b|\binsurance\b|\bwrite[-\s]off|\bfees?\s+and\s+charges|financial\s+statements|capital\s+forecast|\bforecast\b|asset\s+management/i],
  ['Spending and contracts', /\$\s?\d/],
  ['Laws and policies', /\bby-?law\b|\bpolicy\b|\bprocedures?\b|\bcode\s+of\s+conduct\b|\bplan\b|\bguidelines?\b|\bstrategy\b/i],
];

function decisionKind(text) {
  // Council's operative words decide first; the WHEREAS background only
  // when those say nothing (it often mentions grants, budgets, other bodies).
  const full = String(text || '');
  for (const t of [operativeText(full), full]) {
    for (const [kind, re] of KIND_RULES) if (re.test(t)) return kind;
  }
  return 'Other';
}

const DECISION_KINDS = ['Spending and contracts', 'Bills paid', 'Taxes and budget', 'Zoning and land',
  'Agreements', 'Appointments', 'Laws and policies', 'Landfill and waste', 'Grants', 'Support for municipalities',
  'Elections', 'Scheduling', 'Newsletters', 'Conference attendance', 'Closed sessions', 'Other'];

// One list of decisions. Each by-law appears once; the resolution that
// passed it is folded in (passedBy). Returns newest first.
function buildDecisions(bylaws, resolutions) {
  const byNum = new Map(bylaws.map(b => [String(b.number), b]));
  const passedBy = new Map();
  const out = [];
  for (const r of resolutions) {
    if (r.fragment) continue;   // piece of another motion, split off by an old parser
    const num = r.bylaw_number && String(r.bylaw_number);
    const text = r.motion_text || r.title || '';
    // Fold in only the resolution that passes the by-law. One that merely
    // mentions a by-law ("Procurement By-Law 2020-51 was not followed")
    // stays a decision of its own.
    if (num && byNum.has(num) && /\bpass(?:es|ed)?\s+(?:the\s+)?by-?law\b/i.test(text)) { passedBy.set(num, r); continue; }
    const op = operativeText(text);
    out.push({
      type: 'resolution', id: r.number, date: String(r.meeting_date || '').slice(0, 10),
      text: op, full: text, status: String(r.status || '').toLowerCase(),
      routine: isRoutineText(op) || isRoutineText(text), kind: decisionKind(text),
      refersTo: num && byNum.has(num) ? num : '',
      href: `resolution-detail.html?id=${encodeURIComponent(r.number)}`, rec: r,
    });
  }
  for (const b of bylaws) {
    const num = String(b.number);
    const res = passedBy.get(num);
    const title = b.common_name && b.title && b.common_name !== b.title
      ? `${b.common_name}: ${sentenceCase(b.title)}` : sentenceCase(b.title || b.common_name || '');
    out.push({
      type: 'bylaw', id: num, date: String(b.date_passed || b.meeting_date || res?.meeting_date || '').slice(0, 10),
      text: title, full: `${b.title || ''} ${b.common_name || ''}`, status: String(b.status || '').toLowerCase(),
      routine: isRoutineText(b.title), kind: decisionKind(`${b.title || ''} ${b.common_name || ''} by-law`),
      passedBy: res ? res.number : '', href: `bylaw-detail.html?id=${encodeURIComponent(num)}`, rec: b,
    });
  }
  // Newest meeting first; within a meeting, in the order council took them
  // (a by-law sits where the resolution that passed it did).
  const seq = d => String(d.passedBy || d.id);
  return out.sort((a, b) => (b.date || '').localeCompare(a.date || '')
    || seq(a).localeCompare(seq(b), undefined, { numeric: true }));
}

function decisionRowHtml(d, q = '') {
  const label = d.type === 'bylaw' ? `By-law ${d.id}` : d.id;
  const status = d.status && !['carried', 'approved'].includes(d.status)
    ? `<span class="${bylawStatusClass(d.status)}">${escHtml(d.status)}</span>` : '';
  const meta = [`<span class="decision-kind">${escHtml(d.routine ? 'Routine' : d.kind)}</span>`];
  if (d.lawLink) meta.push(`<a href="${escHtml(d.lawLink.href)}">${escHtml(d.lawLink.label)}</a>`);
  if (d.refersTo) meta.push(`<span>Refers to <a href="bylaw-detail.html?id=${encodeURIComponent(d.refersTo)}">By-law ${escHtml(d.refersTo)}</a></span>`);
  if (d.passedBy) meta.push(`<span>Passed by <a href="resolution-detail.html?id=${encodeURIComponent(d.passedBy)}">${escHtml(d.passedBy)}</a></span>`);
  return `<li class="decision-row${d.routine ? ' is-routine' : ''}">
    <a class="decision-num" href="${escHtml(d.href)}">${highlight(label, q)}</a>
    <div class="decision-main">
      <span class="decision-text">${highlight(d.text, q)}</span>${status}
      <div class="decision-meta">${meta.join('')}</div>
    </div>
  </li>`;
}

// ── Amendments, found from by-law titles ───────────────────────────────
// "amend By-law 2021-50", "Amend Zoning By-Law Number 2020-20", "Amend BL 1088".
const AMEND_RE = /\bamend(?:s|ing)?\b[^.]{0,80}?\b(?:by-?law|bl)\s*(?:no\.?|number|#)?\s*(\d{4}-\d{1,3}|\d{3,4})\b/i;

function amendsTarget(bylaw) {
  const m = String(bylaw?.title || '').match(AMEND_RE);
  return m && m[1] !== String(bylaw.number) ? m[1] : null;
}

// Map principal by-law number -> its amending by-laws, oldest first.
function amendmentIndex(bylaws, laws = []) {
  const idx = new Map();
  const add = (p, b) => {
    if (!idx.has(p)) idx.set(p, []);
    if (!idx.get(p).some(x => x.number === b.number)) idx.get(p).push(b);
  };
  const byNum = new Map(bylaws.map(b => [String(b.number), b]));
  for (const b of bylaws) {
    if (b.repeals?.length) continue;           // a replacement, not an amendment
    const t = amendsTarget(b);
    if (t) add(t, b);
  }
  for (const law of laws) for (const n of law.amendments || []) if (byNum.has(n)) add(law.principal, byNum.get(n));
  const when = b => b.date_passed || b.meeting_date || '';
  for (const list of idx.values()) list.sort((a, b) => when(a).localeCompare(when(b)));
  return idx;
}

function amendedNote(list) {
  if (!list?.length) return '';
  const n = list.length;
  const times = n === 1 ? 'once' : n === 2 ? 'twice' : `${n} times`;
  const last = (list[n - 1].date_passed || list[n - 1].meeting_date || '').slice(0, 4);
  return n === 1 ? `Amended once${last ? `, in ${last}` : ''}` : `Amended ${times}${last ? `, most recently in ${last}` : ''}`;
}
