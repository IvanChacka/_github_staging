#!/usr/bin/env python3
"""Minimal clean server.py with Plotly + all anomaly features."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config.single_instance import acquire

from flask import Flask, jsonify, render_template_string

from config.logger import get_logger
from config.settings import APP_HOST, APP_PORT, ROLLING_WINDOW_HOURS, MODULE_CONFIG
import json
from processor.database import get_conn

logger = get_logger("app_server")
app = Flask(__name__)


def _get_window_range() -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MIN(ts_et) AS min_ts, MAX(ts_et) AS max_ts FROM minute_metrics"
        ).fetchone()
        return {"min_ts": row["min_ts"], "max_ts": row["max_ts"]}


@app.route("/api/meta")
def api_meta():
    return jsonify({"window_hours": ROLLING_WINDOW_HOURS, **_get_window_range()})


@app.route("/api/modules")
def api_modules():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT group_key FROM minute_metrics ORDER BY group_key"
        ).fetchall()
    return jsonify([r["group_key"] for r in rows])


@app.route("/api/contracts/<module_key>")
def api_contracts(module_key):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT c.contract_slug, c.contract_name, m.probability, m.volume, "
            "m.probability_anomaly, m.volume_enabled, cc.direction, cc.relevant "
            "FROM contracts c "
            "JOIN minute_metrics m ON m.contract_slug = c.contract_slug "
            "  AND m.group_key = c.group_key "
            "  AND m.ts_et = (SELECT MAX(ts_et) FROM minute_metrics WHERE contract_slug = c.contract_slug) "
            "LEFT JOIN contract_classification cc ON cc.contract_slug = c.contract_slug "
            "  AND cc.group_key = c.group_key "
            "WHERE c.group_key = ? AND m.probability IS NOT NULL "
            "ORDER BY c.contract_name",
            (module_key,),
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/timeseries/<slug>")
def api_timeseries(slug):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT ts_et, probability, volume, probability_anomaly, volume_enabled "
            "FROM minute_metrics WHERE contract_slug = ? ORDER BY ts_et",
            (slug,),
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/anomalies/all")
def api_anomalies_all():
    with get_conn() as conn:
        rows = conn.execute(
            "WITH latest AS (SELECT MAX(ts_et) AS max_ts FROM minute_metrics) "
            "SELECT a.contract_slug, c.contract_name, a.group_key, a.ts_et, "
            "  a.metric, a.old_value, a.new_value, a.change_ratio, a.message, "
            "  (SELECT m.probability FROM minute_metrics m "
            "   WHERE m.contract_slug = a.contract_slug AND m.group_key = a.group_key "
            "   ORDER BY m.ts_et DESC LIMIT 1) AS current_probability "
            "FROM alerts a "
            "JOIN contracts c ON c.contract_slug = a.contract_slug AND c.group_key = a.group_key "
            "LEFT JOIN contract_classification cc ON cc.contract_slug = a.contract_slug AND cc.group_key = a.group_key "
            "WHERE a.ts_et = (SELECT max_ts FROM latest) "
            "  AND (cc.relevant IS NULL OR cc.relevant = 1) "
            "ORDER BY ABS(a.change_ratio) DESC, a.id DESC"
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/anomalies/<module_key>")
def api_anomalies_by_module(module_key):
    with get_conn() as conn:
        rows = conn.execute(
            "WITH latest AS (SELECT MAX(ts_et) AS max_ts FROM minute_metrics) "
            "SELECT a.contract_slug, c.contract_name, a.group_key, a.ts_et, "
            "  a.metric, a.old_value, a.new_value, a.change_ratio, a.message, "
            "  (SELECT m.probability FROM minute_metrics m "
            "   WHERE m.contract_slug = a.contract_slug AND m.group_key = a.group_key "
            "   ORDER BY m.ts_et DESC LIMIT 1) AS current_probability "
            "FROM alerts a "
            "JOIN contracts c ON c.contract_slug = a.contract_slug AND c.group_key = a.group_key "
            "LEFT JOIN contract_classification cc ON cc.contract_slug = a.contract_slug AND cc.group_key = a.group_key "
            "WHERE a.ts_et = (SELECT max_ts FROM latest) "
            "  AND a.group_key = ? "
            "  AND (cc.relevant IS NULL OR cc.relevant = 1) "
            "ORDER BY ABS(a.change_ratio) DESC, a.id DESC",
            (module_key,),
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/rejected")
def api_rejected():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT cc.contract_slug, cc.group_key, cc.reject_reason AS reason, COALESCE(c.contract_name, cc.contract_slug) AS contract_name "
            "FROM contract_classification cc "
            "LEFT JOIN contracts c ON c.contract_slug = cc.contract_slug AND c.group_key = cc.group_key "
            "WHERE cc.relevant = 0 ORDER BY cc.contract_slug"
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/status")
def api_status():
    return jsonify({"status": "ok"})


# ---- Dynamic embedding template parts ----
_PAGE_HEAD = '''<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Probability Monitor</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#111418;--surface:#1b1f25;--border:#2b3038;--text:#e8eaed;--muted:#8b92a0;--accent:#20c997;--danger:#ff4d4f}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);font-size:14px;min-height:100vh}
.header{background:var(--surface);border-bottom:1px solid var(--border);padding:10px 20px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.header h1{font-size:16px;font-weight:600;color:var(--accent)}
.header .meta{color:var(--muted);font-size:12px;margin-left:auto}
.modules{background:var(--surface);border-bottom:1px solid var(--border);padding:8px 20px;display:flex;gap:6px;flex-wrap:wrap}
.mod-btn{padding:5px 14px;border-radius:4px;border:1px solid var(--border);background:transparent;color:var(--muted);cursor:pointer;font-size:12px}
.mod-btn.active{background:var(--accent);color:#000;border-color:var(--accent);font-weight:600}
.mod-btn.rejected{color:#6b7280;border-color:#374151}
.mod-btn.rejected.active{background:#6b7280;color:#fff;border-color:#6b7280}
.sub-tabs{display:flex;gap:6px;padding:8px 20px;background:var(--surface);border-bottom:1px solid var(--border)}
.sub-btn{padding:3px 12px;border-radius:4px;border:1px solid var(--border);background:transparent;color:var(--muted);cursor:pointer;font-size:12px}
.sub-btn.active{background:#2b3038;color:var(--text)}
.content{flex:1;padding:12px 20px;overflow-y:auto}
.contract-item{padding:10px 14px;margin-bottom:6px;border-radius:6px;background:var(--surface);border:1px solid var(--border);display:flex;flex-wrap:wrap;align-items:center;gap:10px;cursor:pointer;transition:border-color .15s}
.contract-item:hover{border-color:#20c99755}
.contract-item .name{flex:1;min-width:160px;font-weight:500;color:var(--text);font-size:13px}
.contract-item .prob{font-size:14px;font-weight:700;min-width:60px;text-align:right;color:var(--accent)}
.contract-item .vol{font-size:12px;color:var(--muted);min-width:60px;text-align:right}
.contract-item .anomaly-badge{background:rgba(255,77,79,0.15);color:var(--danger);padding:2px 8px;border-radius:3px;font-size:11px;font-weight:600}
.cont-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:8px}
.cont-card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:10px 12px;cursor:pointer;transition:border-color .15s}
.cont-card:hover{border-color:#20c99755}
.cont-card .name{font-size:12px;font-weight:500;color:var(--text);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;margin-bottom:6px;line-height:1.4}
.cont-card .row{display:flex;justify-content:space-between;align-items:center;gap:6px}
.cont-card .prob{font-size:15px;font-weight:700;color:var(--accent)}
.cont-card .vol{font-size:11px;color:var(--muted)}
.cont-card .tag{font-size:10px;padding:1px 6px;border-radius:3px;font-weight:600}
.cont-card .tag.bull{background:rgba(32,201,151,0.15);color:#20c997}
.cont-card .tag.bear{background:rgba(255,107,107,0.15);color:#ff6b6b}
.cont-card .tag.neutral{background:rgba(139,146,160,0.2);color:#8b92a0}
.loading{color:var(--muted);padding:32px;text-align:center;font-size:13px}
.no-data{color:var(--muted);padding:32px;text-align:center;font-size:13px}
#chart-panel{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.65);z-index:1000;align-items:center;justify-content:center}
#chart-panel.show{display:flex}
#chart-overlay{position:absolute;top:0;left:0;right:0;bottom:0;cursor:pointer}
.chart-modal{position:relative;background:var(--surface);border-radius:10px;border:1px solid var(--border);width:92%;max-width:900px;max-height:88vh;display:flex;flex-direction:column;box-shadow:0 8px 32px rgba(0,0,0,0.5)}
.chart-header{display:flex;align-items:center;justify-content:space-between;padding:14px 20px;border-bottom:1px solid var(--border)}
.chart-header h2{font-size:15px;font-weight:600;max-width:70%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chart-close{background:none;border:none;color:var(--muted);font-size:22px;cursor:pointer;padding:0 6px}
.chart-close:hover{color:var(--text)}
#chart-div{flex:1;min-height:560px;position:relative}
.popup-overlay{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.55);z-index:2000;display:flex;align-items:center;justify-content:center}
.popup-overlay{cursor:pointer}.popup-content{pointer-events:auto;cursor:default;background:#1e2228;border-radius:10px;border:1px solid #2b3038;width:90%;max-width:640px;max-height:80vh;display:flex;flex-direction:column;box-shadow:0 8px 32px rgba(0,0,0,0.5)}
.popup-header{display:flex;align-items:center;justify-content:space-between;padding:16px 20px;border-bottom:1px solid #2b3038}
.popup-title{font-size:15px;font-weight:600;color:#e8eaed}
.popup-close{background:none;border:none;color:#8b92a0;font-size:22px;cursor:pointer;padding:0 4px}
.popup-close:hover{color:#e8eaed}
.popup-body{padding:12px 20px 20px;overflow-y:auto}
.anomaly-item{padding:12px 14px;margin-bottom:8px;border-radius:6px;border-left:3px solid #ff4d4f;background:rgba(255,77,79,0.06)}
.anomaly-item:hover{background:rgba(255,77,79,0.1)}
.anomaly-item .name{font-weight:600;color:#e8eaed;font-size:13px;margin-bottom:4px;cursor:pointer}
.anomaly-item .name:hover{color:#7ee2c2;text-decoration:underline}
.anomaly-item .detail{color:#8b92a0;font-size:12px;line-height:1.5}
#anomaly-banner-area{position:fixed;top:0;right:20px;z-index:1500;max-width:280px}
.anomaly-banner-item{background:linear-gradient(135deg,#2d1b1b 0%,#1e2228 100%);border-left:3px solid #ff4d4f;border-radius:4px;margin-bottom:4px;padding:8px 12px;cursor:pointer;font-size:12px;color:#e8eaed;animation:slideIn .2s ease}
.anomaly-banner-item:hover{background:linear-gradient(135deg,#3a2525 0%,#262b33 100%)}

@keyframes slideIn{from{transform:translateX(100%);opacity:0}to{transform:translateX(0);opacity:1}}
/* Sidebar */
.anomaly-toggle{position:fixed;right:0;top:50%;transform:translateY(-50%);background:#ff4d4f;color:#fff;border:none;border-radius:6px 0 0 6px;padding:8px 5px;cursor:pointer;font-size:11px;writing-mode:vertical-rl;z-index:100;transition:right .25s;font-weight:600;letter-spacing:1px}
.anomaly-toggle.shifted{right:280px}
.anomaly-sidebar{position:fixed;right:-280px;top:0;width:280px;height:100vh;background:var(--surface);border-left:1px solid var(--border);z-index:99;transition:right .25s;overflow-y:auto;padding:10px}
.anomaly-sidebar.open{right:0}
.anomaly-sidebar .title{font-size:13px;font-weight:700;color:var(--text);padding:8px 4px 10px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center}
.anomaly-sidebar .title button{background:none;border:none;color:var(--muted);cursor:pointer;font-size:18px;padding:0 4px}
.anomaly-sidebar .mod-section{margin-top:10px}
.anomaly-sidebar .mod-label{font-size:11px;font-weight:600;color:var(--accent);text-transform:uppercase;padding:4px 4px;margin-bottom:4px;border-bottom:1px solid rgba(255,255,255,0.05)}
.anomaly-sidebar .item{padding:6px 4px;cursor:pointer;border-radius:4px;font-size:11px;display:flex;justify-content:space-between;align-items:center;gap:6px}
.anomaly-sidebar .item:hover{background:rgba(255,255,255,0.04)}
.anomaly-sidebar .item .name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text)}
.anomaly-sidebar .item .change{font-weight:600;white-space:nowrap}
.anomaly-sidebar .item .change{color:#20c997}
.anomaly-sidebar .item .change.neg{color:#ff4d4f}
.anomaly-sidebar .empty{padding:20px 4px;color:var(--muted);font-size:11px;text-align:center}
</style>
</head><body>
<div class="header"><h1>Probability Monitor</h1><div class="meta" id="meta-date"></div></div>
<div class="modules" id="modules"><span class="loading">Loading modules...</span></div>
<div class="sub-tabs" id="sub-tabs" style="display:none;"></div>
<div class="content" id="content"><div class="loading">Loading...</div></div>
<div id="anomaly-toggle" class="anomaly-toggle" data-act="toggle-sidebar">ALERTS</div>
<div id="anomaly-sidebar" class="anomaly-sidebar"></div>
<div id="anomaly-popup" class="popup-overlay" style="display:none;" >
<div class="popup-content"><div class="popup-header"><span class="popup-title">Probability Anomalies</span>
<button class="popup-close" data-act="close-popup-btn">&times;</button></div>
<div id="anomaly-list" class="popup-body"></div></div></div>
<div id="chart-panel"><div id="chart-overlay" data-act="close-chart"></div>
<div class="chart-modal"><div class="chart-header"><h2 id="chart-title">Chart</h2>
<button class="chart-close" data-act="close-chart-btn">&times;</button></div>
<div id="chart-div"><div class="loading">Loading chart...</div></div></div></div>

<script>'''

_PAGE_TAIL = '''
</script>
<script>
var md = '', sb = 'bull', q = '', cc = [];
var _ls = new Set();
var _anoms = [];  // rolling anomalies for sidebar
var _pi = null;

// Event delegation
document.addEventListener('click', function(e){
  var t = e.target.closest('[data-act]'); if(!t) return;
  var a = t.getAttribute('data-act');
  if(a === 'mod') sm(t.getAttribute('data-val'));
  else if(a === 'sub') sst(t.getAttribute('data-val'));
  else if(a === 'chart') oc(t.getAttribute('data-slug'), t.getAttribute('data-name'));
  else if(a === 'close-popup'){ closeAnomalyPopup(); oc(t.getAttribute('data-slug'), t.getAttribute('data-name')); }
  else if(a === 'close-popup-btn' || a === 'close-popup-overlay') closeAnomalyPopup();
  else if(a === 'close-chart' || a === 'close-chart-btn') cc2();
  else if(a === 'toggle-sidebar') toggleSidebar();
  else if(a === 'close-sidebar'){ document.getElementById('anomaly-sidebar').classList.remove('open'); document.getElementById('anomaly-toggle').classList.remove('shifted'); }
  else if(a === 'sidebar-item'){ oc(t.getAttribute('data-slug'), t.getAttribute('data-name')); document.getElementById('anomaly-sidebar').classList.remove('open'); document.getElementById('anomaly-toggle').classList.remove('shifted'); }
});

function esc(s) { var d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

async function sj(url, fb, ms) {
  ms = ms || 5000;
  try {
    var ac = new AbortController();
    var t = setTimeout(function() { ac.abort(); }, ms);
    var r = await fetch(url, { signal: ac.signal });
    clearTimeout(t);
    return await r.json();
  } catch { return fb; }
}

async function lm() {
  var e = document.getElementById('modules');
  var mods = __EMBEDDED.modules;
  if (!mods || !mods.length) { e.innerHTML = '<span style="color:#ff6b6b;">No modules</span>'; return; }
  e.innerHTML = mods.map(function(m) {
    return '<button class="mod-btn" data-act="mod" data-val="' + m + '">' + esc(m) + '</button>';
  }).join('');
  e.innerHTML += '<button class="mod-btn rejected" data-act="mod" data-val="__rejected">&#x1f6ab; Rejected</button>';
  sm(mods[0]);
}

async function sm(mod) {
  md = mod; sb = 'bull'; q = '';
  document.querySelectorAll('.mod-btn').forEach(function(b) {
    b.classList.toggle('active', b.getAttribute('data-val') === mod || (mod === '__rejected' && b.classList.contains('rejected')));
  });
  if (mod === '__rejected') {
    document.getElementById('sub-tabs').style.display = 'none';
    document.getElementById('content').innerHTML = '<div class="loading">Loading rejected...</div>';
    var rj = __EMBEDDED.rejected || [];
    rr(rj); return;
  }
  var se = document.getElementById('sub-tabs');
  se.style.display = 'flex';
  // IPO 模块不需要 Bull/Bear/Neutral 分类
  if (mod === 'ipo') {
    se.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:3px 0">&#9889; No direction classification</div>';
  } else {
    se.innerHTML = '<button class="sub-btn active" data-act="sub" data-val="bull">Bull</button><button class="sub-btn" data-act="sub" data-val="bear">Bear</button><button class="sub-btn" data-act="sub" data-val="neutral">Neutral</button>';
  }
  sb = 'bull';
  var searchHtml = '<div style="padding:8px 0;display:flex;gap:8px"><input id="search-input" type="text" placeholder="Search contracts..." oninput="qs(this.value)" style="flex:1;background:var(--surface);border:1px solid var(--border);color:var(--text);padding:6px 10px;border-radius:4px;font-size:12px"></div>';
  document.getElementById('content').innerHTML = searchHtml + '<div class="loading">Loading contracts...</div>';
  cc = __EMBEDDED.contracts[mod] || [];
  var ad = __EMBEDDED.anomalies_by_mod[mod] || [];
  rv();
}

function sst(tab) {
  sb = tab;
  document.querySelectorAll('.sub-btn').forEach(function(b) {
    b.classList.toggle('active', b.getAttribute('data-val') === tab);
  });
  rv();
}

function rv() {
  if (!cc.length) { document.getElementById('content').innerHTML = '<div class="no-data">No contracts.</div>'; return; }
  var dm = { bull: '看涨', bear: '看跌', neutral: '中性' };
  var td = dm[sb] || '看涨';
  var fl = cc.filter(function(c) {
    if (md === 'ipo') return !q || c.contract_name.toLowerCase().includes(q.toLowerCase());
    return (c.direction || '看涨') === td && (!q || c.contract_name.toLowerCase().includes(q.toLowerCase()));
  });
  if (!fl.length) { document.getElementById('content').innerHTML = '<div class="no-data">No contracts in this direction.</div>'; return; }
  document.getElementById('content').innerHTML = '<div class="cont-grid">' + fl.map(function(c) {
    var p = c.probability != null ? (c.probability * 100).toFixed(1) + '%' : '?';
    var v = c.volume != null && c.volume_enabled === 1 ? Number(c.volume).toFixed(2) : '';
    var dir = (c.direction || '看涨');
    var tc = '看涨'; var cc = 'bull';
    if(dir === '看跌'){ tc = '看跌'; cc = 'bear'; }
    else if(dir === '中性'){ tc = '中性'; cc = 'neutral'; }
    var tag = '<span class="tag ' + cc + '">' + tc + '</span>';
    var ab = c.probability_anomaly === 1 ? '<span class="anomaly-badge">ANOM</span>' : '';
    return '<div class="cont-card" data-act="chart" data-slug="' + c.contract_slug + '" data-name="' + esc(c.contract_name) + '">' +
      '<div class="name">' + esc(c.contract_name) + '</div>' +
      '<div class="row"><span class="prob">' + p + '</span>' + tag + ab + '</div>' +
      '<div class="row" style="margin-top:4px"><span class="vol">Vol: ' + v + '</span></div></div>';
  }).join('') + '</div>';
}

function rr(items) {
  if (!items.length) { document.getElementById('content').innerHTML = '<div class="no-data">No rejected.</div>'; return; }
  document.getElementById('content').innerHTML = items.map(function(c) {
    return '<div class="contract-item"><span class="name">' + esc(c.contract_name || c.contract_slug) + '</span><span class="vol">' + esc(c.reason || '') + '</span></div>';
  }).join('');
}

function qs(v) { q = v; rv(); }

async function fam(mod) {
  var data = await sj('/api/anomalies/' + mod, []);
  window['_ab_' + mod] = data;
  return data;
}

async function cap() {
  var all = await sj('/api/anomalies/all', []);
  if (!all.length) return;
  var no = all.filter(function(a) { return !_ls.has(a.contract_slug); });
  // Add new anomalies to rolling window
  var now = Date.now();
  no.forEach(function(a) { a._ts = now; _anoms.push(a); });
  renderSidebar();
  if (!no.length) return;
  var items = no.slice(0, 5);
  var popup = document.getElementById('anomaly-popup');
  document.getElementById('anomaly-list').innerHTML = items.map(function(a) {
    var cp = a.current_probability != null ? (a.current_probability * 100).toFixed(1) + '%' : '?';
    return '<div class="anomaly-item" data-slug="' + a.contract_slug + '" data-name="' + esc(a.contract_name) + '">' +
      '<div class="name" data-act="close-popup" data-slug="' + a.contract_slug + '" data-name="' + esc(a.contract_name) + '">' + esc(a.contract_name) + '</div>' +
      '<div class="detail">Probability changed ' + (a.change_ratio > 0 ? '+' : '') + (a.change_ratio * 100).toFixed(1) + '% (now ' + cp + ') &middot; ' + esc(a.message) + '</div></div>';
  }).join('');
  popup.style.display = 'flex';
  no.forEach(function(a) { _ls.add(a.contract_slug); });
}

function closeAnomalyPopup() { document.getElementById('anomaly-popup').style.display = 'none'; }
function toggleSidebar() {
  var s = document.getElementById('anomaly-sidebar');
  var t = document.getElementById('anomaly-toggle');
  s.classList.toggle('open');
  t.classList.toggle('shifted');
}

function renderSidebar() {
  var side = document.getElementById('anomaly-sidebar');
  var toggle = document.getElementById('anomaly-toggle');
  if (!_anoms || !_anoms.length) {
    side.innerHTML = '<div class="title">Anomalies <button data-act="close-sidebar">&times;</button></div><div class="empty">No anomalies</div>';
    side.classList.remove('open');
    toggle.classList.remove('shifted');
    return;
  }
  // Prune entries older than 60 minutes
  var cutoff = Date.now() - 60 * 60 * 1000;
  _anoms = _anoms.filter(function(a) { return a._ts >= cutoff; });
  if (!_anoms.length) {
    side.innerHTML = '<div class="title">Anomalies <button data-act="close-sidebar">&times;</button></div><div class="empty">No anomalies</div>';
    toggle.classList.remove('shifted');
    return;
  }
  // Group by module
  var byMod = {};
  _anoms.forEach(function(a) {
    var m = a.group_key || 'unknown';
    if (!byMod[m]) byMod[m] = [];
    byMod[m].push(a);
  });
  var mods = __EMBEDDED.modules || [];
  var html = '<div class="title">Anomalies <button data-act="close-sidebar">&times;</button></div>';
  mods.forEach(function(m) {
    var items = byMod[m];
    if (!items || !items.length) return;
    html += '<div class="mod-section"><div class="mod-label">' + esc(m) + ' (' + items.length + ')</div>';
    items.slice(0, 10).forEach(function(a) {
      var cp = a.current_probability != null ? (a.current_probability * 100).toFixed(1) + '%' : '?';
      var cls = a.change_ratio > 0 ? 'pos' : 'neg';
      var sign = a.change_ratio > 0 ? '+' : '';
      html += '<div class="item" data-act="sidebar-item" data-slug="' + a.contract_slug + '" data-name="' + esc(a.contract_name) + '">' +
        '<span class="name">' + esc(a.contract_name.substring(0, 24)) + '</span>' +
        '<span class="change ' + cls + '">' + sign + (a.change_ratio * 100).toFixed(1) + '%</span></div>';
    });
    html += '</div>';
  });
  side.innerHTML = html;
}

function rabc(all) {
  // Initialize sidebar with embedded data (if any)
  if (all && all.length) {
    var now = Date.now();
    all.forEach(function(a) { if(!a._ts) a._ts = now; });
    _anoms = all.slice();  // copy
  }
  renderSidebar();
}

async function oc(slug, name) {
  document.getElementById('chart-title').textContent = name;
  var cd = document.getElementById('chart-div');
  cd.innerHTML = '<div class="loading">Loading chart...</div>';
  document.getElementById('chart-panel').classList.add('show');
  var data = await sj('/api/timeseries/' + slug, []);
  if (!data.length) { cd.innerHTML = '<div class="no-data">No data.</div>'; return; }
  var xs = data.map(function(d) { return d.ts_et; });
  var prob = data.map(function(d) { return d.probability; });
  var vol = data.map(function(d) { return d.volume; });
  var anom = data.map(function(d) { return d.probability_anomaly === 1; });
  var ve = data.some(function(d) { return d.volume_enabled === 1 && d.volume != null; });
  var vp = prob.filter(function(p) { return p != null; });
  var mn = vp.length ? vp.reduce(function(a,b) { return Math.min(a,b); }) : 0;
  var mx = vp.length ? vp.reduce(function(a,b) { return Math.max(a,b); }) : 1;
  var pr = mx - mn;
  var pad = Math.max(pr * 0.05, 0.01);
  var ymn = Math.max(0, mn - pad);
  var ymx = Math.min(1, mx + pad);
  var traces = [{
    x: xs, y: prob, mode: 'lines', name: 'Probability',
    line: { color: '#20c997', width: 1.6 },
    yaxis: 'y'
  }];
  // Add anomaly markers as scatter trace
  var ax = [], ay = [], ac = [];
  prob.forEach(function(p,i) { if(anom[i]){ ax.push(xs[i]); ay.push(p); ac.push('#ff4d4f'); } });
  if(ax.length) traces.push({
    x: ax, y: ay, mode: 'markers', name: 'Anomaly',
    marker: { size: 8, color: '#ff4d4f', line: { color: '#ffffff', width: 1 } }
  });
  if (ve) traces.push({ x: xs, y: vol, mode: 'lines', name: 'Volume', line: { color: 'rgba(79,195,247,0.6)', width: 1.4 }, yaxis: 'y2' });
  var layout = {
    paper_bgcolor: '#1b1f25', plot_bgcolor: '#1b1f25',
    font: { color: '#8b92a0', size: 11 },
    margin: { l: 50, r: 50, t: 20, b: 40 },
    xaxis: { gridcolor: 'rgba(43,48,56,0.45)', tickfont: { color: '#8b92a0', size: 10 } },
    yaxis: { range: [ymn, ymx], tickformat: '.0%', gridcolor: 'rgba(43,48,56,0.45)', tickfont: { color: '#8b92a0', size: 10 } },
    yaxis2: { overlaying: 'y', side: 'right', visible: ve, gridcolor: 'rgba(43,48,56,0.2)', tickfont: { color: '#8b92a0', size: 10 } },
    showlegend: false, hovermode: 'x unified', dragmode: false
  };
  Plotly.newPlot('chart-div', traces, layout, { responsive: true, displayModeBar: false });
}

function closeChart() { cc2(); }
function cc2() {
  document.getElementById('chart-panel').classList.remove('show');
  if (Plotly) Plotly.purge('chart-div');
}

async function lm2() {
  var m = __EMBEDDED.meta || {};
  var el = document.getElementById('meta-date');
  if (m.max_ts) el.textContent = 'Updated: ' + m.max_ts;
}

async function ia() {
  var all = __EMBEDDED.anomalies || [];
  rabc(all);
  all.forEach(function(a) { _ls.add(a.contract_slug); });
  if (_pi) clearInterval(_pi);
  _pi = setInterval(cap, 30000);
}

lm(); lm2(); ia();
</script></script></script>'''

def _build_embedded_json() -> str:
    """Build __EMBEDDED JSON from database."""
    from config.settings import MODULE_CONFIG
    import json
    from processor.database import get_conn

    result = {"modules": [], "meta": {}, "contracts": {}, "anomalies": [], "anomalies_by_mod": {}, "rejected": []}

    with get_conn() as conn:
        # --- modules ---
        modules = [r["group_key"] for r in conn.execute(
            "SELECT DISTINCT group_key FROM minute_metrics ORDER BY group_key"
        ).fetchall()]
        result["modules"] = modules

        # --- meta ---
        row = conn.execute("SELECT MAX(ts_et) AS max_ts, MIN(ts_et) AS min_ts FROM minute_metrics").fetchone()
        result["meta"] = {"max_ts": row["max_ts"], "min_ts": row["min_ts"]}

        # --- contracts per module ---
        latest_ts_row = conn.execute("SELECT MAX(ts_et) AS max_ts FROM minute_metrics").fetchone()
        latest_ts = latest_ts_row["max_ts"]

        for mod in modules:
            rows = conn.execute(
                "SELECT c.contract_slug, c.contract_name, m.probability, m.volume, "
                "m.probability_anomaly, m.volume_enabled, cc.direction, cc.relevant "
                "FROM contracts c "
                "JOIN minute_metrics m ON m.contract_slug = c.contract_slug "
                "  AND m.group_key = c.group_key "
                "  AND m.ts_et = (SELECT MAX(ts_et) FROM minute_metrics WHERE contract_slug = c.contract_slug) "
                "LEFT JOIN contract_classification cc ON cc.contract_slug = c.contract_slug "
                "  AND cc.group_key = c.group_key "
                "WHERE c.group_key = ? AND m.probability IS NOT NULL "
                "ORDER BY c.contract_name",
                (mod,),
            ).fetchall()
            result["contracts"][mod] = [dict(r) for r in rows]

        # --- anomalies (all) ---
        anom_all = conn.execute(
            "WITH latest AS (SELECT MAX(ts_et) AS max_ts FROM minute_metrics) "
            "SELECT a.contract_slug, c.contract_name, a.group_key, a.ts_et, "
            "  a.metric, a.old_value, a.new_value, a.change_ratio, a.message, "
            "  (SELECT m.probability FROM minute_metrics m "
            "   WHERE m.contract_slug = a.contract_slug AND m.group_key = a.group_key "
            "   ORDER BY m.ts_et DESC LIMIT 1) AS current_probability "
            "FROM alerts a "
            "JOIN contracts c ON c.contract_slug = a.contract_slug AND c.group_key = a.group_key "
            "LEFT JOIN contract_classification cc ON cc.contract_slug = a.contract_slug AND cc.group_key = a.group_key "
            "WHERE a.ts_et = (SELECT max_ts FROM latest) "
            "  AND (cc.relevant IS NULL OR cc.relevant = 1) "
            "ORDER BY ABS(a.change_ratio) DESC, a.id DESC"
        ).fetchall()
        result["anomalies"] = [dict(r) for r in anom_all]

        # --- anomalies by module ---
        anom_by_mod = {}
        for mod in modules:
            rows = conn.execute(
                "WITH latest AS (SELECT MAX(ts_et) AS max_ts FROM minute_metrics) "
                "SELECT a.contract_slug, c.contract_name, a.group_key, a.ts_et, "
                "  a.metric, a.old_value, a.new_value, a.change_ratio, a.message, "
                "  (SELECT m.probability FROM minute_metrics m "
                "   WHERE m.contract_slug = a.contract_slug AND m.group_key = a.group_key "
                "   ORDER BY m.ts_et DESC LIMIT 1) AS current_probability "
                "FROM alerts a "
                "JOIN contracts c ON c.contract_slug = a.contract_slug AND c.group_key = a.group_key "
                "LEFT JOIN contract_classification cc ON cc.contract_slug = a.contract_slug AND cc.group_key = a.group_key "
                "WHERE a.ts_et = (SELECT max_ts FROM latest) "
                "  AND a.group_key = ? "
                "  AND (cc.relevant IS NULL OR cc.relevant = 1) "
                "ORDER BY ABS(a.change_ratio) DESC, a.id DESC",
                (mod,),
            ).fetchall()
            if rows:
                anom_by_mod[mod] = [dict(r) for r in rows]
        result["anomalies_by_mod"] = anom_by_mod

        # --- rejected classifications ---
        rows = conn.execute(
            "SELECT cc.contract_slug, cc.group_key, cc.reject_reason AS reason, COALESCE(c.contract_name, cc.contract_slug) AS contract_name "
            "FROM contract_classification cc "
            "LEFT JOIN contracts c ON c.contract_slug = cc.contract_slug AND c.group_key = cc.group_key "
            "WHERE cc.relevant = 0 ORDER BY cc.contract_slug"
        ).fetchall()
        result["rejected"] = [dict(r) for r in rows]

    return json.dumps(result, ensure_ascii=False, default=str)


@app.route("/")
def index():
    embedded_json = _build_embedded_json()
    page_html = _PAGE_HEAD + 'var __EMBEDDED = ' + embedded_json + ';' + _PAGE_TAIL
    return render_template_string(page_html)





def run():
    app.run(host=APP_HOST, port=APP_PORT, threaded=True)


if __name__ == "__main__":
    if not acquire("server"):
        sys.exit(1)
    run()
