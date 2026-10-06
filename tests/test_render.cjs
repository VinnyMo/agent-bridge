// Minimal DOM harness: exercise actual rendering with inert hostile strings.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this._text = ''; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(c => typeof c === 'string' ? c : c.textContent).join(''); }
  set innerHTML(_) { throw new Error('HTML parsing is forbidden for board text'); }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this._text = ''; this.children = items; }
}
test('messages, labels, errors and timestamps render as text; references stay local', async () => {
  const nodes = Object.fromEntries(['notes', 'older', 'status'].map(id => [id, new Element('div')]));
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
