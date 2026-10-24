// 用 Windows Node 验证已安装的适配器和 Windows -> WSL 接线；不调用模型。
// 参数：profile 目录、全局 hooks.json、已接入项目（可用 WSL UNC 路径）。
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
(async () => {
  const [profile, configPath, workspace] = process.argv.slice(2);
  assert(profile && configPath && workspace, '需要参数：profile configPath workspace');
  const plugin = await import(pathToFileURL(path.join(profile, 'node_modules/hooks-adapter/dsh/plugin.js')));
  const listeners = new Map();
  const injected = [], recorded = [], disposers = [];
  const ctx = {
    logger: { warn: (...args) => console.error(...args), debug() {}, info() {}, error() {} },
    get() { return undefined; },
    on(event, fn) { listeners.set(event, fn); },
    effect(fn) { disposers.push(fn()); },
  };
  plugin.apply(ctx, { discover: false, configPath });
  assert(listeners.has('agent/created'));
  assert(!listeners.has('agent/session-start'));
  const agent = { session: {
    header: { id: 'adapter-selfcheck-' + Date.now(), cwd: workspace },
    events: [{ type: 'turn/start', data: { turn: 1 } }],
    append(type, data) { recorded.push({ type, data }); },
  }, inject(message) { injected.push(message); }, steer() { throw Error('unexpected stop block'); } };
  const signal = new AbortController().signal;
  try {
    await listeners.get('agent/created')({ agent, source: 'startup', signal });
    assert.equal(injected.length, 1, '启动事件返回之前必须已注入上下文');
    assert.equal(injected[0].source.kind, 'hooks-adapter', 'V4 禁止通用 plugin 来源');
    assert(injected[0].content.some(x => x.text?.includes('continuity reminder')));
    await listeners.get('tools/post-execute')({ agent, name: 'write', arguments: { file_path: 'tmp/adapter-ignored.txt' }, callId: 'adapter-test', signal }, { content: [{ type: 'text', text: 'ok' }] }, async () => ({}));
    await listeners.get('agent/turn-stopping')({ agent, turn: 1, signal });
    const results = recorded.filter(x => x.type === 'hook/result');
    assert.equal(results.length, 2);
    assert(results.every(x => x.data.exitCode === 0 && x.data.decision === 'pass'));
    console.log('PASS: awaited SessionStart + V4 source + full tool arguments + Stop');
  } finally {
    for (const dispose of disposers.reverse()) if (typeof dispose === 'function') await dispose();
  }
})().catch(e => { console.error(e); process.exitCode = 1; });
