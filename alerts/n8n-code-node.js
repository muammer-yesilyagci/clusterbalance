// Proxmox sağlık kontrolü — yalnızca DURUM DEĞİŞİNCE e-posta üretir (HTML + düz metin).
const d = $input.first().json || {};
const st = $getWorkflowStaticData('global');
const prev = st.states || null;
const PANEL = 'https://YOUR-PVE-NODE:5000';  // panel adresi
const now = new Date().toLocaleString('tr-TR', { timeZone: 'Europe/Istanbul', dateStyle: 'long', timeStyle: 'short' });

const ORDER = ['panel', 'cluster', 'storage', 'ha', 'backups', 'tasks'];
const NAMES = { panel: 'Panel erişimi', cluster: 'Cluster / Quorum', storage: 'Depolama bağlantıları', ha: 'HA servisleri', backups: 'Yedekler (PBS)', tasks: 'Başarısız görevler' };
const HINTS = {
  panel: "Panelin calistigi node'da servisi kontrol edin: systemctl status clusterbalance-dashboard",
  cluster: "Erişilemeyen node'un açık olduğunu ve ağ kablosunu kontrol edin. Quorum yoksa HA çalışmaz.",
  storage: "İlgili node'da paylaşımlı diski bağlayın (ör. mount <yol>) ve depolama sunucusunun (NFS) açık olduğunu kontrol edin. Disk bağlı değilken o node'a VM taşınamaz.",
  ha: "Proxmox → Datacenter → HA ekranına bakın. Error durumundaki servis için: ha-manager set vm:ID --state disabled, ardından --state started.",
  backups: "PBS yedek görevinin çalıştığını ve yedek deposunda yer olduğunu kontrol edin.",
  tasks: "Proxmox arayüzünde Görevler (Tasks) listesinde ilgili görevi açıp hatayı inceleyin.",
};

const states = {}, summary = {}, headline = {}, details = {};
if (!d.cluster && !d.storage) {
  states.panel = 'SORUN';
  headline.panel = 'Panele ulaşılamıyor';
  summary.panel = 'Cevap yok';
  details.panel = [String((d.error && (d.error.message || d.error)) || 'Bağlantı kurulamadı').slice(0, 200)];
} else {
  states.panel = 'OK'; summary.panel = 'Erişilebilir';

  const c = d.cluster || {};
  const down = (c.nodes || []).filter(n => !n.online).map(n => n.name);
  states.cluster = c.ok ? 'OK' : 'SORUN';
  summary.cluster = `${(c.nodes || []).length - down.length}/${(c.nodes || []).length} node çevrimiçi, quorum ${c.quorate ? 'var' : 'YOK'}`;
  headline.cluster = !c.quorate ? 'Quorum kaybedildi' : `${down.join(', ')} erişilemiyor`;
  details.cluster = c.error ? [c.error] : down.map(n => `${n}: erişilemiyor`);

  const s = d.storage || {};
  const um = (s.items || []).filter(i => !i.mounted);
  states.storage = s.ok ? 'OK' : 'SORUN';
  summary.storage = `${(s.items || []).length - um.length}/${(s.items || []).length} bağlantı bağlı`;
  headline.storage = um.length ? `${um[0].storage} ${um.map(i => i.node).join(', ')} üzerinde bağlı değil` : 'Depolama okunamadı';
  details.storage = s.error ? [s.error] : um.map(i => `${i.storage} @ ${i.node}: bağlı değil (${i.path})`);

  const h = d.ha || {};
  const bad = h.bad || [];
  states.ha = h.ok ? 'OK' : 'SORUN';
  summary.ha = bad.length ? `${bad.length}/${h.total} serviste sorun` : `${h.total} servis normal`;
  headline.ha = bad.length ? `${bad[0].sid} ${bad[0].state} durumunda${bad.length > 1 ? ` (+${bad.length - 1})` : ''}` : 'HA okunamadı';
  details.ha = h.error ? [h.error] : bad.map(b => `${b.sid} @ ${b.node}: ${b.state} (olması gereken: ${b.request_state})`);

  const b = d.backups || {};
  const probs = b.problems || [];
  states.backups = b.ok ? 'OK' : (probs.length && probs.every(p => p.level === 'warn') ? 'UYARI' : 'SORUN');
  summary.backups = `${b.newest_ok ?? 0}/${b.total ?? 0} VM son 26 saatte yedeklendi`;
  headline.backups = `${probs.length} VM'in yedeği eski`;
  details.backups = b.error ? [b.error] : probs.map(p => `${p.vmid} ${p.name}: ${p.last ? `son yedek ${p.last} (${p.age_h} saat önce)` : 'hiç yedek yok'}`);

  const t = d.tasks || {};
  const failed = t.failed || [];
  // Her yeni başarısız görev yeni bir durum sayılır (aynı görev için tekrar mail atılmaz)
  states.tasks = failed.length ? 'SORUN#' + failed.map(x => `${x.time}|${x.node}|${x.type}|${x.id}`).sort().join(',') : 'OK';
  summary.tasks = failed.length ? `Son 24 saatte ${failed.length} başarısız görev` : 'Son 24 saatte yok';
  headline.tasks = failed.length ? `${failed.length} başarısız görev (son: ${failed[0].type} ${failed[0].id || ''} @ ${failed[0].node})` : '';
  details.tasks = failed.map(x => `${x.time} ${x.node} — ${x.type} ${x.id || ''}: ${x.status}`);
}
st.states = states;

const lab = v => (v || 'BILINMIYOR').split('#')[0];
const changed = ORDER.filter(k => k in states).filter(k => !prev ? lab(states[k]) !== 'OK'
  : lab(prev[k]) !== lab(states[k]) || (k === 'tasks' && prev[k] !== states[k] && states[k] !== 'OK'));
if (!changed.length) return [];

const active = ORDER.filter(k => k in states && lab(states[k]) !== 'OK');
const hasBad = active.some(k => lab(states[k]) === 'SORUN');
const level = hasBad ? 'SORUN' : active.length ? 'UYARI' : 'DUZELDI';
const TAG = { SORUN: 'SORUN', UYARI: 'UYARI', DUZELDI: 'DÜZELDİ' };
const COLOR = { SORUN: '#dc2626', UYARI: '#ca8a04', DUZELDI: '#16a34a' };
const PILL = { OK: ['#dcfce7', '#166534', 'Normal'], UYARI: ['#fef9c3', '#854d0e', 'Uyarı'], SORUN: ['#fee2e2', '#991b1b', 'Sorun'], BILINMIYOR: ['#f3f4f6', '#374151', '—'] };

const first = active[0];
const subject = level === 'DUZELDI'
  ? '[DÜZELDİ] Proxmox – tüm kontroller normale döndü'
  : `[${TAG[level]}] Proxmox – ${headline[first]}${active.length > 1 ? ` (+${active.length - 1} sorun daha)` : ''}`;
const lead = level === 'DUZELDI' ? 'Tüm kontroller normale döndü.'
  : `${active.length} kontrolde ${hasBad ? 'sorun' : 'uyarı'} var: ${active.map(k => NAMES[k]).join(', ')}.`;

const esc = s => String(s ?? '').replace(/[&<>"]/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch]));
const pill = v => { const [bg, fg, tx] = PILL[lab(v)] || PILL.BILINMIYOR; return `<span style="display:inline-block;padding:2px 10px;border-radius:10px;background:${bg};color:${fg};font-size:12px;font-weight:600">${tx}</span>`; };
const wasIs = k => `${prev ? (PILL[lab(prev[k])] || PILL.BILINMIYOR)[2] : 'ilk kontrol'} → ${lab(states[k]) === 'OK' ? 'Normal (düzeldi)' : PILL[lab(states[k])][2]}`;

let html = `<div style="background:#f4f5f7;padding:24px 0;font-family:Segoe UI,Arial,sans-serif;color:#1f2937">
<table role="presentation" width="100%" align="center" cellpadding="0" cellspacing="0" style="max-width:600px;background:#ffffff;border-radius:8px;overflow:hidden;border:1px solid #e5e7eb">
<tr><td style="background:${COLOR[level]};padding:18px 24px;color:#ffffff">
  <div style="font-size:12px;opacity:.9;letter-spacing:.5px">PROXMOX CLUSTER • ${TAG[level]}</div>
  <div style="font-size:20px;font-weight:600;margin-top:4px">${esc(level === 'DUZELDI' ? 'Tüm kontroller normal' : headline[first])}</div>
  <div style="font-size:13px;margin-top:6px;opacity:.95">${esc(lead)}</div>
</td></tr>
<tr><td style="padding:20px 24px">
  <div style="font-size:12px;color:#6b7280;margin-bottom:14px">${esc(now)}</div>
  <div style="font-size:14px;font-weight:600;margin-bottom:8px">Ne değişti?</div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="font-size:13px;margin-bottom:18px">
  ${changed.map(k => `<tr><td style="padding:6px 0;border-bottom:1px solid #f0f1f3">${esc(NAMES[k])}</td><td style="padding:6px 0;border-bottom:1px solid #f0f1f3;text-align:right;color:${lab(states[k]) === 'OK' ? '#166534' : COLOR[lab(states[k]) === 'UYARI' ? 'UYARI' : 'SORUN']};font-weight:600">${esc(wasIs(k))}</td></tr>`).join('')}
  </table>`;
if (active.length) {
  html += `<div style="font-size:14px;font-weight:600;margin-bottom:8px">Ne yapmalı?</div>`;
  for (const k of active) {
    html += `<div style="border-left:3px solid ${lab(states[k]) === 'UYARI' ? COLOR.UYARI : COLOR.SORUN};background:#fafafa;padding:10px 12px;margin-bottom:10px;font-size:13px">
      <div style="font-weight:600;margin-bottom:4px">${esc(NAMES[k])}: ${esc(headline[k])}</div>
      ${(details[k] || []).slice(0, 8).map(l => `<div style="color:#4b5563;font-family:Consolas,monospace;font-size:12px">• ${esc(l)}</div>`).join('')}
      ${(details[k] || []).length > 8 ? `<div style="color:#6b7280;font-size:12px">+${details[k].length - 8} satır daha</div>` : ''}
      <div style="margin-top:6px;color:#374151">👉 ${esc(HINTS[k])}</div></div>`;
  }
}
html += `<div style="font-size:14px;font-weight:600;margin:16px 0 8px">Genel durum</div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="font-size:13px">
  ${ORDER.filter(k => k in states).map(k => `<tr><td style="padding:7px 0;border-bottom:1px solid #f0f1f3;width:38%">${esc(NAMES[k])}</td><td style="padding:7px 0;border-bottom:1px solid #f0f1f3;width:22%">${pill(states[k])}</td><td style="padding:7px 0;border-bottom:1px solid #f0f1f3;color:#6b7280">${esc(summary[k] || '')}</td></tr>`).join('')}
  </table>
  <div style="margin-top:20px"><a href="${PANEL}" style="display:inline-block;background:#3b82f6;color:#ffffff;text-decoration:none;padding:10px 18px;border-radius:6px;font-size:14px;font-weight:600">Paneli aç</a></div>
</td></tr>
<tr><td style="padding:12px 24px;background:#f9fafb;font-size:11px;color:#9ca3af">Bu e-posta yalnızca bir kontrolün durumu değiştiğinde gönderilir (kontrol sıklığı: 5 dk). Kaynak: n8n “Proxmox Sağlık Uyarısı”.</td></tr>
</table></div>`;

const text = [subject, now, '', lead, '', 'Ne değişti?', ...changed.map(k => `- ${NAMES[k]}: ${wasIs(k)}`), '',
  ...(active.length ? ['Ne yapmalı?', ...active.flatMap(k => [`* ${NAMES[k]}: ${headline[k]}`, ...(details[k] || []).slice(0, 8).map(l => `    ${l}`), `    -> ${HINTS[k]}`]), ''] : []),
  'Genel durum:', ...ORDER.filter(k => k in states).map(k => `- ${NAMES[k]}: ${(PILL[lab(states[k])] || PILL.BILINMIYOR)[2]} — ${summary[k] || ''}`), '', `Panel: ${PANEL}`].join('\n');

return [{ json: { subject, html, text } }];
