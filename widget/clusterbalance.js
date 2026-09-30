// ClusterBalance - Proxmox UI Widget v2.0
// Original author: Cemal Demirci (github.com/cemal-demirci) - https://github.com/muammer-yesilyagci/clusterbalance
(function() {
    var DASHBOARD = "__DASHBOARD_URL__";  // install.sh tarafindan doldurulur, or: https://pve1.example.lan:5000

    function init() {
        if (document.getElementById("cb-widget")) return;

        var css = `
            #cb-widget {
                position: fixed;
                bottom: 20px;
                right: 20px;
                z-index: 99999;
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            }

            #cb-fab {
                width: 56px;
                height: 56px;
                border-radius: 28px;
                background: linear-gradient(135deg, #f97316, #ea580c);
                border: none;
                cursor: pointer;
                box-shadow: 0 4px 20px rgba(249, 115, 22, 0.4);
                display: flex;
                align-items: center;
                justify-content: center;
                transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            }

            #cb-fab:hover {
                transform: scale(1.1) rotate(5deg);
                box-shadow: 0 6px 25px rgba(249, 115, 22, 0.5);
            }

            #cb-fab svg {
                width: 26px;
                height: 26px;
                fill: white;
            }

            #cb-panel {
                position: absolute;
                bottom: 70px;
                right: 0;
                width: 360px;
                background: linear-gradient(180deg, #1e293b 0%, #0f172a 100%);
                border-radius: 20px;
                box-shadow: 0 25px 60px rgba(0, 0, 0, 0.5);
                opacity: 0;
                visibility: hidden;
                transform: translateY(20px) scale(0.95);
                transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
                border: 1px solid rgba(255, 255, 255, 0.1);
                overflow: hidden;
            }

            #cb-panel.open {
                opacity: 1;
                visibility: visible;
                transform: translateY(0) scale(1);
            }

            .cb-header {
                padding: 24px;
                background: linear-gradient(135deg, #f97316 0%, #ea580c 100%);
                display: flex;
                align-items: center;
                gap: 14px;
            }

            .cb-header svg {
                width: 36px;
                height: 36px;
                fill: white;
            }

            .cb-header-text h3 {
                margin: 0;
                color: white;
                font-size: 18px;
                font-weight: 700;
            }

            .cb-header-text span {
                color: rgba(255, 255, 255, 0.85);
                font-size: 12px;
            }

            .cb-stats {
                padding: 20px;
                display: grid;
                grid-template-columns: repeat(2, 1fr);
                gap: 14px;
            }

            .cb-stat {
                background: rgba(255, 255, 255, 0.05);
                border-radius: 14px;
                padding: 16px;
                text-align: center;
                transition: all 0.2s;
            }

            .cb-stat:hover {
                background: rgba(255, 255, 255, 0.08);
                transform: translateY(-2px);
            }

            .cb-stat-value {
                font-size: 28px;
                font-weight: 700;
                color: #f97316;
                line-height: 1;
            }

            .cb-stat-label {
                font-size: 11px;
                color: #cbd5e1;
                text-transform: uppercase;
                margin-top: 6px;
                letter-spacing: 0.5px;
            }

            .cb-status {
                margin: 0 20px;
                padding: 14px 16px;
                background: rgba(34, 197, 94, 0.1);
                border: 1px solid rgba(34, 197, 94, 0.3);
                border-radius: 12px;
                display: flex;
                align-items: center;
                gap: 12px;
            }

            .cb-status.warning {
                background: rgba(249, 115, 22, 0.1);
                border-color: rgba(249, 115, 22, 0.3);
            }

            .cb-status.error {
                background: rgba(239, 68, 68, 0.1);
                border-color: rgba(239, 68, 68, 0.3);
            }

            .cb-status-dot {
                width: 12px;
                height: 12px;
                border-radius: 50%;
                background: #22c55e;
                box-shadow: 0 0 10px rgba(34, 197, 94, 0.5);
            }

            .cb-status.warning .cb-status-dot {
                background: #f97316;
                box-shadow: 0 0 10px rgba(249, 115, 22, 0.5);
            }

            .cb-status.error .cb-status-dot {
                background: #ef4444;
                box-shadow: 0 0 10px rgba(239, 68, 68, 0.5);
            }

            .cb-status-text {
                color: #e2e8f0;
                font-size: 13px;
                font-weight: 500;
            }

            .cb-actions {
                padding: 20px;
                display: flex;
                flex-direction: column;
                gap: 10px;
            }

            .cb-btn {
                padding: 14px 18px;
                border-radius: 12px;
                border: none;
                cursor: pointer;
                font-size: 14px;
                font-weight: 600;
                display: flex;
                align-items: center;
                justify-content: center;
                gap: 10px;
                transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
            }

            .cb-btn-primary {
                background: linear-gradient(135deg, #f97316, #ea580c);
                color: white;
            }

            .cb-btn-primary:hover {
                transform: translateY(-2px);
                box-shadow: 0 6px 20px rgba(249, 115, 22, 0.4);
            }

            .cb-btn-secondary {
                background: rgba(255, 255, 255, 0.05);
                color: #e2e8f0;
                border: 1px solid rgba(255, 255, 255, 0.1);
            }

            .cb-btn-secondary:hover {
                background: rgba(255, 255, 255, 0.1);
            }

            .cb-btn svg {
                width: 18px;
                height: 18px;
                fill: currentColor;
            }

            .cb-row {
                display: flex;
                gap: 10px;
            }

            .cb-row .cb-btn {
                flex: 1;
            }

            .cb-footer {
                padding: 16px 20px;
                border-top: 1px solid rgba(255, 255, 255, 0.05);
                text-align: center;
            }

            .cb-footer a {
                color: #94a3b8;
                font-size: 11px;
                text-decoration: none;
                transition: color 0.2s;
            }

            .cb-footer a:hover {
                color: #f97316;
            }

            #cb-panel {
                color: #f1f5f9;
            }
        `;

        var style = document.createElement("style");
        style.textContent = css;
        document.head.appendChild(style);

        // Language support
        var lang = localStorage.getItem("cb_language") || "en";
        var i18n = {
            en: {
                subtitle: "Proxmox Load Balancer",
                nodes: "Nodes",
                vms: "VMs",
                avg_cpu: "Avg CPU",
                avg_ram: "Avg RAM",
                connecting: "Connecting...",
                balanced: "Cluster Balanced",
                imbalance: "Imbalance",
                critical: "Critical",
                conn_error: "Connection Error",
                open_dash: "Open Dashboard",
                settings: "Settings",
                new_tab: "New Tab"
            },
            tr: {
                subtitle: "Proxmox Yuk Dengeleyici",
                nodes: "Sunucu",
                vms: "VM",
                avg_cpu: "Ort. CPU",
                avg_ram: "Ort. RAM",
                connecting: "Baglaniyor...",
                balanced: "Kume Dengeli",
                imbalance: "Dengesizlik",
                critical: "Kritik",
                conn_error: "Baglanti Hatasi",
                open_dash: "Paneli Ac",
                settings: "Ayarlar",
                new_tab: "Yeni Sekme"
            }
        };
        var t = i18n[lang] || i18n.en;

        var svgBalance = '<svg viewBox="0 0 24 24"><path d="M12 3c-1.27 0-2.4.8-2.82 2H3v2h1.95L2 14c-.47 2 1 3 3.5 3s4.06-1 3.5-3L6.05 7h3.12c.33.85.98 1.5 1.83 1.83V20H7v2h10v-2h-4V8.82c.85-.32 1.5-.97 1.83-1.82h3.12L15 14c-.47 2 1 3 3.5 3s4.06-1 3.5-3l-2.95-7H21V5h-6.17C14.4 3.8 13.27 3 12 3z"/></svg>';
        var svgDash = '<svg viewBox="0 0 24 24"><path d="M3 13h8V3H3v10zm0 8h8v-6H3v6zm10 0h8V11h-8v10zm0-18v6h8V3h-8z"/></svg>';
        var svgSettings = '<svg viewBox="0 0 24 24"><path d="M19.14 12.94c.04-.31.06-.63.06-.94 0-.31-.02-.63-.06-.94l2.03-1.58c.18-.14.23-.41.12-.61l-1.92-3.32c-.12-.22-.37-.29-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54c-.04-.24-.24-.41-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.04.31-.06.63-.06.94s.02.63.06.94l-2.03 1.58c-.18.14-.23.41-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z"/></svg>';
        var svgOpen = '<svg viewBox="0 0 24 24"><path d="M19 19H5V5h7V3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2v-7h-2v7zM14 3v2h3.59l-9.83 9.83 1.41 1.41L19 6.41V10h2V3h-7z"/></svg>';

        var widget = document.createElement("div");
        widget.id = "cb-widget";
        widget.innerHTML = `
            <div id="cb-panel">
                <div class="cb-header">
                    ${svgBalance}
                    <div class="cb-header-text">
                        <h3>ClusterBalance</h3>
                        <span>${t.subtitle}</span>
                    </div>
                </div>
                <div class="cb-stats">
                    <div class="cb-stat">
                        <div class="cb-stat-value" id="cb-nodes">-</div>
                        <div class="cb-stat-label">${t.nodes}</div>
                    </div>
                    <div class="cb-stat">
                        <div class="cb-stat-value" id="cb-vms">-</div>
                        <div class="cb-stat-label">${t.vms}</div>
                    </div>
                    <div class="cb-stat">
                        <div class="cb-stat-value" id="cb-cpu">-</div>
                        <div class="cb-stat-label">${t.avg_cpu}</div>
                    </div>
                    <div class="cb-stat">
                        <div class="cb-stat-value" id="cb-mem">-</div>
                        <div class="cb-stat-label">${t.avg_ram}</div>
                    </div>
                </div>
                <div class="cb-status" id="cb-status">
                    <div class="cb-status-dot"></div>
                    <div class="cb-status-text">${t.connecting}</div>
                </div>
                <div class="cb-actions">
                    <button class="cb-btn cb-btn-primary" id="cb-open-dash">${svgDash} ${t.open_dash}</button>
                    <div class="cb-row">
                        <button class="cb-btn cb-btn-secondary" id="cb-open-settings">${svgSettings} ${t.settings}</button>
                        <button class="cb-btn cb-btn-secondary" id="cb-open-new">${svgOpen} ${t.new_tab}</button>
                    </div>
                </div>
                <div class="cb-footer">
                    <a href="https://github.com/muammer-yesilyagci/clusterbalance" target="_blank">ClusterBalance v2</a>
                </div>
            </div>
            <button id="cb-fab" title="ClusterBalance">${svgBalance}</button>
        `;

        document.body.appendChild(widget);

        // Event listeners
        document.getElementById("cb-fab").onclick = function() {
            document.getElementById("cb-panel").classList.toggle("open");
            loadStats();
        };

        document.getElementById("cb-open-dash").onclick = function() {
            window.open(DASHBOARD, "clusterbalance", "width=1400,height=900");
        };

        document.getElementById("cb-open-settings").onclick = function() {
            window.open(DASHBOARD + "/#settings", "clusterbalance", "width=1400,height=900");
        };

        document.getElementById("cb-open-new").onclick = function() {
            window.open(DASHBOARD, "_blank");
        };

        // Close panel when clicking outside
        document.addEventListener("click", function(e) {
            if (!e.target.closest("#cb-widget")) {
                document.getElementById("cb-panel").classList.remove("open");
            }
        });

        // Initial load
        loadStats();
        setInterval(loadStats, 30000);

        console.log("ClusterBalance: Widget v2.0 loaded - https://github.com/muammer-yesilyagci/clusterbalance");
    }

    function loadStats() {
        var lang = localStorage.getItem("cb_language") || "en";
        var msgs = {
            en: { connecting: "Connecting...", balanced: "Cluster Balanced", imbalance: "Imbalance", critical: "Critical", error: "Connection Error" },
            tr: { connecting: "Baglaniyor...", balanced: "Kume Dengeli", imbalance: "Dengesizlik", critical: "Kritik", error: "Baglanti Hatasi" }
        };
        var m = msgs[lang] || msgs.en;

        var status = document.getElementById("cb-status");
        status.className = "cb-status";
        status.querySelector(".cb-status-text").textContent = m.connecting;

        fetch(DASHBOARD + "/api/widget-summary")
            .then(function(r) { return r.json(); })
            .then(function(d) {
                document.getElementById("cb-nodes").textContent = d.node_count || "-";
                document.getElementById("cb-vms").textContent = d.total || d.running_vms || "-";
                document.getElementById("cb-cpu").textContent = (d.avg_cpu || 0).toFixed(1) + "%";
                document.getElementById("cb-mem").textContent = (d.avg_mem || 0).toFixed(1) + "%";

                if (d.balanced !== false && d.max_diff < 15) {
                    status.className = "cb-status";
                    status.querySelector(".cb-status-text").textContent = m.balanced;
                } else if (d.max_diff < 25) {
                    status.className = "cb-status warning";
                    status.querySelector(".cb-status-text").textContent = m.imbalance + ": " + (d.max_diff || 0).toFixed(1) + "%";
                } else {
                    status.className = "cb-status error";
                    status.querySelector(".cb-status-text").textContent = m.critical + ": " + (d.max_diff || 0).toFixed(1) + "%";
                }
            })
            .catch(function(e) {
                status.className = "cb-status error";
                status.querySelector(".cb-status-text").textContent = m.error;
                console.log("ClusterBalance Error:", e);
            });
    }

    // Initialize when DOM is ready
    if (document.readyState === "complete") {
        init();
    } else {
        window.addEventListener("load", init);
    }
})();
