// Run with: node --experimental-vm-modules tests/frontend_smoke.mjs
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const staticRoot = new URL('../src/musicscraper/web/static/', import.meta.url);
const context = vm.createContext({});
const modules = new Map();

async function loadModule(url) {
  if (!modules.has(url)) {
    const path = url.startsWith('/static/') ? url.slice('/static/'.length) : url.slice(1);
    const source = await readFile(new URL(path, staticRoot), 'utf8');
    modules.set(url, new vm.SourceTextModule(source, { context, identifier: url }));
  }
  return modules.get(url);
}

const entry = await loadModule('/app.js');
await entry.link(loadModule);

test('the browser entry point links every feature module', async () => {
  assert.equal(entry.status, 'linked');
  const html = await readFile(new URL('index.html', staticRoot), 'utf8');
  assert.match(html, /<script\b[^>]*type="module"[^>]*src="\/app\.js"/);
  assert.doesNotMatch(html, /\bon(?:click|submit|change)=/);
  const tabs = [...html.matchAll(/data-tab="([^"]+)"/g)].map((match) => match[1]);
  assert.deepEqual([...new Set(tabs)].sort(), ['artist', 'dashboard', 'releases', 'settings', 'tasks']);
  for (const tab of tabs) assert.ok(html.includes(`id="view-${tab}"`), `Missing tab pane: ${tab}`);
});

test('release markup keeps quoted identifiers and titles as inert data', async () => {
  const module = await loadModule('/static/js/release-view.js');
  await module.evaluate();
  const html = module.namespace.renderReleaseListItemHtml({
    id: 'release\'"<&',
    artist: 'Artist\'s <Name>',
    title: 'Night\'s "Album"',
    year: '2026',
    status: 'incomplete',
    found_count: 2,
    missing_count: 1,
    total_tracks_expected: 3,
    formats: ['FLAC'],
  }, null);
  assert.match(html, /data-release-id="release&#039;&quot;&lt;&amp;"/);
  assert.match(html, /Artist&#039;s &lt;Name&gt;/);
  assert.match(html, /Night&#039;s &quot;Album&quot;/);
  assert.match(html, /1 missing/);
  assert.doesNotMatch(html, /onclick|selectLibraryRelease/);
});

test('terminal output preserves ANSI formatting without interpreting log HTML', async () => {
  const module = await loadModule('/static/js/ansi.js');
  await module.evaluate();
  const { ansiToHtml } = module.namespace;
  assert.equal(ansiToHtml(null), '');
  assert.equal(ansiToHtml('old progress\rnew progress'), 'new progress');
  const styled = ansiToHtml('\x1b[31m<script>alert("log")</script>\x1b[0m');
  assert.match(styled, /<span class="ansi-fg-red">/);
  assert.match(styled, /&lt;script&gt;alert\(&quot;log&quot;\)&lt;\/script&gt;/);
  assert.doesNotMatch(styled, /<script>|\x1b/);
  assert.equal(ansiToHtml('\x1b]0;terminal title\x07visible'), 'visible');
});
