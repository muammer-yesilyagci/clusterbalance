// ClusterBalance - Proxmox UI Widget v2.1
// Original author: Cemal Demirci (github.com/cemal-demirci) - https://github.com/muammer-yesilyagci/clusterbalance
(function() {
    var DASHBOARD = "__DASHBOARD_URL__";  // install.sh tarafindan doldurulur, or: https://pve1.example.lan:5000
    var VERSION = "2.1";

    var I18N = {
        en: {
            subtitle: "Proxmox load balancer", nodes: "Nodes", vms: "VMs running", balance: "Balance",
            connecting: "Connecting…", balanced: "Cluster is balanced", imbalance: "Imbalance", critical: "Critical imbalance",
            gap: "gap {d} pts · threshold 15", conn_error: "Dashboard not reachable",
            open_dash: "Open dashboard", import: "Import", settings: "Settings", close: "Close"
        },
        tr: {
            subtitle: "Proxmox yük dengeleyici", nodes: "Node", vms: "Çalışan VM", balance: "Denge",
            connecting: "Bağlanıyor…", balanced: "Cluster dengeli", imbalance: "Dengesizlik var", critical: "Ciddi dengesizlik",
            gap: "fark {d} puan · eşik 15", conn_error: "Panele ulaşılamıyor",
            open_dash: "Paneli aç", import: "İçe Aktar", settings: "Ayarlar", close: "Kapat"
        }
    };

    function lang() {
        var l = null;
        try { l = localStorage.getItem("cb_language"); } catch (e) {}
        if (!l) l = (navigator.language || "").toLowerCase().indexOf("tr") === 0 ? "tr" : "en";
        return I18N[l] ? l : "en";
    }

    // su terazisi logosu: koyu zemin, beyaz terazi, yesil kabarcik
    var LOGO = '<svg viewBox="0 0 100 100" aria-hidden="true"><rect x="8" y="33" width="84" height="34" rx="17" fill="#fff" fill-opacity=".2" stroke="#fff" stroke-width="7"/>' +
        '<line x1="35" y1="40" x2="35" y2="60" stroke="#fff" stroke-width="5" stroke-linecap="round"/><line x1="65" y1="40" x2="65" y2="60" stroke="#fff" stroke-width="5" stroke-linecap="round"/>' +
        '<ellipse cx="50" cy="50" rx="10" ry="8" fill="#30d158"/></svg>';
    var ICON = {
        dash: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 13h8V3H3v10zm0 8h8v-6H3v6zm10 0h8V11h-8v10zm0-18v6h8V3h-8z"/></svg>',
        imp: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M11 3h2v9.17l3.59-3.58L18 10l-6 6-6-6 1.41-1.41L11 12.17zM4 19h16v2H4z"/></svg>',
        gear: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19.14 12.94a7.07 7.07 0 0 0 0-1.88l2.03-1.58a.5.5 0 0 0 .12-.61l-1.92-3.32a.5.5 0 0 0-.59-.22l-2.39.96a7 7 0 0 0-1.62-.94l-.36-2.54a.5.5 0 0 0-.48-.41h-3.84a.5.5 0 0 0-.47.41l-.36 2.54a7 7 0 0 0-1.62.94l-2.39-.96a.5.5 0 0 0-.59.22L2.74 8.87a.5.5 0 0 0 .12.61l2.03 1.58a7.07 7.07 0 0 0 0 1.88l-2.03 1.58a.5.5 0 0 0-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54a7 7 0 0 0 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32a.5.5 0 0 0-.12-.61zM12 15.6a3.6 3.6 0 1 1 0-7.2 3.6 3.6 0 0 1 0 7.2z"/></svg>'
    };

    var CSS = `
        #cb-widget { position: fixed; bottom: 20px; right: 20px; z-index: 99999;
            font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Segoe UI', system-ui, sans-serif; -webkit-font-smoothing: antialiased; }
        #cb-fab { width: 54px; height: 54px; border-radius: 15px; border: 0; cursor: pointer; padding: 0;
            background: linear-gradient(180deg, #3a3a3c, #0b0b0d); display: flex; align-items: center; justify-content: center;
            box-shadow: inset 0 1px 0 rgba(255,255,255,.18), 0 0 0 1px rgba(255,255,255,.14), 0 10px 28px rgba(0,0,0,.35);
            transition: transform .2s cubic-bezier(.4,0,.2,1), box-shadow .2s; }
        #cb-fab:hover { transform: translateY(-2px) scale(1.04); }
        #cb-fab:focus-visible, #cb-panel button:focus-visible { outline: 3px solid rgba(10,132,255,.6); outline-offset: 2px; }
        #cb-fab svg { width: 38px; height: 38px; }
        #cb-panel { position: absolute; bottom: 68px; right: 0; width: 340px; color: #f5f5f7; border-radius: 20px; overflow: hidden;
            background: rgba(30,30,32,.86); backdrop-filter: blur(30px) saturate(160%); -webkit-backdrop-filter: blur(30px) saturate(160%);
            border: 1px solid rgba(255,255,255,.12); box-shadow: 0 30px 70px rgba(0,0,0,.45);
            opacity: 0; visibility: hidden; transform: translateY(12px) scale(.97); transform-origin: bottom right;
            transition: opacity .22s, transform .22s cubic-bezier(.4,0,.2,1), visibility .22s; }
        #cb-panel.open { opacity: 1; visibility: visible; transform: none; }
        .cb-head { display: flex; align-items: center; gap: 12px; padding: 18px 18px 14px; }
        .cb-logo { width: 40px; height: 40px; border-radius: 11px; flex: none; display: flex; align-items: center; justify-content: center;
            background: linear-gradient(180deg, #3a3a3c, #0b0b0d); box-shadow: inset 0 1px 0 rgba(255,255,255,.18), 0 0 0 1px rgba(255,255,255,.14); }
        .cb-logo svg { width: 30px; height: 30px; }
        .cb-head h3 { margin: 0; font-size: 16px; font-weight: 700; letter-spacing: -.01em; color: #fff; }
        .cb-head span { font-size: 12px; color: #a1a1a6; }
        .cb-x { margin-left: auto; width: 28px; height: 28px; border-radius: 14px; border: 0; cursor: pointer; color: #a1a1a6;
            background: rgba(118,118,128,.24); font-size: 16px; line-height: 28px; padding: 0; }
        .cb-x:hover { color: #fff; }
        .cb-hero { margin: 0 14px; padding: 14px; border-radius: 16px; background: rgba(255,255,255,.06); display: flex; align-items: center; gap: 14px; }
        .cb-hero svg { width: 92px; height: 92px; flex: none; }
        .cb-ring { transition: stroke-dasharray .8s ease; }
        .cb-vals { display: grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: 8px 12px; flex: 1; }
        .cb-val b { display: block; font-size: 20px; font-weight: 700; letter-spacing: -.01em; color: #fff; line-height: 1.1; }
        .cb-val i { font-style: normal; font-size: 11px; font-weight: 600; }
        .cb-status { margin: 10px 14px 0; padding: 11px 13px; border-radius: 13px; display: flex; align-items: center; gap: 10px;
            background: rgba(48,209,88,.14); }
        .cb-status.warning { background: rgba(255,159,10,.16); }
        .cb-status.error { background: rgba(255,69,58,.18); }
        .cb-dot { width: 10px; height: 10px; border-radius: 50%; flex: none; background: #30d158; box-shadow: 0 0 0 4px rgba(48,209,88,.18); }
        .cb-status.warning .cb-dot { background: #ff9f0a; box-shadow: 0 0 0 4px rgba(255,159,10,.2); }
        .cb-status.error .cb-dot { background: #ff453a; box-shadow: 0 0 0 4px rgba(255,69,58,.2); }
        .cb-status p { margin: 0; font-size: 13px; font-weight: 600; color: #fff; }
        .cb-status small { display: block; font-size: 11px; font-weight: 500; color: #c7c7cc; margin-top: 1px; }
        .cb-actions { padding: 14px; display: flex; flex-direction: column; gap: 8px; }
        .cb-row { display: flex; gap: 8px; }
        .cb-btn { flex: 1; min-height: 40px; border-radius: 11px; border: 0; cursor: pointer; font: inherit; font-size: 13px; font-weight: 600;
            display: flex; align-items: center; justify-content: center; gap: 8px; color: #f5f5f7; background: rgba(118,118,128,.24);
            transition: background .15s, transform .15s; }
        .cb-btn:hover { background: rgba(118,118,128,.36); }
        .cb-btn.primary { background: #0a84ff; color: #fff; }
        .cb-btn.primary:hover { background: #409cff; }
        .cb-btn svg { width: 16px; height: 16px; fill: currentColor; }
        .cb-foot { padding: 0 14px 12px; text-align: center; font-size: 11px; color: #8e8e93; }
    `;

    function ringSvg() {
        // denge (yesil) / RAM (pembe) / CPU (mavi) halkalari, paneldeki gibi
        var rings = [[42, "#30d158", "cb-r-bal"], [31, "#ff375f", "cb-r-ram"], [20, "#64d2ff", "cb-r-cpu"]];
        return '<svg viewBox="0 0 100 100" role="img" aria-hidden="true">' + rings.map(function(r) {
            return '<circle cx="50" cy="50" r="' + r[0] + '" fill="none" stroke="' + r[1] + '" stroke-opacity=".2" stroke-width="9"/>' +
                '<circle id="' + r[2] + '" class="cb-ring" cx="50" cy="50" r="' + r[0] + '" fill="none" stroke="' + r[1] + '" stroke-width="9" stroke-linecap="round" stroke-dasharray="0 ' + (2 * Math.PI * r[0]).toFixed(1) + '" transform="rotate(-90 50 50)"/>';
        }).join("") + '</svg>';
    }

    function setRing(id, v) {
        var c = document.getElementById(id);
        if (!c) return;
        var len = 2 * Math.PI * c.r.baseVal.value;
        c.style.strokeDasharray = (len * Math.min(100, Math.max(0, v || 0)) / 100) + " " + len;
    }

    function init() {
        if (document.getElementById("cb-widget")) return;
        var L = I18N[lang()];

        var style = document.createElement("style");
        style.textContent = CSS;
        document.head.appendChild(style);

        var widget = document.createElement("div");
        widget.id = "cb-widget";
        widget.innerHTML = `
            <div id="cb-panel" role="dialog" aria-label="ClusterBalance">
                <div class="cb-head">
                    <div class="cb-logo">${LOGO}</div>
                    <div><h3>ClusterBalance</h3><span>${L.subtitle}</span></div>
                    <button class="cb-x" id="cb-close" title="${L.close}" aria-label="${L.close}">&times;</button>
                </div>
                <div class="cb-hero">
                    ${ringSvg()}
                    <div class="cb-vals">
                        <div class="cb-val"><i style="color:#30d158">${L.balance}</i><b id="cb-bal">-</b></div>
                        <div class="cb-val"><i style="color:#ff6b8b">RAM</i><b id="cb-mem">-</b></div>
                        <div class="cb-val"><i style="color:#64d2ff">CPU</i><b id="cb-cpu">-</b></div>
                        <div class="cb-val"><i style="color:#a1a1a6">${L.nodes}</i><b id="cb-nodes">-</b></div>
                        <div class="cb-val" style="grid-column: span 2"><i style="color:#a1a1a6">${L.vms}</i><b id="cb-vms">-</b></div>
                    </div>
                </div>
                <div class="cb-status" id="cb-status"><span class="cb-dot"></span><p><span id="cb-st">${L.connecting}</span><small id="cb-st2"></small></p></div>
                <div class="cb-actions">
                    <button class="cb-btn primary" id="cb-open-dash">${ICON.dash} ${L.open_dash}</button>
                    <div class="cb-row">
                        <button class="cb-btn" id="cb-open-import">${ICON.imp} ${L.import}</button>
                        <button class="cb-btn" id="cb-open-settings">${ICON.gear} ${L.settings}</button>
                    </div>
                </div>
                <div class="cb-foot">ClusterBalance v${VERSION}</div>
            </div>
            <button id="cb-fab" title="ClusterBalance" aria-label="ClusterBalance" aria-controls="cb-panel" aria-expanded="false">${LOGO}</button>
        `;
        document.body.appendChild(widget);

        var panel = document.getElementById("cb-panel"), fab = document.getElementById("cb-fab");
        function setOpen(open) {
            panel.classList.toggle("open", open);
            fab.setAttribute("aria-expanded", open ? "true" : "false");
            if (open) loadStats();
        }
        fab.onclick = function() { setOpen(!panel.classList.contains("open")); };
        document.getElementById("cb-close").onclick = function() { setOpen(false); };
        document.getElementById("cb-open-dash").onclick = function() { window.open(DASHBOARD, "_blank", "noopener"); };
        document.getElementById("cb-open-import").onclick = function() { window.open(DASHBOARD + "/#import", "_blank", "noopener"); };
        document.getElementById("cb-open-settings").onclick = function() { window.open(DASHBOARD + "/#settings", "_blank", "noopener"); };
        document.addEventListener("click", function(e) { if (!e.target.closest("#cb-widget")) setOpen(false); });
        document.addEventListener("keydown", function(e) { if (e.key === "Escape" && panel.classList.contains("open")) setOpen(false); });

        loadStats();
        setInterval(loadStats, 30000);
        console.log("ClusterBalance: Widget v" + VERSION + " loaded - https://github.com/muammer-yesilyagci/clusterbalance");
    }

    function loadStats() {
        var L = I18N[lang()];
        var status = document.getElementById("cb-status");
        var st = document.getElementById("cb-st"), st2 = document.getElementById("cb-st2");
        fetch(DASHBOARD + "/api/widget-summary")
            .then(function(r) { return r.json(); })
            .then(function(d) {
                var cpu = d.avg_cpu || 0, mem = d.avg_mem || 0, diff = d.max_diff || 0;
                var bal = d.balance != null ? d.balance : Math.max(0, 100 - diff);
                document.getElementById("cb-nodes").textContent = d.node_count || "-";
                document.getElementById("cb-vms").textContent = (d.running_vms != null ? d.running_vms : "-") + (d.total ? " / " + d.total : "");
                document.getElementById("cb-cpu").textContent = cpu.toFixed(1) + "%";
                document.getElementById("cb-mem").textContent = mem.toFixed(1) + "%";
                document.getElementById("cb-bal").textContent = Math.round(bal);
                setRing("cb-r-bal", bal); setRing("cb-r-ram", mem); setRing("cb-r-cpu", cpu);
                var level = d.balanced !== false && diff < 15 ? "" : diff < 25 ? "warning" : "error";
                status.className = "cb-status" + (level ? " " + level : "");
                st.textContent = level === "" ? L.balanced : level === "warning" ? L.imbalance : L.critical;
                st2.textContent = L.gap.replace("{d}", diff.toFixed(1));
            })
            .catch(function(e) {
                status.className = "cb-status error";
                st.textContent = L.conn_error;
                st2.textContent = DASHBOARD.replace(/^https?:\/\//, "");
                console.log("ClusterBalance Error:", e);
            });
    }

    if (document.readyState === "complete") {
        init();
    } else {
        window.addEventListener("load", init);
    }
})();
