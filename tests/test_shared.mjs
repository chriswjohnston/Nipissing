// Checks for shared.js helpers that sort and link decisions. Run: node tests/test_shared.mjs
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const el = () => ({ addEventListener() {}, appendChild() {}, setAttribute() {}, classList: { add() {}, remove() {}, toggle() {} } });
const ctx = {
  console, URL,
  window: { location: { href: 'https://nipissing.news/' } },
  document: { addEventListener() {}, querySelector: () => null, getElementById: () => null, createElement: el, head: el() },
};
vm.createContext(ctx);
vm.runInContext(readFileSync(new URL('../shared.js', import.meta.url), 'utf8'), ctx);
const run = code => vm.runInContext(code, ctx);

let n = 0;
const check = (name, fn) => { fn(); n++; };

check('operative words decide the kind', () => {
  const kind = t => run(`decisionKind(${JSON.stringify(t)})`);
  assert.equal(kind('That the statement of accounts for May be approved. Carried.'), 'Bills paid');
  assert.equal(kind('That council move into closed session under section 239.'), 'Closed sessions');
  assert.equal(kind('That council supports the resolution of the Town of Kearney. Carried.'), 'Support for municipalities');
  assert.equal(kind('That the next newsletter be mailed in June.'), 'Newsletters');
  assert.equal(run('DECISION_KINDS').includes(kind('Something unrelated entirely.')), true);
});

check('operativeText drops the WHEREAS and the outcome', () => {
  const t = run(`operativeText("WHEREAS the road is closed; NOW THEREFORE BE IT RESOLVED that the road be opened. Carried.")`);
  assert.equal(t, 'The road be opened.');
});

check('routine motions are recognised', () => {
  assert.equal(run(`isRoutineText("That the minutes of the regular meeting of May 6 be adopted.")`), true);
  assert.equal(run(`isRoutineText("That council award the culvert contract.")`), false);
});

check('resolution references link both ways', () => {
  const refs = run(`resolutionRefs([
    { number: 'R2026-09', motion_text: 'That swipe cards be in place by April 1, 2026.' },
    { number: 'R2026-77', motion_text: 'That the date set in Resolution R2026-09 be moved to September 30, 2026.' },
  ])`);
  const later = refs.get('R2026-77'), earlier = refs.get('R2026-09');
  assert.ok(later && later.refersTo.some(x => (x.number || x) === 'R2026-09' || JSON.stringify(x).includes('R2026-09')));
  assert.ok(earlier && earlier.referredBy.length === 1);
});

check('amendment target comes from the title', () => {
  assert.equal(run(`amendsTarget({ number: '2024-54', title: 'A By-law to amend By-law 2022-42' })`), '2022-42');
  assert.equal(run(`amendsTarget({ number: '2021-58', title: 'A By-law to regulate the landfill' })`), null);
});

check('dates read as local days', () => {
  assert.match(run(`fmtDate('2025-11-18')`), /18/);
  assert.equal(run(`fmtDate('')`), '');
});

console.log(`shared.js: ${n} checks passed`);
