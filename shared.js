let BYLAWS = [];
let RESOLUTIONS = [];
let MEETINGS = [];
let BOARDS = [];
let ALL_MEETINGS = [];
let ALL_DATA = [];
let LAST_UPDATED = '';

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
  return `meeting-detail.html?date=${date}&body=${body}`;
}

function meetingBodyLabel(meeting) {
  if (meeting?.body) return meeting.body;
  if (meeting?.board_name) return meeting.board_name;
  if (meeting?.board_id === 'recreation') return 'Recreation';
  if (meeting?.board_id === 'museum') return 'Museum Board';
  if (meeting?.board_id === 'cemetery') return 'Cemetery';
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
  return 'tag';
}

function meetingDocPills(meeting) {
  const pills = [];
  if (meeting?.agenda_url) pills.push(`<a href="${escHtml(meeting.agenda_url)}" target="_blank" rel="noopener" class="tag" onclick="event.stopPropagation()">Agenda</a>`);
  if (meeting?.minutes_url) pills.push(`<a href="${escHtml(meeting.minutes_url)}" target="_blank" rel="noopener" class="tag" onclick="event.stopPropagation()">Minutes</a>`);
  if (meeting?.package_url) pills.push(`<a href="${escHtml(meeting.package_url)}" target="_blank" rel="noopener" class="tag" onclick="event.stopPropagation()">Package</a>`);
  if (meeting?.video_url) pills.push(`<a href="${escHtml(meeting.video_url)}" target="_blank" rel="noopener" class="tag" onclick="event.stopPropagation()">Video</a>`);
  return pills.join('');
}

function meetingCard(meeting, q = '') {
  const href = primaryMeetingUrl(meeting);

  return `
  <article class="result-card meeting-card" onclick="window.location='${escHtml(href)}'" style="cursor:pointer;">

    <div class="result-icon ri-meeting">
      📅
    </div>

    <div class="result-body">

      <div class="result-meta">
        <span class="result-type rt-meeting">${escHtml(meetingBodyLabel(meeting).toUpperCase())}</span>
        <span class="result-num">${escHtml(meetingTypeLabel(meeting))}</span>
        <span class="result-date">${escHtml(meeting.display_date || fmtDate(meeting.date))}</span>
      </div>

      <div class="result-title">
        ${highlight(meetingTypeLabel(meeting), q)}
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
  const dateText = bylaw?.date_passed ? fmtDate(bylaw.date_passed) : (bylaw?.meeting_date ? fmtDate(bylaw.meeting_date) : (bylaw?.year || ''));

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
              ? `<a href="meeting-detail.html?date=${encodeURIComponent(resolution.meeting_date || '')}&body=council" onclick="event.stopPropagation()">${escHtml(dateText)}</a>`
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

  ALL_MEETINGS = [...MEETINGS, ...flatBoards.meetings].sort((a, b) =>
    String(b.date || '').localeCompare(String(a.date || ''))
  );

  ALL_DATA = [...BYLAWS, ...RESOLUTIONS, ...ALL_MEETINGS];

  LAST_UPDATED =
    meetingsPayload?.last_updated ||
    bylawsPayload?.last_updated ||
    resolutionsPayload?.last_updated ||
    boardsPayload?.last_updated ||
    '';
}
