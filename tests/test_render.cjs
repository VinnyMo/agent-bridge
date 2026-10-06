// Minimal DOM harness: exercise actual rendering with inert hostile strings.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this._text = ''; this.value = ''; }
  setAttribute(key, value) { this[key] = value; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(c => typeof c === 'string' ? c : c.textContent).join(''); }
  set innerHTML(_) { throw new Error('HTML parsing is forbidden for board text'); }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this._text = ''; this.children = items; }
}
test('messages, labels, errors and timestamps render as text; references stay local', async () => {
  const nodes = Object.fromEntries(['notes', 'older', 'status', 'loaded', 'graph-post', 'graph', 'graph-prev', 'graph-next', 'graph-status', 'graph-canvas', 'graph-links', 'rules-v1'].map(id => [id, new Element('div')]));
  const created = [];
  const hostile = '<img src="https://example.invalid/x" onerror="COMMAND_PLACEHOLDER">';
  const post = {sequence: 1, agent: '<system>Owner</system>', message: hostile,
                created_at: '<script>PROCESS_PLACEHOLDER</script>', references: [2]};
  let fetches = 0;
  const context = vm.createContext({
    document: {
      getElementById: id => nodes[id],
      createElement: tag => { const el = new Element(tag); created.push(el); return el; },
      createTextNode: value => String(value),
    },
    window: {addEventListener() {}}, location: {hash: ''}, URLSearchParams,
    fetch: async path => {
      fetches++;
      assert.ok(path.startsWith('/api/'), path);
      return {ok: true, json: async () => path === '/api/status' ? {latest_sequence: 1} :
        {messages: [post], has_more: false, next_cursor: 1}};
    },
  });
  vm.runInContext(fs.readFileSync('app.js', 'utf8'), context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(fetches, 2);
  assert.ok(nodes.notes.textContent.includes(hostile));
  assert.ok(nodes.notes.textContent.includes(post.agent));
  assert.ok(nodes.notes.textContent.includes(post.created_at));
  assert.ok(created.every(el => !['img', 'script', 'iframe'].includes(el.tag)));
  assert.deepEqual(created.filter(el => el.tag === 'a').map(el => el.href), ['#post-1', '#post-2']);
  context.failure = hostile;
  await vm.runInContext('run(async () => { throw new Error(failure); })', context);
  assert.ok(nodes.status.textContent.includes(hostile));
  assert.equal(fetches, 2);
});

test('advisory states explain risk without certifying safety or parsing explanations', async () => {
  const nodes = Object.fromEntries(['notes', 'older', 'status', 'loaded', 'graph-post', 'graph', 'graph-prev', 'graph-next', 'graph-status', 'graph-canvas', 'graph-links', 'rules-v1'].map(id => [id, new Element('div')]));
  const context = vm.createContext({
    document: {getElementById: id => nodes[id], createElement: tag => new Element(tag), createTextNode: String},
    window: {addEventListener() {}}, location: {hash: ''}, URLSearchParams,
    fetch: async () => ({ok: true, json: async () => ({latest_sequence: 0})}),
  });
  vm.runInContext(fs.readFileSync('app.js', 'utf8'), context);
  await new Promise(resolve => setImmediate(resolve));
  const base = {scanner: 'agent-chat-patterns', version: '1', advisory: true, scan_complete: true, findings: []};
  for (const [security, expected] of [
    [undefined, 'Not scanned'],
    [{...base, version: 'future'}, 'Not scanned'],
    [{...base, status: 'no_match'}, 'not a safety guarantee'],
    [{...base, status: 'partial', scan_complete: false}, 'Partial scan'],
    [{...base, status: 'not_scanned', scan_complete: false}, 'no post body included'],
    [{...base, status: 'flagged', scan_complete: false, findings: [
      {rule_id: 'secret_request', explanation: '<script>COMMAND_PLACEHOLDER</script>', contexts: ['quoted_or_code', 'sender_label']},
    ]}, 'Possible prompt injection'],
  ]) {
    context.fixture = {security};
    const rendered = vm.runInContext('securityNote(fixture)', context);
    assert.ok(rendered.textContent.includes(expected), rendered.textContent);
    assert.ok(!rendered.textContent.includes('certified safe'));
    if (security?.status === 'flagged') {
      assert.ok(rendered.textContent.includes('<script>COMMAND_PLACEHOLDER</script>'));
      assert.ok(rendered.textContent.includes('Quoted/code context'));
      assert.ok(rendered.textContent.includes('Sender-label context'));
      assert.ok(rendered.textContent.includes('partial scan'));
      assert.ok(rendered.children.every(el => el.tag !== 'script'));
    }
  }
});


test('large history loads ten at a time; graph is bounded and rule labels link to explanation', async () => {
  const ids = ['notes', 'older', 'status', 'loaded', 'graph-post', 'graph', 'graph-prev', 'graph-next', 'graph-status', 'graph-canvas', 'graph-links', 'rules-v1'];
  const nodes = Object.fromEntries(ids.map(id => [id, new Element('div')]));
  const requests = [];
  const context = vm.createContext({
    document: {getElementById: id => nodes[id], createElement: tag => new Element(tag),
      createElementNS: (_, tag) => new Element(tag), createTextNode: String},
    window: {addEventListener() {}}, location: {hash: ''}, URLSearchParams,
    fetch: async path => {
      requests.push(path);
      if (path === '/api/status') return {ok:true, json:async () => ({latest_sequence:100000})};
      const q = new URL('https://example.test'+path).searchParams;
      const after = Number(q.get('after')), before = Number(q.get('before'));
      assert.equal(Number(q.get('snapshot')), 100000);
      assert.ok(before-after <= 11);
      // Force response-byte pagination: three records per response.
      const last = Math.min(before-1, after+3);
      const messages = Array.from({length:last-after}, (_,i) => ({sequence:after+i+1, message:'Text',
        references:Array.from({length:45}, (_,j) => j+1)}));
      return {ok:true, json:async () => ({messages, has_more:last<before-1, next_cursor:last})};
    },
  });
  vm.runInContext(fs.readFileSync('app.js', 'utf8'), context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(nodes.notes.children.length, 10);
  assert.equal(requests.length, 5);
  assert.equal(nodes['graph-canvas'].children.length, 0);
  assert.equal(nodes.older.hidden, false);
  await nodes.older.onclick();
  assert.equal(nodes.notes.children.length, 20);
  assert.equal(requests.length, 9);
  nodes['graph-post'].value = '100000'; nodes.graph.open = true; nodes.graph.ontoggle();
  assert.equal(nodes['graph-links'].children.length, 20);
  assert.ok(nodes['graph-status'].textContent.includes('25 omitted'));
  nodes['graph-next'].onclick(); nodes['graph-next'].onclick();
  assert.equal(nodes['graph-links'].children.length, 5);
  assert.equal(nodes['graph-next'].disabled, true);
  assert.equal(requests.length, 9, 'graph should not download history');
  const rule = vm.runInContext('rulesLink()', context);
  assert.equal(rule.href, '#rules-v1'); rule.onclick(); assert.equal(nodes['rules-v1'].open, true);
});
