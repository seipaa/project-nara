/* ==========================================================================
   SMART STUDENT FOCUS MONITORING & ADAPTIVE POMODORO - CLIENT SCRIPT
   ========================================================================== */

let ws = null;
let reconnectTimer = null;

// DOM Elements Cache
const connStatus = document.getElementById("connection-status");
const connText = document.getElementById("conn-text");
const statusDot = connStatus ? connStatus.querySelector(".status-dot") : null;

// Telemetry Elements
const valYaw = document.getElementById("val-yaw");
const barYaw = document.getElementById("bar-yaw");
const valPitch = document.getElementById("val-pitch");
const barPitch = document.getElementById("bar-pitch");
const valRoll = document.getElementById("val-roll");
const barRoll = document.getElementById("bar-roll");
const valEar = document.getElementById("val-ear");
const barEar = document.getElementById("bar-ear");
const valMar = document.getElementById("val-mar");
const barMar = document.getElementById("bar-mar");
const valBlink = document.getElementById("val-blink");
const barBlink = document.getElementById("bar-blink");
const valGazeDown = document.getElementById("val-gaze-down");
const barGazeDown = document.getElementById("bar-gaze-down");
const valGazeUp = document.getElementById("val-gaze-up");
const barGazeUp = document.getElementById("bar-gaze-up");
const valGazeSide = document.getElementById("val-gaze-side");
const barGazeSide = document.getElementById("bar-gaze-side");

// ML Elements
const mlBadge = document.getElementById("ml-badge");
const focusScoreVal = document.getElementById("focus-score-val");
const focusCircle = document.querySelector(".focus-circle");
const decisionText = document.getElementById("decision-text");
const riskProbText = document.getElementById("risk-prob-text");
const reasonText = document.getElementById("reason-text");
const riskProgressBar = document.getElementById("risk-progress-bar");
const overlayLabel = document.getElementById("overlay-label");

// Pomodoro Elements
const timerDisplay = document.getElementById("timer-display");
const pomodoroModeBadge = document.getElementById("pomodoro-mode-badge");
const completedSessionsCount = document.getElementById("completed-sessions-count");
const btnStart = document.getElementById("btn-start");
const btnPause = document.getElementById("btn-pause");
const btnResume = document.getElementById("btn-resume");

// Stats Elements
const statFocusTime = document.getElementById("stat-focus-time");
const statUnfocusTime = document.getElementById("stat-unfocus-time");
const statDistractionCount = document.getElementById("stat-distraction-count");
const statYawnCount = document.getElementById("stat-yawn-count");

// Intervention Elements
const interventionBanner = document.getElementById("intervention-banner");
const alertIcon = document.getElementById("alert-icon");
const alertTitle = document.getElementById("alert-title");
const alertMessage = document.getElementById("alert-message");
const btnAcceptBreak = document.getElementById("btn-accept-break");

// --------------------------------------------------------------------------
// WEBSOCKET TELEMETRY CLIENT
// --------------------------------------------------------------------------
function initWebSocket() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${protocol}//${window.location.host}/ws`;

  if (connText) connText.textContent = "Menghubungkan...";

  ws = new WebSocket(wsUrl);

  ws.onopen = () => {
    if (connText) connText.textContent = "Live Telemetry Connected";
    if (statusDot) {
      statusDot.classList.remove("pulsing");
      statusDot.classList.add("connected");
    }
    if (reconnectTimer) {
      clearInterval(reconnectTimer);
      reconnectTimer = null;
    }
  };

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      updateDashboard(data);
    } catch (e) {
      console.error("Error parsing telemetry:", e);
    }
  };

  ws.onclose = () => {
    if (connText) connText.textContent = "Terputus. Mencoba reconnect...";
    if (statusDot) {
      statusDot.classList.remove("connected");
      statusDot.classList.add("pulsing");
    }
    if (!reconnectTimer) {
      reconnectTimer = setInterval(initWebSocket, 2000);
    }
  };

  ws.onerror = (err) => {
    console.warn("WebSocket Error:", err);
    ws.close();
  };
}

// --------------------------------------------------------------------------
// DASHBOARD UI UPDATE
// --------------------------------------------------------------------------
function updateDashboard(data) {
  const pred = data.prediction;
  const pomo = data.pomodoro;

  if (pred) {
    const tele = pred.telemetry || {};

    // 1. Raw Telemetry dengan Normalisasi & Color Coding Cerdas
    const yawVal = tele.yaw || 0.0;
    const pitchVal = tele.pitch || 0.0;
    const rollVal = tele.roll || 0.0;
    const earVal = tele.ear || 0.0;
    const marVal = tele.mar || 0.0;
    const blinkVal = tele.blink_score || 0.0;
    const gazeDownVal = tele.gaze_down || 0.0;
    const gazeUpVal = tele.gaze_up || 0.0;
    const gazeSideVal = tele.gaze_side || 0.0;

    if (valYaw) valYaw.textContent = `${yawVal.toFixed(1)}°`;
    if (barYaw) {
      barYaw.style.width = `${Math.min(100, (Math.abs(yawVal) / 25.0) * 100)}%`;
      barYaw.style.background = Math.abs(yawVal) > 18.0 ? "var(--accent-red)" : "var(--accent-cyan)";
    }

    if (valPitch) valPitch.textContent = `${pitchVal.toFixed(1)}°`;
    if (barPitch) {
      barPitch.style.width = `${Math.min(100, (Math.abs(pitchVal) / 25.0) * 100)}%`;
      barPitch.style.background = (pitchVal < -15.0 || pitchVal > 15.0) ? "var(--accent-red)" : "var(--accent-cyan)";
    }

    if (valRoll) valRoll.textContent = `${rollVal.toFixed(1)}°`;
    if (barRoll) {
      barRoll.style.width = `${Math.min(100, (Math.abs(rollVal) / 20.0) * 100)}%`;
    }

    // EAR: Normalnya 0.25 - 0.35 (Mata Terbuka). Sayu/Tidur jika < 0.22
    if (valEar) valEar.textContent = `${earVal.toFixed(3)}`;
    if (barEar) {
      const earOpenPct = Math.min(100, Math.max(0, (earVal / 0.35) * 100));
      barEar.style.width = `${earOpenPct}%`;
      barEar.style.background = earVal < 0.22 ? "var(--accent-red)" : "var(--accent-green)";
    }

    // MAR: Normalnya < 0.15 (Mulut Tertutup). Menguap jika > 0.45
    if (valMar) valMar.textContent = `${marVal.toFixed(3)}`;
    if (barMar) {
      const marOpenPct = Math.min(100, Math.max(0, (marVal / 0.50) * 100));
      barMar.style.width = `${marOpenPct}%`;
      barMar.style.background = marVal > 0.45 ? "var(--accent-red)" : "var(--accent-cyan)";
    }

    if (valBlink) valBlink.textContent = `${blinkVal.toFixed(2)}`;
    if (barBlink) {
      barBlink.style.width = `${Math.min(100, blinkVal * 100)}%`;
      barBlink.style.background = blinkVal > 0.55 ? "var(--accent-red)" : "var(--accent-cyan)";
    }

    if (valGazeDown) valGazeDown.textContent = `${gazeDownVal.toFixed(2)}`;
    if (barGazeDown) {
      barGazeDown.style.width = `${Math.min(100, gazeDownVal * 100)}%`;
      barGazeDown.style.background = gazeDownVal > 0.40 ? "var(--accent-red)" : "var(--accent-cyan)";
    }

    if (valGazeUp) valGazeUp.textContent = `${gazeUpVal.toFixed(2)}`;
    if (barGazeUp) {
      barGazeUp.style.width = `${Math.min(100, gazeUpVal * 100)}%`;
      barGazeUp.style.background = gazeUpVal > 0.35 ? "var(--accent-yellow)" : "var(--accent-cyan)";
    }

    if (valGazeSide) valGazeSide.textContent = `${gazeSideVal.toFixed(2)}`;
    if (barGazeSide) {
      barGazeSide.style.width = `${Math.min(100, gazeSideVal * 100)}%`;
      barGazeSide.style.background = gazeSideVal > 0.45 ? "var(--accent-red)" : "var(--accent-cyan)";
    }

    // 2. ML Prediction Status & Face Presence Handling
    const faceDetected = tele.face_detected === true;
    const isStandby = pred.label === -1 || !faceDetected && pred.label === -1;
    const isAbsent = !faceDetected && pred.label === 1;
    const isFocused = faceDetected && pred.label === 0;
    const focusScore = pred.focus_score || 0;
    const pRisk = (pred.p_risk || 0) * 100;

    if (reasonText) reasonText.textContent = tele.reason || "Normal";

    if (isStandby) {
      if (focusScoreVal) focusScoreVal.textContent = "--";
      if (decisionText) decisionText.textContent = "Menunggu Wajah...";
      if (riskProbText) riskProbText.textContent = "--";
      if (riskProgressBar) riskProgressBar.style.width = "0%";
      if (mlBadge) {
        mlBadge.className = "badge badge-idle";
        mlBadge.textContent = "STANDBY";
      }
      if (focusCircle) focusCircle.className = "focus-circle idle";
    } else if (isAbsent) {
      if (focusScoreVal) focusScoreVal.textContent = "0%";
      if (decisionText) decisionText.textContent = "Tidak Ada Siswa (1)";
      if (riskProbText) riskProbText.textContent = "100.0%";
      if (riskProgressBar) riskProgressBar.style.width = "100%";
      if (mlBadge) {
        mlBadge.className = "badge badge-danger";
        mlBadge.textContent = "TIDAK TERDETEKSI";
      }
      if (focusCircle) focusCircle.className = "focus-circle danger";
    } else {
      if (focusScoreVal) focusScoreVal.textContent = `${focusScore.toFixed(0)}%`;
      if (decisionText) decisionText.textContent = isFocused ? "Risiko Rendah (0)" : "Risiko Tinggi (1)";
      if (riskProbText) riskProbText.textContent = `${pRisk.toFixed(1)}%`;
      if (riskProgressBar) riskProgressBar.style.width = `${Math.max(5, pRisk)}%`;

      if (mlBadge) {
        if (isFocused) {
          mlBadge.className = "badge badge-success";
          mlBadge.textContent = "FOKUS";
          if (focusCircle) focusCircle.className = "focus-circle";
        } else {
          mlBadge.className = "badge badge-danger";
          mlBadge.textContent = "TIDAK FOKUS";
          if (focusCircle) focusCircle.className = "focus-circle danger";
        }
      }
    }

    if (overlayLabel) {
      overlayLabel.textContent = `${pred.label_text || "Memproses..."} | ${tele.reason || ""}`;
    }
  }

  // 3. Pomodoro Status & Controls
  if (pomo) {
    if (timerDisplay) timerDisplay.textContent = pomo.time_formatted || "25:00";
    if (completedSessionsCount) completedSessionsCount.textContent = pomo.completed_sessions || 0;

    if (pomodoroModeBadge) {
      pomodoroModeBadge.textContent = pomo.mode;
      if (pomo.mode === "STUDY") {
        pomodoroModeBadge.className = "badge badge-success";
      } else if (pomo.mode.includes("BREAK")) {
        pomodoroModeBadge.className = "badge badge-live";
      } else {
        pomodoroModeBadge.className = "badge badge-idle";
      }
    }

    // Tombol Toggle
    if (pomo.is_running) {
      if (btnStart) btnStart.classList.add("hidden");
      if (btnPause) btnPause.classList.remove("hidden");
      if (btnResume) btnResume.classList.add("hidden");
    } else {
      if (pomo.mode === "IDLE") {
        if (btnStart) btnStart.classList.remove("hidden");
        if (btnPause) btnPause.classList.add("hidden");
        if (btnResume) btnResume.classList.add("hidden");
      } else {
        if (btnStart) btnStart.classList.add("hidden");
        if (btnPause) btnPause.classList.add("hidden");
        if (btnResume) btnResume.classList.remove("hidden");
      }
    }

    // Stats
    if (statFocusTime) statFocusTime.textContent = formatDuration(pomo.total_focus_sec || 0);
    if (statUnfocusTime) statUnfocusTime.textContent = formatDuration(pomo.total_unfocused_sec || 0);
    if (statDistractionCount) statDistractionCount.textContent = pomo.distraction_count || 0;
    if (statYawnCount) statYawnCount.textContent = pomo.yawn_count || 0;

    // 4. Adaptive Intervention Banner
    if (pomo.active_alert) {
      if (interventionBanner) interventionBanner.classList.remove("hidden");
      if (alertMessage) alertMessage.textContent = pomo.active_alert;
      if (btnAcceptBreak) {
        if (pomo.suggested_break) {
          btnAcceptBreak.classList.remove("hidden");
        } else {
          btnAcceptBreak.classList.add("hidden");
        }
      }
    } else {
      if (interventionBanner) interventionBanner.classList.add("hidden");
    }
  }
}

function formatDuration(totalSeconds) {
  const mins = Math.floor(totalSeconds / 60);
  const secs = totalSeconds % 60;
  if (mins === 0) return `${secs}s`;
  return `${mins}m ${secs}s`;
}

// --------------------------------------------------------------------------
// REST API CALLS
// --------------------------------------------------------------------------
async function postAPI(endpoint, body = {}) {
  try {
    const res = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    return await res.json();
  } catch (err) {
    console.error("API error:", err);
  }
}

function startPomodoro(mode = "STUDY") {
  postAPI(`/api/pomodoro/start?mode=${mode}`);
}

function pausePomodoro() {
  postAPI("/api/pomodoro/pause");
}

function resumePomodoro() {
  postAPI("/api/pomodoro/resume");
}

function resetPomodoro() {
  postAPI("/api/pomodoro/reset");
}

function skipPomodoro() {
  postAPI("/api/pomodoro/skip");
  setTimeout(fetchHistory, 500);
}

function acceptSuggestedBreak() {
  postAPI("/api/pomodoro/accept_break");
}

function dismissAlert() {
  postAPI("/api/pomodoro/dismiss_alert");
  if (interventionBanner) interventionBanner.classList.add("hidden");
}

async function fetchHistory() {
  try {
    const res = await fetch("/api/history");
    const data = await res.json();
    const history = data.history || [];
    const tbody = document.getElementById("history-table-body");

    if (!tbody) return;

    if (history.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" class="text-center">Belum ada riwayat sesi yang tersimpan.</td></tr>';
      return;
    }

    tbody.innerHTML = history.map(item => `
      <tr>
        <td>${item.timestamp}</td>
        <td><strong>${item.session_type}</strong></td>
        <td>${formatDuration(item.duration_sec)}</td>
        <td><span class="badge ${item.focus_percentage >= 80 ? 'badge-success' : 'badge-danger'}">${item.focus_percentage}%</span></td>
        <td>${item.distraction_count}</td>
        <td>${item.yawn_count}</td>
        <td><small>${item.notes || '-'}</small></td>
      </tr>
    `).join("");
  } catch (e) {
    console.error("Error fetching history:", e);
  }
}

// Inisialisasi saat window dimuat
window.addEventListener("DOMContentLoaded", () => {
  initWebSocket();
  fetchHistory();
});
