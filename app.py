#!/usr/bin/env python3
"""Simple QR scanning web app."""

import csv
import io
import os
import sqlite3
from datetime import datetime, timezone

from flask import Flask, Response, jsonify, render_template_string, request


def resolve_db_path() -> str:
    configured = os.environ.get("DB_PATH", "").strip()
    if configured:
        return configured
    if os.environ.get("RENDER", "").lower() == "true":
        return "/tmp/qr_scans.db"
    return "qr_scans.db"


DB_PATH = resolve_db_path()

app = Flask(__name__)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def db_connect():
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    with db_connect() as connection:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scanned_at_utc TEXT NOT NULL,
                qr_text TEXT NOT NULL,
                source TEXT NOT NULL
            )
            """
        )
        connection.commit()


def add_scan(qr_text: str, source: str):
    if not qr_text.strip():
        raise ValueError("qr_text is required")

    with db_connect() as connection:
        connection.execute(
            "INSERT INTO scans(scanned_at_utc, qr_text, source) VALUES(?, ?, ?)",
            (utc_now_iso(), qr_text.strip(), source.strip().upper() or "UNKNOWN"),
        )
        connection.commit()


def list_scans(limit: int = 300):
    with db_connect() as connection:
        rows = connection.execute(
            """
            SELECT id, scanned_at_utc, qr_text, source
            FROM scans
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_gate_summary(limit: int = 300):
    with db_connect() as connection:
        rows = connection.execute(
            """
            SELECT
                qr_text AS gate_code,
                COUNT(*) AS scan_count,
                MAX(scanned_at_utc) AS last_scanned_at_utc
            FROM scans
            GROUP BY qr_text
            ORDER BY last_scanned_at_utc DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


INDEX_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Gate Scanner</title>
  <style>
    :root {
      --ink: #f8fafc;
      --muted: #cbd5e1;
      --glass: rgba(15, 23, 42, 0.55);
      --glass-strong: rgba(2, 6, 23, 0.74);
      --line: rgba(255, 255, 255, 0.22);
      --accent: #22c55e;
      --danger: #f87171;
      --button: rgba(30, 41, 59, 0.76);
      --button-soft: rgba(15, 23, 42, 0.7);
    }
    * {
      box-sizing: border-box;
    }
    html, body {
      height: 100%;
    }
    body {
      margin: 0;
      font-family: "Avenir Next", "Segoe UI", sans-serif;
      background: #020617;
      color: var(--ink);
      overflow: hidden;
    }
    .scanner-shell {
      position: fixed;
      inset: 0;
      background: #000;
    }
    video {
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      object-fit: cover;
      background: #000;
    }
    #qr-canvas {
      display: none;
    }
    .top-overlay {
      position: absolute;
      left: 0;
      right: 0;
      top: 0;
      padding: max(env(safe-area-inset-top), 14px) 14px 16px;
      background: linear-gradient(180deg, rgba(2, 6, 23, 0.88), rgba(2, 6, 23, 0));
      pointer-events: none;
      z-index: 4;
    }
    .topbar {
      pointer-events: auto;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }
    .topbar-right {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .topbar h1 {
      margin: 0;
      font-size: 30px;
      letter-spacing: 0.01em;
      text-shadow: 0 4px 22px rgba(0, 0, 0, 0.45);
    }
    .topbar .badge {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 6px 10px;
      font-size: 12px;
      backdrop-filter: blur(8px);
      background: rgba(15, 23, 42, 0.4);
    }
    .topbar .top-link {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 6px 10px;
      font-size: 12px;
      color: #f8fafc;
      text-decoration: none;
      backdrop-filter: blur(8px);
      background: rgba(15, 23, 42, 0.4);
    }
    .scan-zone {
      position: absolute;
      left: 50%;
      top: 50%;
      width: min(74vw, 360px);
      aspect-ratio: 1;
      transform: translate(-50%, -50%);
      z-index: 3;
      pointer-events: none;
    }
    .scan-zone::before {
      content: "";
      position: absolute;
      inset: 0;
      border-radius: 28px;
      box-shadow: 0 0 0 999vmax rgba(2, 6, 23, 0.34);
    }
    .corner {
      position: absolute;
      width: 56px;
      height: 56px;
      border: 4px solid #fff;
      opacity: 0.88;
    }
    .detected-chip {
      position: absolute;
      left: 50%;
      top: 50%;
      transform: translate(-50%, -50%);
      min-width: 150px;
      max-width: 82%;
      border-radius: 999px;
      background: rgba(15, 23, 42, 0.9);
      color: #f8fafc;
      border: 1px solid rgba(255, 255, 255, 0.18);
      text-align: center;
      font-size: 28px;
      font-weight: 700;
      letter-spacing: 0.02em;
      padding: 10px 24px;
      overflow-wrap: anywhere;
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.35);
    }
    .corner.tl {
      left: 0;
      top: 0;
      border-right: 0;
      border-bottom: 0;
      border-top-left-radius: 24px;
    }
    .corner.tr {
      right: 0;
      top: 0;
      border-left: 0;
      border-bottom: 0;
      border-top-right-radius: 24px;
    }
    .corner.bl {
      left: 0;
      bottom: 0;
      border-right: 0;
      border-top: 0;
      border-bottom-left-radius: 24px;
    }
    .corner.br {
      right: 0;
      bottom: 0;
      border-left: 0;
      border-top: 0;
      border-bottom-right-radius: 24px;
    }
    .bottom-overlay {
      position: absolute;
      left: 0;
      right: 0;
      bottom: 0;
      z-index: 5;
      padding: 18px 14px max(env(safe-area-inset-bottom), 16px);
      background: linear-gradient(0deg, rgba(2, 6, 23, 0.96), rgba(2, 6, 23, 0.64) 45%, rgba(2, 6, 23, 0.02));
    }
    .result {
      min-height: 22px;
      margin-bottom: 10px;
      color: #86efac;
      font-size: 14px;
      font-weight: 600;
      text-shadow: 0 2px 18px rgba(0, 0, 0, 0.45);
    }
    .result.error {
      color: var(--danger);
    }
    .control-row {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
    }
    .capture-row {
      margin-top: 8px;
      display: grid;
      grid-template-columns: 2fr 1fr;
      gap: 8px;
    }
    button {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 11px 12px;
      font-weight: 700;
      font-size: 14px;
      color: #fff;
      background: var(--button);
      backdrop-filter: blur(10px);
      cursor: pointer;
    }
    button.primary {
      border-color: rgba(74, 222, 128, 0.7);
      background: linear-gradient(180deg, rgba(34, 197, 94, 0.85), rgba(22, 163, 74, 0.86));
    }
    button.capture {
      border-color: rgba(134, 239, 172, 0.9);
      background: linear-gradient(180deg, rgba(34, 197, 94, 0.95), rgba(22, 163, 74, 0.96));
    }
    button.ghost {
      background: var(--button-soft);
    }
    .hidden {
      display: none;
    }
    .manual {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
      margin-top: 10px;
      max-height: 80px;
      opacity: 1;
      overflow: hidden;
      transition: max-height 0.2s ease, opacity 0.2s ease;
    }
    .manual.collapsed {
      max-height: 0;
      opacity: 0;
      pointer-events: none;
      margin-top: 0;
    }
    input {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: rgba(15, 23, 42, 0.68);
      color: #fff;
      padding: 11px 14px;
      font-size: 15px;
      outline: none;
    }
    input::placeholder {
      color: var(--muted);
    }
    .history {
      margin-top: 10px;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: var(--glass);
      backdrop-filter: blur(10px);
      padding: 10px;
    }
    .history-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 8px;
      font-size: 12px;
      color: var(--muted);
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }
    .history-head a {
      color: #bfdbfe;
      text-decoration: none;
      font-weight: 700;
      letter-spacing: 0;
      text-transform: none;
    }
    #rows {
      list-style: none;
      margin: 0;
      padding: 0;
      max-height: 20vh;
      overflow: auto;
    }
    #rows li {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
      padding: 8px 0;
      border-top: 1px solid rgba(255, 255, 255, 0.12);
      align-items: center;
    }
    #rows li:first-child {
      border-top: 0;
      padding-top: 0;
    }
    .row-code {
      font-size: 14px;
      font-weight: 700;
      color: #f8fafc;
      overflow-wrap: anywhere;
    }
    .row-meta {
      font-size: 11px;
      color: #cbd5e1;
      text-align: right;
      white-space: nowrap;
    }
    body.scanning .badge-dot {
      animation: pulse 1.1s ease-in-out infinite;
      background: #22c55e;
    }
    .badge-dot {
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: #94a3b8;
      display: inline-block;
      margin-right: 6px;
      vertical-align: middle;
    }
    @keyframes pulse {
      0%, 100% { box-shadow: 0 0 0 0 rgba(34, 197, 94, 0.8); }
      50% { box-shadow: 0 0 0 9px rgba(34, 197, 94, 0); }
    }
    @media (min-width: 900px) {
      .scan-zone {
        width: min(38vw, 420px);
      }
      .bottom-overlay {
        max-width: 540px;
        left: 50%;
        transform: translateX(-50%);
        border-radius: 18px 18px 0 0;
        border: 1px solid rgba(255, 255, 255, 0.14);
        border-bottom: 0;
      }
    }
  </style>
</head>
<body>
  <div class="scanner-shell">
    <video id="qr-video" autoplay playsinline muted></video>
    <canvas id="qr-canvas"></canvas>

    <div class="top-overlay">
      <div class="topbar">
        <h1>Gate Scanner</h1>
        <div class="topbar-right">
          <div class="badge"><span class="badge-dot"></span>Live</div>
          <a class="top-link" href="/office">Office</a>
        </div>
      </div>
    </div>

    <div class="scan-zone">
      <span class="corner tl"></span>
      <span class="corner tr"></span>
      <span class="corner bl"></span>
      <span class="corner br"></span>
      <div id="detected-chip" class="detected-chip hidden"></div>
    </div>

    <div class="bottom-overlay">
      <div id="scan-result" class="result">Ready to scan gate code</div>
      <div class="control-row">
        <button id="start-scan" class="primary" type="button">Start</button>
        <button id="stop-scan" class="ghost" type="button">Stop</button>
        <button id="toggle-manual" class="ghost" type="button">Manual</button>
      </div>
      <div class="capture-row">
        <button id="capture-scan" class="capture hidden" type="button">Capture</button>
        <button id="clear-detected" class="ghost hidden" type="button">Reset</button>
      </div>

      <form id="manual-form" class="manual collapsed">
        <input id="manual-text" placeholder="Manual gate code">
        <button type="submit">Submit</button>
      </form>

      <div class="history">
        <div class="history-head">
          <span>Recent scans</span>
          <a href="/api/export.csv">Export CSV</a>
        </div>
        <ul id="rows"></ul>
      </div>
    </div>
  </div>

  <script src="https://cdn.jsdelivr.net/npm/jsqr@1.4.0/dist/jsQR.min.js"></script>
  <script>
    const video = document.getElementById('qr-video');
    const canvas = document.getElementById('qr-canvas');
    const canvasCtx = canvas.getContext('2d', { willReadFrequently: true });
    const resultBox = document.getElementById('scan-result');
    const rowsList = document.getElementById('rows');
    const manualForm = document.getElementById('manual-form');
    const manualInput = document.getElementById('manual-text');
    const manualToggle = document.getElementById('toggle-manual');
    const captureButton = document.getElementById('capture-scan');
    const clearButton = document.getElementById('clear-detected');
    const detectedChip = document.getElementById('detected-chip');
    let stream = null;
    let detector = null;
    let detectorMode = null;
    let scanning = false;
    let pendingDetectedText = '';
    let lastSentText = '';
    let lastSentAt = 0;

    function setResult(text, isError = false) {
      resultBox.textContent = text;
      resultBox.className = isError ? 'result error' : 'result';
    }

    function setScanningState(isOn) {
      document.body.classList.toggle('scanning', isOn);
    }

    function escapeHtml(text) {
      return String(text)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }

    function setPendingDetection(text) {
      pendingDetectedText = (text || '').trim();
      const hasPending = Boolean(pendingDetectedText);
      captureButton.classList.toggle('hidden', !hasPending);
      clearButton.classList.toggle('hidden', !hasPending);
      detectedChip.classList.toggle('hidden', !hasPending);

      if (hasPending) {
        detectedChip.textContent = pendingDetectedText;
        setResult(`Detected: ${pendingDetectedText}. Tap Capture to submit.`);
      } else {
        detectedChip.textContent = '';
      }
    }

    async function refreshRows() {
      const res = await fetch('/api/scans?limit=400');
      if (!res.ok) return;
      const rows = await res.json();
      if (!Array.isArray(rows)) return;

      rowsList.innerHTML = '';
      rows.slice(0, 12).forEach((row) => {
        const li = document.createElement('li');
        const code = escapeHtml(row.qr_text || '');
        const meta = `${escapeHtml(row.source || 'UNKNOWN')} | ${escapeHtml(row.scanned_at_utc || '')}`;
        li.innerHTML = `<span class="row-code">${code}</span><span class="row-meta">${meta}</span>`;
        rowsList.appendChild(li);
      });
    }

    async function submitScan(qrText, source) {
      const payload = (qrText || '').trim();
      if (!payload) {
        return false;
      }

      const now = Date.now();
      if (payload === lastSentText && now - lastSentAt < 1600) {
        return false;
      }

      let res;
      try {
        res = await fetch('/api/scan', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ qr_text: payload, source }),
        });
      } catch (err) {
        setResult(`Submit failed: ${err.message || err}`, true);
        return false;
      }

      let data = null;
      let textBody = '';
      const contentType = res.headers.get('content-type') || '';
      if (contentType.includes('application/json')) {
        data = await res.json().catch(() => null);
      } else {
        textBody = await res.text().catch(() => '');
      }

      if (!res.ok) {
        const backendError = data && data.error ? data.error : '';
        const genericError = textBody || `Scan rejected (${res.status})`;
        setResult(backendError || genericError, true);
        return false;
      }

      lastSentText = payload;
      lastSentAt = now;
      setResult('Scan saved');
      await refreshRows();
      return true;
    }

    async function detectQrFromFrame() {
      if (detectorMode === 'barcode' && detector) {
        const barcodes = await detector.detect(video);
        if (barcodes && barcodes.length > 0 && barcodes[0].rawValue) {
          return barcodes[0].rawValue;
        }
        return null;
      }

      if (detectorMode === 'jsqr' && window.jsQR) {
        const width = video.videoWidth;
        const height = video.videoHeight;
        if (!width || !height) {
          return null;
        }
        canvas.width = width;
        canvas.height = height;
        canvasCtx.drawImage(video, 0, 0, width, height);
        const imageData = canvasCtx.getImageData(0, 0, width, height);
        const code = window.jsQR(imageData.data, width, height, { inversionAttempts: 'dontInvert' });
        return code && code.data ? code.data : null;
      }

      return null;
    }

    async function scanLoop() {
      if (!scanning) {
        return;
      }

      try {
        if (pendingDetectedText) {
          if (scanning) {
            requestAnimationFrame(scanLoop);
          }
          return;
        }

        const qrText = await detectQrFromFrame();
        if (qrText) {
          setPendingDetection(qrText);
        }
      } catch (_) {
        // keep loop alive
      }

      if (scanning) {
        requestAnimationFrame(scanLoop);
      }
    }

    async function startCameraScan() {
      if (scanning) {
        return;
      }

      detector = null;
      detectorMode = null;

      if ('BarcodeDetector' in window) {
        try {
          detector = new BarcodeDetector({ formats: ['qr_code'] });
          detectorMode = 'barcode';
        } catch (_) {
          detector = null;
        }
      }

      if (!detectorMode && window.jsQR) {
        detectorMode = 'jsqr';
      }

      if (!detectorMode) {
        setResult('Browser camera QR scan not supported. Use manual input.', true);
        return;
      }

      try {
        stream = await navigator.mediaDevices.getUserMedia({
          video: { facingMode: { ideal: 'environment' } },
          audio: false,
        });
      } catch (err) {
        setResult(`Camera access failed: ${err.message || err}`, true);
        return;
      }

      video.srcObject = stream;
      try {
        await video.play();
      } catch (_) {
        // ignored
      }

      scanning = true;
      setScanningState(true);
      setPendingDetection('');
      setResult(detectorMode === 'barcode' ? 'Camera scan started' : 'Camera scan started (fallback mode)');
      scanLoop();
    }

    function stopCameraScan() {
      scanning = false;
      setScanningState(false);
      setPendingDetection('');
      if (stream) {
        stream.getTracks().forEach((track) => track.stop());
        stream = null;
      }
      video.srcObject = null;
      setResult('Camera scan stopped');
    }

    document.getElementById('start-scan').addEventListener('click', startCameraScan);
    document.getElementById('stop-scan').addEventListener('click', stopCameraScan);
    manualToggle.addEventListener('click', () => {
      manualForm.classList.toggle('collapsed');
      if (!manualForm.classList.contains('collapsed')) {
        manualInput.focus();
      }
    });
    captureButton.addEventListener('click', async () => {
      if (!pendingDetectedText) {
        setResult('No detected gate code yet.', true);
        return;
      }
      const codeToSubmit = pendingDetectedText;
      const ok = await submitScan(codeToSubmit, 'CAMERA');
      if (ok) {
        setPendingDetection('');
      }
    });
    clearButton.addEventListener('click', () => {
      setPendingDetection('');
      setResult('Ready to detect next gate code');
    });

    manualForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      const text = manualInput.value;
      await submitScan(text, 'MANUAL');
      manualInput.value = '';
    });

    setScanningState(false);
    refreshRows();
    setInterval(refreshRows, 2500);
  </script>
</body>
</html>
"""


OFFICE_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Gate Office Dashboard</title>
  <style>
    :root {
      --bg: #f1f5f9;
      --card: #ffffff;
      --ink: #0f172a;
      --muted: #64748b;
      --border: #dbe4ef;
      --accent: #0f766e;
      --warn: #b91c1c;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Avenir Next", "Segoe UI", sans-serif;
      color: var(--ink);
      background: radial-gradient(circle at top right, #dcfce7 0, var(--bg) 42%);
    }
    .wrap {
      max-width: 1160px;
      margin: 24px auto 28px;
      padding: 0 14px;
    }
    .top {
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      gap: 10px;
      align-items: center;
      margin-bottom: 14px;
    }
    h1 {
      margin: 0;
      font-size: 28px;
    }
    .muted { color: var(--muted); font-size: 14px; }
    .links {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    .btn {
      border: 1px solid #cbd5e1;
      border-radius: 999px;
      background: #fff;
      color: var(--ink);
      text-decoration: none;
      padding: 9px 13px;
      font-size: 14px;
      font-weight: 700;
    }
    .btn.primary {
      background: var(--accent);
      color: #fff;
      border-color: #0f766e;
    }
    .stats {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 12px;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 14px;
      box-shadow: 0 4px 16px rgba(15, 23, 42, 0.05);
    }
    .kpi-title {
      font-size: 12px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }
    .kpi-value {
      margin-top: 7px;
      font-size: 30px;
      font-weight: 800;
    }
    .grid {
      display: grid;
      grid-template-columns: 1.1fr 1fr;
      gap: 10px;
    }
    .panel-title {
      margin: 0 0 10px;
      font-size: 17px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }
    th, td {
      border-bottom: 1px solid var(--border);
      text-align: left;
      padding: 9px 8px;
      vertical-align: top;
    }
    th {
      color: var(--muted);
      font-size: 12px;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }
    td.mono {
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-weight: 700;
    }
    .status {
      min-height: 18px;
      font-size: 13px;
      color: var(--muted);
      margin-bottom: 10px;
    }
    .status.err {
      color: var(--warn);
      font-weight: 700;
    }
    @media (max-width: 940px) {
      .stats { grid-template-columns: 1fr; }
      .grid { grid-template-columns: 1fr; }
      h1 { font-size: 24px; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <div>
        <h1>Gate Office Dashboard</h1>
        <div class="muted">Live monitor of scanned gate codes.</div>
      </div>
      <div class="links">
        <a class="btn" href="/">Open Scanner</a>
        <a class="btn primary" href="/api/export.csv">Export CSV</a>
      </div>
    </div>

    <div class="stats">
      <div class="card">
        <div class="kpi-title">Total Scans</div>
        <div class="kpi-value" id="kpi-total">0</div>
      </div>
      <div class="card">
        <div class="kpi-title">Unique Gates</div>
        <div class="kpi-value" id="kpi-gates">0</div>
      </div>
      <div class="card">
        <div class="kpi-title">Last Scan (UTC)</div>
        <div class="kpi-value" id="kpi-last" style="font-size:18px;">-</div>
      </div>
    </div>

    <div class="status" id="status">Refreshing...</div>

    <div class="grid">
      <div class="card">
        <h2 class="panel-title">Gate Summary</h2>
        <table>
          <thead>
            <tr>
              <th>Gate</th>
              <th>Total Scans</th>
              <th>Last Scanned (UTC)</th>
            </tr>
          </thead>
          <tbody id="gate-rows"></tbody>
        </table>
      </div>

      <div class="card">
        <h2 class="panel-title">Recent Activity</h2>
        <table>
          <thead>
            <tr>
              <th>Time (UTC)</th>
              <th>Gate</th>
              <th>Source</th>
            </tr>
          </thead>
          <tbody id="scan-rows"></tbody>
        </table>
      </div>
    </div>
  </div>

  <script>
    const gateRows = document.getElementById('gate-rows');
    const scanRows = document.getElementById('scan-rows');
    const kpiTotal = document.getElementById('kpi-total');
    const kpiGates = document.getElementById('kpi-gates');
    const kpiLast = document.getElementById('kpi-last');
    const statusBox = document.getElementById('status');

    function esc(text) {
      return String(text || '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }

    function setStatus(text, isError = false) {
      statusBox.textContent = text;
      statusBox.className = isError ? 'status err' : 'status';
    }

    async function refreshDashboard() {
      try {
        const [summaryRes, scansRes] = await Promise.all([
          fetch('/api/gate-summary?limit=1000'),
          fetch('/api/scans?limit=200'),
        ]);

        if (!summaryRes.ok || !scansRes.ok) {
          setStatus(`Failed to refresh (${summaryRes.status}/${scansRes.status})`, true);
          return;
        }

        const summary = await summaryRes.json();
        const scans = await scansRes.json();

        if (!Array.isArray(summary) || !Array.isArray(scans)) {
          setStatus('Unexpected API response', true);
          return;
        }

        let totalScans = 0;
        summary.forEach((row) => {
          totalScans += Number(row.scan_count || 0);
        });
        kpiTotal.textContent = String(totalScans);
        kpiGates.textContent = String(summary.length);
        kpiLast.textContent = scans.length > 0 ? scans[0].scanned_at_utc : '-';

        gateRows.innerHTML = '';
        summary.forEach((row) => {
          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td class="mono">${esc(row.gate_code)}</td>
            <td>${esc(row.scan_count)}</td>
            <td>${esc(row.last_scanned_at_utc)}</td>
          `;
          gateRows.appendChild(tr);
        });

        scanRows.innerHTML = '';
        scans.slice(0, 40).forEach((row) => {
          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td>${esc(row.scanned_at_utc)}</td>
            <td class="mono">${esc(row.qr_text)}</td>
            <td>${esc(row.source)}</td>
          `;
          scanRows.appendChild(tr);
        });

        setStatus(`Updated at ${new Date().toISOString()}`);
      } catch (err) {
        setStatus(`Refresh error: ${err.message || err}`, true);
      }
    }

    refreshDashboard();
    setInterval(refreshDashboard, 3000);
  </script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(INDEX_TEMPLATE)


@app.route("/office")
def office():
    return render_template_string(OFFICE_TEMPLATE)


@app.route("/api/scan", methods=["POST"])
def api_scan():
    payload = request.get_json(silent=True) or {}
    qr_text = str(payload.get("qr_text", "")).strip()
    source = str(payload.get("source", "MANUAL")).strip().upper() or "UNKNOWN"

    try:
        add_scan(qr_text, source)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except sqlite3.Error as exc:
        return jsonify({"error": f"database error: {exc}"}), 500

    return jsonify({"ok": True})


@app.route("/api/scans", methods=["GET"])
def api_scans():
    try:
        limit = int(request.args.get("limit", "300"))
    except ValueError:
        limit = 300
    limit = max(1, min(limit, 5000))
    try:
        return jsonify(list_scans(limit=limit))
    except sqlite3.Error as exc:
        return jsonify({"error": f"database error: {exc}"}), 500


@app.route("/api/gate-summary", methods=["GET"])
def api_gate_summary():
    try:
        limit = int(request.args.get("limit", "300"))
    except ValueError:
        limit = 300
    limit = max(1, min(limit, 5000))
    try:
        return jsonify(list_gate_summary(limit=limit))
    except sqlite3.Error as exc:
        return jsonify({"error": f"database error: {exc}"}), 500


@app.route("/api/export.csv")
def api_export_csv():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "scanned_at_utc", "qr_text", "source"])

    try:
        rows = list_scans(limit=200000)
    except sqlite3.Error as exc:
        return jsonify({"error": f"database error: {exc}"}), 500
    for row in reversed(rows):
        writer.writerow([row["id"], row["scanned_at_utc"], row["qr_text"], row["source"]])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=qr_scans.csv"},
    )


def main():
    port = int(os.environ.get("PORT", "5053"))
    host = os.environ.get("HOST", "0.0.0.0")
    app.run(host=host, port=port, debug=False, use_reloader=False)


init_db()


if __name__ == "__main__":
    main()
