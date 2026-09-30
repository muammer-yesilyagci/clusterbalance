// Consistency checks for the dashboard translations (dashboard/templates/index.html).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const html = readFileSync(new URL('../dashboard/templates/index.html', import.meta.url), 'utf8');
const js = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).join('\n');
const dictSrc = js.match(/const I18N = (\{[\s\S]*?\n {8}\});/);
const I18N = new Function(`return ${dictSrc[1]}`)();
const langs = Object.keys(I18N);

test('every language has the same keys', () => {
  const base = Object.keys(I18N.en).sort();
  for (const l of langs) assert.deepEqual(Object.keys(I18N[l]).sort(), base, `language ${l}`);
});

test('every placeholder exists in all languages', () => {
  const ph = s => (s.match(/\{\w+\}/g) || []).sort().join();
  for (const k of Object.keys(I18N.en)) {
    for (const l of langs) assert.equal(ph(I18N[l][k]), ph(I18N.en[k]), `${l}.${k}`);
  }
});

test('all keys used in the page exist', () => {
  const used = new Set([
    ...[...js.matchAll(/\bt\('([\w. ]+)'/g)].map(m => m[1]),
    ...[...html.matchAll(/data-i18n(?:-html|-title|-placeholder)?="([^"]+)"/g)].map(m => m[1]),
  ]);
  const missing = [...used].filter(k => !k.endsWith('.') && !(k in I18N.en));
  assert.deepEqual(missing, []);
});

test('nothing shadows the t() helper', () => {
  const bad = js.split('\n').filter(l => /\b(const|let|var)\s+t\b|[(,]\s*t\s*[,)]\s*=>|\bt\s*=>/.test(l));
  assert.deepEqual(bad, []);
});

test('Turkish texts use Turkish characters', () => {
  const ascii = /\b(Yukleniyor|Tasima|Gecmis|Basarili|basarisiz|Bakim|Saglik|Ayarlari)\b/;
  for (const [k, v] of Object.entries(I18N.tr)) assert.ok(!ascii.test(v), `tr.${k}: ${v}`);
});
