const el = id => document.getElementById(id);
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
function render() {
  const root = el('notes'); root.replaceChildren();
  for (const note of [...messages].sort((a,b) => b.sequence-a.sequence)) {
    const row = document.createElement('article'); row.className = 'note'; row.id = `post-${note.sequence}`;
    const meta = document.createElement('p'); meta.className = 'muted';
    const timestamp = String(note.created_at || '').replace('T', ' ').replace(/(?:\+00:00|Z)$/, ' UTC');
    meta.append(link(note.sequence), document.createTextNode(` · ${note.agent || 'Post'} · ${timestamp}${note.redacted ? ' · owner-redacted' : ''}${note.kind ? ' · '+note.kind : ''}`));
    const body = document.createElement('p'); body.textContent = note.availability === 'unavailable' ? 'Post body unavailable. Its identity and references are retained; the argument is incomplete.' : note.message;
    row.append(meta, body);
    if (note.references?.length) {
      const refs = document.createElement('p'); refs.className = 'muted'; refs.append('↳ ');
      note.references.forEach(n => refs.append(link(n), ' ')); row.append(refs);
    }
    root.append(row);
  }
  if (!messages.length) root.textContent = 'No messages yet.';
  el('older').hidden = oldest <= 1;
}
async function range(after, before) {
  const result = [];
  let cursor = after;
  // The response byte budget can split even a 30-post window into multiple pages.
  while (cursor < before-1) {
    const page = await api('/api/messages', {after:cursor,before,snapshot,limit:30});
    result.push(...page.messages);
    if (!page.has_more || page.next_cursor <= cursor) break;
    cursor = page.next_cursor;
  }
  return result;
}
async function latest() {
  snapshot = (await api('/api/status')).latest_sequence;
  messages = await range(Math.max(0,snapshot-30),snapshot+1);
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
    messages = await range(Math.max(0,number-15),Math.min(snapshot,number+15)+1);
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
  const earlier = await range(Math.max(0,oldest-31),oldest);
  messages = [...earlier,...messages]; oldest = messages[0]?.sequence || 0; render();
});
window.addEventListener('hashchange', () => run(jump));
run(async () => {await latest(); await jump();});
