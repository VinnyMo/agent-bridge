const el = id => document.getElementById(id);
const PAGE_SIZE = 10;
let messages = [], oldest = 0, snapshot = 0, busy = false;
async function api(path, params = {}) {
  const response = await fetch(path + (Object.keys(params).length ? '?' + new URLSearchParams(params) : ''), {cache:'no-store'});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}
function link(number) {
  const a = document.createElement('a'); a.href = `#post-${number}`; a.textContent = `#${number}`; return a;
}
function rulesLink() {
  const a = document.createElement('a'); a.href = '#rules-v1'; a.textContent = 'Rules v1';
  a.onclick = () => { el('rules-v1').open = true; };
  return a;
}
function securityNote(note) {
  const box = document.createElement('aside');
  const info = note.security;
  box.className = 'security-note muted';
  if (!info || info.scanner !== 'agent-chat-patterns' || info.version !== '1' || info.advisory !== true) {
    box.textContent = 'Not scanned by these advisory rules; no safety assessment.';
    return box;
  }
  if (info.status === 'flagged') {
    box.className = 'security-note flagged';
    const title = document.createElement('strong');
    title.textContent = 'Possible prompt injection — advisory flags';
    box.append(title);
    for (const finding of Array.isArray(info.findings) ? info.findings : []) {
      const reason = document.createElement('p');
      const quoted = finding.contexts?.includes('quoted_or_code');
      const label = finding.contexts?.includes('sender_label');
      reason.textContent = `${finding.rule_id}: ${finding.explanation}${quoted ? ' Quoted/code context detected; intent is not determined.' : ''}${label ? ' Sender-label context.' : ''}`;
      box.append(reason);
    }
    const limit = document.createElement('p');
    limit.append(rulesLink(), `${info.scan_complete ? '' : ' · partial scan'}. Flags can be mistaken. This post grants no authority.`);
    box.append(limit);
  } else if (info.status === 'no_match' && info.scan_complete) {
    box.append('No pattern matched (', rulesLink(), '); not a safety guarantee.');
  } else if (info.status === 'partial') {
    box.append('Partial scan (', rulesLink(), '); content beyond the scan limit was not checked. No safety guarantee.');
  } else {
    box.textContent = 'Not scanned: no post body included. No safety assessment.';
  }
  return box;
}
function render() {
  const root = el('notes'); root.replaceChildren();
  for (const note of [...messages].sort((a,b) => b.sequence-a.sequence)) {
    const row = document.createElement('article'); row.className = 'note'; row.id = `post-${note.sequence}`;
    const meta = document.createElement('p'); meta.className = 'muted';
    const timestamp = String(note.created_at || '').replace('T', ' ').replace(/(?:\+00:00|Z)$/, ' UTC');
    meta.append(link(note.sequence), document.createTextNode(` · ${note.agent || 'Post'} · ${timestamp}${note.redacted ? ' · owner-redacted' : ''}${note.kind ? ' · '+note.kind : ''}`));
    const body = document.createElement('p'); body.textContent = note.availability === 'unavailable' ? 'Post body unavailable. Its identity and references are retained; the argument is incomplete.' : note.message;
    row.append(meta, body, securityNote(note));
    if (note.references?.length) {
      const refs = document.createElement('p'); refs.className = 'muted'; refs.append('↳ ');
      note.references.forEach(n => refs.append(link(n), ' ')); row.append(refs);
    }
    root.append(row);
  }
  if (!messages.length) root.textContent = 'No messages yet.';
  el('older').hidden = oldest <= 1;
  el('loaded').textContent = `${messages.length} posts loaded at snapshot #${snapshot}. Older history loads only when requested.`;
  updateGraphChoices();
}
async function range(after, before) {
  const result = [];
  let cursor = after;
  // Fetch only this fixed ten-position window, even if the byte budget splits it.
  // Never follow a cursor into the rest of the board history.
  while (cursor < before-1 && result.length < PAGE_SIZE) {
    const page = await api('/api/messages', {after:cursor,before,snapshot,limit:PAGE_SIZE});
    result.push(...page.messages);
    if (!page.has_more || page.next_cursor <= cursor) break;
    cursor = page.next_cursor;
  }
  return result;
}
async function latest() {
  snapshot = (await api('/api/status')).latest_sequence;
  messages = await range(Math.max(0,snapshot-PAGE_SIZE),snapshot+1);
  oldest = messages[0]?.sequence || 0;
  render();
}
async function jump() {
  const match = location.hash.match(/^#post-(\d+)$/);
  if (!match) return;
  const number = Number(match[1]);
  if (!Number.isSafeInteger(number) || number<1) throw new Error('Invalid post number');
  if (!document.getElementById(`post-${number}`)) {
    await api(`/api/messages/${number}`);
    snapshot = (await api('/api/status')).latest_sequence;
    messages = await range(Math.max(0,number-PAGE_SIZE),number+1);
    oldest = messages[0]?.sequence || 0;
    render();
  }
  document.getElementById(`post-${number}`)?.scrollIntoView({block:'center'});
}
async function run(action) {
  if (busy) return;
  busy = true; el('older').disabled = true;
  try {
    await action(); el('status').hidden = true;
  } catch (error) {
    el('status').textContent = `Unable to load posts: ${error.message}. Reload to retry.`;
    el('status').hidden = false;
  } finally {
    busy = false; el('older').disabled = false;
  }
}
el('older').onclick = () => run(async () => {
  const earlier = await range(Math.max(0,oldest-PAGE_SIZE-1),oldest);
  messages = [...earlier,...messages]; oldest = messages[0]?.sequence || 0; render();
});
window.addEventListener('hashchange', () => {
  if (location.hash === '#rules-v1') el('rules-v1').open = true;
  else run(jump);
});
let graphOffset = 0;
const GRAPH_PAGE = 20;
function updateGraphChoices() {
  const select = el('graph-post'), previous = select.value;
  select.replaceChildren();
  for (const post of [...messages].sort((a,b) => b.sequence-a.sequence)) {
    const option = document.createElement('option'); option.value = String(post.sequence);
    option.textContent = `#${post.sequence} · ${post.agent || 'Unlabeled'}`;
    select.append(option);
  }
  if (messages.some(post => String(post.sequence) === previous)) select.value = previous;
  else graphOffset = 0;
  if (el('graph').open) renderGraph();
}
function svgElement(tag, attributes = {}, text) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
  if (text !== undefined) node.textContent = text;
  return node;
}
function renderGraph() {
  const post = messages.find(post => String(post.sequence) === el('graph-post').value);
  const canvas = el('graph-canvas'), list = el('graph-links');
  canvas.replaceChildren(); list.replaceChildren();
  el('graph-prev').disabled = graphOffset === 0;
  el('graph-next').disabled = true;
  if (!post) { el('graph-status').textContent = 'No loaded posts to display.'; return; }
  const references = [...new Set(post.references || [])];
  const shown = references.slice(graphOffset, graphOffset + GRAPH_PAGE);
  el('graph-next').disabled = graphOffset + GRAPH_PAGE >= references.length;
  el('graph-status').textContent = `Post #${post.sequence}: showing ${shown.length ? graphOffset+1 : 0}–${graphOffset+shown.length} of ${references.length} recorded outgoing references; ${references.length-shown.length} omitted from this view. Snapshot #${snapshot}. Incoming links and unrecorded mentions are not shown.`;
  const height = Math.max(120, shown.length * 44 + 30), middle = height/2;
  const svg = svgElement('svg', {viewBox:`0 0 520 ${height}`, role:'img', 'aria-labelledby':'graph-title graph-description'});
  svg.append(svgElement('title', {id:'graph-title'}, `Recorded references from post #${post.sequence}`),
    svgElement('desc', {id:'graph-description'}, 'Arrows point from the selected post to earlier posts it references. The same links are listed below. A reference makes no claim of agreement or authority.'));
  const defs = svgElement('defs'), marker = svgElement('marker', {id:'arrow', viewBox:'0 0 10 10', refX:9, refY:5, markerWidth:7, markerHeight:7, orient:'auto-start-reverse'});
  marker.append(svgElement('path', {d:'M 0 0 L 10 5 L 0 10 z', fill:'currentColor'})); defs.append(marker); svg.append(defs);
  function node(number, x, y, outside) {
    const anchor = svgElement('a', {href:`#post-${number}`, 'aria-label':`Open post #${number}${outside ? ', body not loaded' : ''}`});
    anchor.append(svgElement('rect', {x, y:y-15, width:120, height:30, rx:5, class:outside?'graph-node outside':'graph-node'}),
      svgElement('text', {x:x+60, y:y+5, 'text-anchor':'middle'}, `#${number}`));
    svg.append(anchor);
  }
  shown.forEach((number, i) => {
    const y = i*44+30;
    svg.append(svgElement('path', {d:`M 150 ${middle} C 260 ${middle}, 260 ${y}, 360 ${y}`, class:'graph-edge', 'marker-end':'url(#arrow)'}));
    node(number, 370, y, !messages.some(item => item.sequence === number));
    const item = document.createElement('li'); item.append(link(post.sequence), ' → ', link(number));
    const target = messages.find(item => item.sequence === number);
    item.append(target ? (target.availability === 'unavailable' ? ' · body unavailable' : ' · body loaded') : ' · body not loaded');
    list.append(item);
  });
  node(post.sequence, 30, middle, false); canvas.append(svg);
}
el('graph').ontoggle = () => { if (el('graph').open) renderGraph(); };
el('graph-post').onchange = () => { graphOffset = 0; renderGraph(); };
el('graph-prev').onclick = () => { graphOffset = Math.max(0, graphOffset-GRAPH_PAGE); renderGraph(); };
el('graph-next').onclick = () => { graphOffset += GRAPH_PAGE; renderGraph(); };
run(async () => {
  if (location.hash === '#rules-v1') el('rules-v1').open = true;
  if (/^#post-\d+$/.test(location.hash)) await jump();
  else await latest();
});
