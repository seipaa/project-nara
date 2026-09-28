/* ==========================================================================
   NARA PROJECT — CLIENT SCRIPT v3
   State: welcome → dashboard → summary overlay / history
   ========================================================================== */

// ---------------------------------------------------------------------------
// GLOBAL STATE
// ---------------------------------------------------------------------------
let ws = null;
let wsReconnectTimer = null;

// Snapshot state terakhir dari server
let lastPomoData = null;
let lastPredData = null;

// Tracking lokal untuk summary yang lebih kaya
let distractionLog = {};       // { alasan_terjemahan: count }
let sessionStarted = false;    // Apakah sesi sudah pernah jalan
let sessionDurationSec = 25 * 60;

// Deteksi mode sebelumnya untuk auto-summary
let _prevMode    = null;
let _prevRunning = false;

// Timer ring circumference (r=52): 2*PI*52 ≈ 326.7
const RING_CIRC = 326.7;

// ---------------------------------------------------------------------------
// VIEW MANAGEMENT
// ---------------------------------------------------------------------------
function showView(name) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  const target = document.getElementById('view-' + name);
  if (target) target.classList.add('active');

  if (name === 'history')   fetchHistory();
  if (name === 'dashboard') fetchConfig();

  window._currentView = name;
}

function historyBack() {
  showView(sessionStarted ? 'dashboard' : 'welcome');
}

// ---------------------------------------------------------------------------
// WEBSOCKET
// ---------------------------------------------------------------------------
function initWebSocket() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${protocol}//${window.location.host}/ws`;

  setConnStatus('connecting');
  ws = new WebSocket(wsUrl);

  ws.onopen = () => {
    setConnStatus('live');
    if (wsReconnectTimer) { clearInterval(wsReconnectTimer); wsReconnectTimer = null; }
  };

  ws.onmessage = (event) => {
    try { handleTelemetry(JSON.parse(event.data)); }
    catch (e) { console.warn('[WS] Parse error:', e); }
  };

  ws.onclose = () => {
    setConnStatus('error');
    if (!wsReconnectTimer) wsReconnectTimer = setInterval(initWebSocket, 3000);
  };

  ws.onerror = () => ws.close();
}

function setConnStatus(state) {
  const labels = { connecting: 'Menghubungkan sistem...', live: 'Sistem terhubung', error: 'Koneksi terputus' };
  ['welcome-dot', 'dash-dot'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.className = 'conn-dot ' + state;
  });
  ['welcome-conn-text', 'dash-conn-text'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.textContent = labels[state] || '';
  });
}

// ---------------------------------------------------------------------------
// TELEMETRY — PUSAT PENGOLAHAN DATA
// ---------------------------------------------------------------------------
function handleTelemetry(data) {
  const pred = data.prediction;
  const pomo = data.pomodoro;

  // Simpan snapshot terkini
  if (pred) lastPredData = pred;
  if (pomo) lastPomoData = pomo;

  if (pred) updateCVPanel(pred);
  if (pomo && pred) {
    updateTimerPanel(pomo);
    updateFocusBar(pred, pomo);
    updateInterventionBanner(pomo);
    detectAutoSummary(pomo);
    if (pomo.mode === 'STUDY' && pomo.is_running) {
      collectDistractionData(pred);
    }
  }
}

// ---------------------------------------------------------------------------
// CV INDICATORS
// ---------------------------------------------------------------------------
function updateCVPanel(pred) {
  const t = pred.telemetry || {};
  setMetric('val-yaw',       `${(t.yaw||0).toFixed(1)}°`,   'bar-yaw',       Math.min(100,(Math.abs(t.yaw||0)/25)*100),       Math.abs(t.yaw||0)>18);
  setMetric('val-pitch',     `${(t.pitch||0).toFixed(1)}°`, 'bar-pitch',     Math.min(100,(Math.abs(t.pitch||0)/25)*100),     (t.pitch||0)<-15||(t.pitch||0)>12);
  setMetric('val-roll',      `${(t.roll||0).toFixed(1)}°`,  'bar-roll',      Math.min(100,(Math.abs(t.roll||0)/20)*100),      false);
  setMetric('val-ear',       (t.ear||0).toFixed(3),          'bar-ear',       Math.min(100,((t.ear||0)/0.35)*100),             (t.ear||0)<0.22);
  setMetric('val-mar',       (t.mar||0).toFixed(3),          'bar-mar',       Math.min(100,((t.mar||0)/0.50)*100),             (t.mar||0)>0.45);
  setMetric('val-blink',     (t.blink_score||0).toFixed(2),  'bar-blink',     Math.min(100,(t.blink_score||0)*100),            (t.blink_score||0)>0.55);
  setMetric('val-gaze-down', (t.gaze_down||0).toFixed(2),    'bar-gaze-down', Math.min(100,(t.gaze_down||0)*100),              (t.gaze_down||0)>0.40);
  setMetric('val-gaze-up',   (t.gaze_up||0).toFixed(2),      'bar-gaze-up',   Math.min(100,(t.gaze_up||0)*100),                (t.gaze_up||0)>0.35);
  setMetric('val-gaze-side', (t.gaze_side||0).toFixed(2),    'bar-gaze-side', Math.min(100,(t.gaze_side||0)*100),              (t.gaze_side||0)>0.45);

  setText('overlay-label', `${pred.label_text || 'Memproses...'} — ${t.reason || ''}`);
}

function setMetric(valId, valText, barId, pct, isDanger) {
  setText(valId, valText);
  const bar = document.getElementById(barId);
  if (!bar) return;
  bar.style.width = `${Math.max(0, pct)}%`;
  bar.style.background = isDanger ? 'var(--red)' : 'var(--green)';
}

// ---------------------------------------------------------------------------
// FOCUS BAR
// ---------------------------------------------------------------------------
function updateFocusBar(pred, pomo) {
  const t = pred.telemetry || {};
  const faceOk  = t.face_detected === true;
  const focused = faceOk && pred.label === 0;
  const standby = pred.label === -1 || !faceOk;
  const score   = pred.focus_score || 0;
  const pRisk   = (pred.p_risk || 0) * 100;

  const fill     = document.getElementById('focus-bar-fill');
  const camChip  = document.getElementById('camera-status-chip');
  const chipText = document.getElementById('camera-status-text');

  if (standby) {
    setText('focus-decision', '—');
    if (fill) { fill.style.width = '0%'; fill.className = 'focus-bar-fill'; }
    setText('focus-risk-text', 'Menunggu deteksi wajah...');
    if (camChip) camChip.className = 'status-chip status-idle';
    if (chipText) chipText.textContent = 'Siap';
    return;
  }

  if (fill) {
    fill.style.width = `${Math.max(3, score)}%`;
    fill.className = 'focus-bar-fill' + (focused ? '' : ' danger');
  }
  setText('focus-decision', focused ? 'Fokus' : 'Tidak fokus');
  setText('focus-risk-text', `Risiko gangguan: ${pRisk.toFixed(1)}% — ${t.reason || 'Normal'}`);
  if (camChip) camChip.className = 'status-chip ' + (focused ? 'status-focus' : 'status-unfocus');
  if (chipText) chipText.textContent = focused ? 'Fokus' : 'Tidak fokus';
}

// ---------------------------------------------------------------------------
// TIMER PANEL
// ---------------------------------------------------------------------------
function updateTimerPanel(pomo) {
  setText('timer-display', pomo.time_formatted || '00:00');
  setText('completed-sessions', pomo.completed_sessions || 0);

  // Mode chip & label
  const modeMap = {
    STUDY:      { label: 'Sesi belajar',      cls: 'mode-chip study',  timerLabel: 'belajar' },
    BREAK:      { label: 'Istirahat',          cls: 'mode-chip break',  timerLabel: 'istirahat' },
    LONG_BREAK: { label: 'Istirahat panjang',  cls: 'mode-chip break',  timerLabel: 'istirahat panjang' },
    PAUSED:     { label: 'Dijeda',             cls: 'mode-chip paused', timerLabel: 'dijeda' },
    IDLE:       { label: 'Siap',               cls: 'mode-chip',        timerLabel: 'siap' },
  };
  const m = modeMap[pomo.mode] || { label: pomo.mode, cls: 'mode-chip', timerLabel: pomo.mode.toLowerCase() };
  const modeChip = document.getElementById('mode-chip');
  if (modeChip) { document.getElementById('mode-label').textContent = m.label; modeChip.className = m.cls; }
  setText('timer-mode-label', m.timerLabel);

  // Timer ring progress
  updateTimerRing(pomo);

  // Kontrol tombol
  const btnStart  = document.getElementById('btn-start');
  const btnPause  = document.getElementById('btn-pause');
  const btnResume = document.getElementById('btn-resume');
  const btnSkip   = document.getElementById('btn-skip');
  const btnStop   = document.getElementById('btn-stop');

  if (pomo.mode === 'IDLE') {
    show(btnStart); hide(btnPause); hide(btnResume); hide(btnSkip); hide(btnStop);
    sessionStarted = false;
  } else if (pomo.is_running) {
    hide(btnStart); show(btnPause); hide(btnResume); show(btnSkip); show(btnStop);
    sessionStarted = true;
  } else {
    // Paused
    hide(btnStart); hide(btnPause); show(btnResume); show(btnSkip); show(btnStop);
  }

  // Mini stats
  setText('stat-focus-pct', pomo.focus_percentage != null ? `${pomo.focus_percentage.toFixed(0)}%` : '—');
  setText('stat-distract', pomo.distraction_count || 0);
  setText('stat-yawn', pomo.yawn_count || 0);

  _prevMode    = pomo.mode;
  _prevRunning = pomo.is_running;
}

function updateTimerRing(pomo) {
  const ring = document.getElementById('ring-fill');
  if (!ring) return;

  let totalSec = sessionDurationSec;
  if (pomo.mode === 'BREAK')      totalSec = (document.getElementById('cfg-break')?.value || 5) * 60;
  if (pomo.mode === 'LONG_BREAK') totalSec = (document.getElementById('cfg-longbreak')?.value || 15) * 60;

  const remaining = pomo.time_remaining || totalSec;
  const fraction  = Math.max(0, Math.min(1, remaining / totalSec));
  ring.style.strokeDashoffset = RING_CIRC * (1 - fraction);

  const urgent = pomo.mode === 'STUDY' && remaining < 60;
  ring.className = 'ring-fill' + (urgent ? ' danger' : pomo.mode.includes('BREAK') ? ' break' : '');
}

// ---------------------------------------------------------------------------
// INTERVENTION BANNER
// ---------------------------------------------------------------------------
function updateInterventionBanner(pomo) {
  const banner   = document.getElementById('intervention-banner');
  const msgEl    = document.getElementById('alert-message');
  const btnBreak = document.getElementById('btn-accept-break');
  if (!banner) return;

  if (pomo.active_alert) {
    const msg = pomo.active_alert.replace(/^\[.*?\]\s*/, '');
    banner.classList.remove('hidden');
    if (msgEl) msgEl.textContent = msg;
    if (btnBreak) pomo.suggested_break ? btnBreak.classList.remove('hidden') : btnBreak.classList.add('hidden');
  } else {
    banner.classList.add('hidden');
  }
}

// ---------------------------------------------------------------------------
// AUTO-SUMMARY: deteksi transisi STUDY→BREAK otomatis dari server
// ---------------------------------------------------------------------------
function detectAutoSummary(pomo) {
  const wasStudyRunning = _prevMode === 'STUDY' && _prevRunning;
  const nowBreak = pomo.mode === 'BREAK' || pomo.mode === 'LONG_BREAK';
  const modeChanged = pomo.mode !== _prevMode;

  if (wasStudyRunning && nowBreak && modeChanged) {
    buildAndShowSummary(pomo, 'Sesi belajar selesai');
  }
}

// ---------------------------------------------------------------------------
// STOP SESSION MANUAL — ambil snapshot stats saat ini, reset, tampilkan summary
// ---------------------------------------------------------------------------
async function stopSessionNow() {
  // Ambil snapshot data terakhir SEBELUM reset
  const snapPomo = lastPomoData ? { ...lastPomoData } : null;
  const snapLog  = { ...distractionLog };

  if (!snapPomo || (snapPomo.total_focus_sec === 0 && snapPomo.total_unfocused_sec === 0)) {
    // Sesi belum sempat mulai, cukup reset saja
    await postAPI('/api/pomodoro/reset');
    return;
  }

  // Reset sesi di server (ini juga auto-save ke DB)
  await postAPI('/api/pomodoro/skip');

  // Tampilkan summary dari snapshot
  buildAndShowSummary(snapPomo, 'Sesi dihentikan lebih awal', snapLog);
  setTimeout(fetchHistory, 600);
}

// ---------------------------------------------------------------------------
// BUILD & SHOW SUMMARY
// ---------------------------------------------------------------------------
function buildAndShowSummary(pomo, label, customLog) {
  const log = customLog || distractionLog;

  // Stats utama
  const focusPct   = pomo.focus_percentage ?? 0;
  const focusSec   = pomo.total_focus_sec ?? 0;
  const unfocusSec = pomo.total_unfocused_sec ?? 0;
  const totalSec   = focusSec + unfocusSec;

  setText('summary-type-label', label);
  setText('sum-focus-pct',   totalSec > 0 ? `${focusPct.toFixed(0)}%` : '—');
  setText('sum-focus-dur',   formatDuration(focusSec));
  setText('sum-unfocus-dur', formatDuration(unfocusSec));
  setText('sum-total-dur',   formatDuration(totalSec));
  setText('sum-distract',    pomo.distraction_count || 0);
  setText('sum-yawn',        pomo.yawn_count || 0);
  setText('sum-sleep',       pomo.sleep_count || 0);

  // Breakdown penyebab gangguan
  const breakdownEl = document.getElementById('breakdown-content');
  if (breakdownEl) {
    const entries = Object.entries(log).sort((a, b) => b[1] - a[1]).slice(0, 5);
    const total   = entries.reduce((s, [, v]) => s + v, 0);

    if (entries.length === 0) {
      breakdownEl.innerHTML = `<p style="font-size:12px;color:var(--txt-3);padding:6px 0">Tidak ada gangguan yang tercatat.</p>`;
    } else {
      breakdownEl.innerHTML = entries.map(([reason, count]) => {
        const pct = total > 0 ? Math.round((count / total) * 100) : 0;
        return `
          <div class="breakdown-item">
            <span>${reason}</span>
            <div style="display:flex;align-items:center;gap:10px">
              <div class="breakdown-bar" style="width:80px">
                <div class="breakdown-bar-fill" style="width:${pct}%"></div>
              </div>
              <span class="breakdown-val">${count}×</span>
            </div>
          </div>`;
      }).join('');
    }
  }

  // Insight singkat berdasarkan data
  const insightEl = document.getElementById('summary-insight');
  if (insightEl) {
    insightEl.innerHTML = buildInsight(focusPct, focusSec, unfocusSec, pomo, log);
  }

  // Reset log untuk sesi berikutnya
  distractionLog = {};

  // Tampilkan overlay
  const overlay = document.getElementById('summary-overlay');
  if (overlay) overlay.classList.remove('hidden');
}

function buildInsight(focusPct, focusSec, unfocusSec, pomo, log) {
  const parts = [];

  // Kondisi fokus keseluruhan
  if (focusSec + unfocusSec === 0) {
    return '<strong>Sesi tidak sempat merekam data.</strong>';
  }

  const focusMin = Math.round(focusSec / 60);
  const unfocusMin = Math.round(unfocusSec / 60);

  if (focusPct >= 80) {
    parts.push(`Konsentrasimu sangat baik — <strong>${focusMin} menit</strong> dari sesi dihabiskan dalam keadaan fokus.`);
  } else if (focusPct >= 50) {
    parts.push(`Kamu fokus selama <strong>${focusMin} menit</strong>, terganggu selama <strong>${unfocusMin} menit</strong>.`);
  } else {
    parts.push(`Fokusmu hanya <strong>${focusMin} menit</strong>. Perlu strategi lebih untuk menjaga perhatian.`);
  }

  // Penyebab utama gangguan
  const top = Object.entries(log).sort((a, b) => b[1] - a[1])[0];
  if (top && top[1] > 0) {
    parts.push(`Gangguan terbanyak: <strong>${top[0]}</strong>.`);
  }

  // Kantuk/menguap
  if ((pomo.yawn_count || 0) >= 3) {
    parts.push(`Kamu menguap ${pomo.yawn_count} kali — pertimbangkan tidur cukup sebelum belajar.`);
  }

  return parts.join(' ');
}

function closeSummary() {
  const overlay = document.getElementById('summary-overlay');
  if (overlay) overlay.classList.add('hidden');
}

function startFromSummary() {
  closeSummary();
  // Mode sudah berpindah di server (misal ke BREAK setelah STUDY selesai)
  // Jika dari stop manual (IDLE), mulai sesi baru
  if (!lastPomoData || lastPomoData.mode === 'IDLE') {
    postAPI('/api/pomodoro/start?mode=STUDY');
  }
}

// ---------------------------------------------------------------------------
// DISTRACTION LOG
// ---------------------------------------------------------------------------
function collectDistractionData(pred) {
  const t = pred.telemetry || {};
  if (t.face_detected && pred.label === 0) return; // sedang fokus, tidak perlu log
  if (t.reason && t.reason !== 'Normal') {
    const key = translateReason(t.reason);
    distractionLog[key] = (distractionLog[key] || 0) + 1;
  }
}

function translateReason(r) {
  const map = {
    'Looking Away (Yaw)':     'Menoleh ke samping',
    'Looking Down (Pitch)':   'Menunduk',
    'Gaze Down (Phone/Desk)': 'Lihat HP / meja',
    'Gaze Up (Daydreaming)':  'Melamun ke atas',
    'Gaze Side':              'Melirik samping',
    'Sleeping/Drowsy':        'Mengantuk / tertidur',
    'Yawning':                'Menguap',
    'No Face Detected':       'Keluar dari kamera',
  };
  return map[r] || r;
}

// ---------------------------------------------------------------------------
// REST API
// ---------------------------------------------------------------------------
async function postAPI(endpoint, body = null) {
  try {
    const opts = { method: 'POST', headers: { 'Content-Type': 'application/json' } };
    if (body !== null) opts.body = JSON.stringify(body);
    const res = await fetch(endpoint, opts);
    return await res.json();
  } catch (err) { console.warn('[API]', endpoint, err); }
}

async function getAPI(endpoint) {
  try {
    const res = await fetch(endpoint);
    return await res.json();
  } catch (err) { console.warn('[API]', endpoint, err); }
}

// --- POMODORO CONTROLS ---
function startPomodoro(mode = 'STUDY') {
  distractionLog = {};
  sessionStarted = true;
  postAPI(`/api/pomodoro/start?mode=${mode}`);
}

function pausePomodoro()  { postAPI('/api/pomodoro/pause'); }
function resumePomodoro() { postAPI('/api/pomodoro/resume'); }

function skipPomodoro() {
  // Lewati ke sesi berikutnya — auto-summary akan ditangani oleh detectAutoSummary
  postAPI('/api/pomodoro/skip').then(() => setTimeout(fetchHistory, 800));
}

function acceptSuggestedBreak() { postAPI('/api/pomodoro/accept_break'); }
function dismissAlert()          { postAPI('/api/pomodoro/dismiss_alert'); }

// ---------------------------------------------------------------------------
// CONFIG PANEL
// ---------------------------------------------------------------------------
function toggleConfig() {
  const panel = document.getElementById('config-panel');
  if (!panel) return;
  panel.classList.toggle('open');
}

function applyTemplate(study, brk, longBrk, tplId) {
  document.getElementById('cfg-study').value     = study;
  document.getElementById('cfg-break').value     = brk;
  document.getElementById('cfg-longbreak').value = longBrk;
  document.querySelectorAll('.tpl-btn').forEach(b => b.classList.remove('active'));
  const el = document.getElementById(tplId);
  if (el) el.classList.add('active');
}

async function saveConfig() {
  const study    = parseInt(document.getElementById('cfg-study').value)    || 25;
  const brk      = parseInt(document.getElementById('cfg-break').value)    || 5;
  const longBrk  = parseInt(document.getElementById('cfg-longbreak').value)|| 15;
  const interval = parseFloat(document.getElementById('cfg-interval').value)|| 1.0;

  await postAPI('/api/pomodoro/configure', {
    study_minutes: study, break_minutes: brk, long_break_minutes: longBrk
  });
  await postAPI('/api/config', { telemetry_interval_sec: interval });

  sessionDurationSec = study * 60;
  toggleConfig();
}

async function fetchConfig() {
  const cfg = await getAPI('/api/config');
  if (!cfg) return;
  const s = document.getElementById('cfg-study');
  const b = document.getElementById('cfg-break');
  const l = document.getElementById('cfg-longbreak');
  const i = document.getElementById('cfg-interval');
  if (s) s.value = cfg.study_minutes    || 25;
  if (b) b.value = cfg.break_minutes    || 5;
  if (l) l.value = cfg.long_break_minutes || 15;
  if (i) i.value = cfg.telemetry_interval_sec || 1;
  sessionDurationSec = (cfg.study_minutes || 25) * 60;
}

// ---------------------------------------------------------------------------
// HISTORY
// ---------------------------------------------------------------------------
async function fetchHistory() {
  const data = await getAPI('/api/history?limit=30');
  if (!data) return;
  const tbody = document.getElementById('history-table-body');
  if (!tbody) return;

  const history = data.history || [];
  if (history.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" class="table-empty">Belum ada sesi yang tersimpan.</td></tr>';
    return;
  }

  tbody.innerHTML = history.map(item => {
    const cls = item.focus_percentage >= 70 ? 'good' : 'bad';
    return `
      <tr>
        <td>${item.timestamp}</td>
        <td><strong>${translateSessionType(item.session_type)}</strong></td>
        <td>${formatDuration(item.duration_sec)}</td>
        <td><span class="focus-tag ${cls}">${item.focus_percentage}%</span></td>
        <td>${item.distraction_count}</td>
        <td>${item.yawn_count}</td>
        <td style="color:var(--txt-3);font-size:12px">${item.notes || '—'}</td>
      </tr>`;
  }).join('');
}

function translateSessionType(type) {
  if (!type) return '—';
  if (type.includes('ADAPTIVE')) return 'Belajar (jeda adaptif)';
  if (type.includes('STUDY'))    return 'Belajar';
  if (type.includes('LONG_BREAK')) return 'Istirahat panjang';
  if (type.includes('BREAK'))    return 'Istirahat';
  return type;
}

// ---------------------------------------------------------------------------
// UTIL
// ---------------------------------------------------------------------------
function formatDuration(totalSeconds) {
  const s = parseInt(totalSeconds) || 0;
  const m = Math.floor(s / 60);
  const sec = s % 60;
  if (s === 0) return '0d';
  if (m === 0) return `${sec}d`;
  if (sec === 0) return `${m}m`;
  return `${m}m ${sec}d`;
}

function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

function show(el) { if (el) el.classList.remove('hidden'); }
function hide(el) { if (el) el.classList.add('hidden'); }

// ---------------------------------------------------------------------------
// INIT
// ---------------------------------------------------------------------------
window.addEventListener('DOMContentLoaded', () => {
  showView('welcome');
  initWebSocket();
});
