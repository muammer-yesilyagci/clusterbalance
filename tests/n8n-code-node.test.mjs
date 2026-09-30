// Runs alerts/n8n-code-node.js outside n8n with a fake $input / static data.
// Usage: node --test tests/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const SRC = readFileSync(new URL('../alerts/n8n-code-node.js', import.meta.url), 'utf8');
const node = new Function('$input', '$getWorkflowStaticData', SRC);

function run(health, staticData) {
  return node({ first: () => ({ json: health }) }, () => staticData);
}

const healthy = () => ({
  cluster: { ok: true, quorate: true, nodes: [{ name: 'pve1', online: true }, { name: 'pve2', online: true }, { name: 'pve3', online: true }] },
  storage: { ok: true, items: [{ storage: 'nfs-vms', node: 'pve1', path: '/mnt/nfs-vms', mounted: true }, { storage: 'nfs-vms', node: 'pve2', path: '/mnt/nfs-vms', mounted: true }] },
  ha: { ok: true, total: 5, bad: [] },
  backups: { ok: true, total: 20, newest_ok: 20, problems: [] },
  tasks: { ok: true, failed: [] },
});

test('first healthy check sends nothing', () => {
  const st = {};
  assert.deepEqual(run(healthy(), st), []);
  assert.equal(st.states.storage, 'OK');
});

test('unchanged state sends nothing', () => {
  const st = {};
  run(healthy(), st);
  assert.deepEqual(run(healthy(), st), []);
});

test('storage failure then recovery', () => {
  const st = {};
  run(healthy(), st);
  const bad = healthy();
  bad.storage.ok = false;
  bad.storage.items[1].mounted = false;
  const [mail] = run(bad, st);
  assert.match(mail.json.subject, /^\[SORUN\] Proxmox – nfs-vms pve2 üzerinde bağlı değil/);
  assert.match(mail.json.html, /mount/);
  assert.match(mail.json.text, /nfs-vms @ pve2: bağlı değil/);

  assert.deepEqual(run(bad, st), [], 'no repeat while still broken');

  const [ok] = run(healthy(), st);
  assert.match(ok.json.subject, /^\[DÜZELDİ\]/);
});

test('backup warnings are UYARI, not SORUN', () => {
  const st = {};
  run(healthy(), st);
  const h = healthy();
  h.backups = { ok: false, total: 20, newest_ok: 19, problems: [{ vmid: 131, name: 'erp-01', last: '2026-09-28 01:00', age_h: 30, level: 'warn' }] };
  const [mail] = run(h, st);
  assert.match(mail.json.subject, /^\[UYARI\]/);
});

test('each new failed task alerts once', () => {
  const st = {};
  run(healthy(), st);
  const h = healthy();
  h.tasks.failed = [{ time: '10:00', node: 'pve1', type: 'vzdump', id: '131', status: 'error' }];
  assert.equal(run(h, st).length, 1);
  assert.deepEqual(run(h, st), []);
  h.tasks.failed.unshift({ time: '11:00', node: 'pve2', type: 'qmigrate', id: '141', status: 'error' });
  assert.equal(run(h, st).length, 1);
});

test('unreachable panel', () => {
  const st = {};
  run(healthy(), st);
  const [mail] = run({ error: { message: 'ECONNREFUSED' } }, st);
  assert.match(mail.json.subject, /Panele ulaşılamıyor/);
  assert.match(mail.json.text, /ECONNREFUSED/);
});

test('html escapes values coming from the API', () => {
  const st = {};
  run(healthy(), st);
  const h = healthy();
  h.ha = { ok: false, total: 5, bad: [{ sid: 'vm:<script>', node: 'pve1', state: 'error', request_state: 'started' }] };
  const [mail] = run(h, st);
  assert.ok(!mail.json.html.includes('<script>'));
  assert.ok(mail.json.html.includes('&lt;script&gt;'));
});
