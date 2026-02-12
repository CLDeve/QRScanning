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


INDEX_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>QR Scanner</title>
  <style>
    :root {
      --bg: #f2f5f8;
      --card: #ffffff;
      --ink: #0f172a;
      --muted: #64748b;
      --accent: #0f766e;
      --secondary: #1f2937;
      --border: #d5dee8;
      --danger: #b91c1c;
    }
    body {
      margin: 0;
      font-family: "Avenir Next", "Segoe UI", sans-serif;
      background: radial-gradient(circle at top right, #d9efe9 0, #f2f5f8 35%);
      color: var(--ink);
    }
    .wrap {
      max-width: 980px;
      margin: 24px auto;
      padding: 0 16px;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 16px;
      margin-bottom: 16px;
      box-shadow: 0 4px 20px rgba(10, 30, 50, 0.06);
    }
    h1 {
      margin: 0 0 8px;
      font-size: 24px;
    }
    .muted {
      color: var(--muted);
      font-size: 14px;
    }
    video {
      width: 100%;
      max-height: 340px;
      background: #111827;
      border-radius: 12px;
      margin-top: 10px;
    }
    #qr-canvas {
      display: none;
    }
    .actions {
      margin-top: 10px;
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    button {
      border: 0;
      border-radius: 10px;
      padding: 11px 14px;
      font-weight: 600;
      font-size: 14px;
      background: var(--accent);
      color: white;
      cursor: pointer;
    }
    .secondary {
      background: var(--secondary);
    }
    form {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
      margin-top: 10px;
    }
    input {
      width: 100%;
      box-sizing: border-box;
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px;
      font-size: 15px;
    }
    .result {
      margin-top: 8px;
      min-height: 18px;
      font-size: 14px;
      color: #0b5f59;
    }
    .error {
      color: var(--danger);
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
      margin-top: 10px;
    }
    th, td {
      text-align: left;
      padding: 9px;
      border-bottom: 1px solid var(--border);
      vertical-align: top;
    }
    th {
      font-size: 12px;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      color: var(--muted);
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>QR Scanner</h1>
      <div class="muted">Scan QR with camera. Every scan is saved in the system.</div>
      <video id="qr-video" autoplay playsinline muted></video>
      <canvas id="qr-canvas"></canvas>
      <div class="actions">
        <button id="start-scan" type="button">Start Camera Scan</button>
        <button id="stop-scan" type="button" class="secondary">Stop Scan</button>
      </div>

      <form id="manual-form">
        <input id="manual-text" placeholder="Manual QR text fallback">
        <button type="submit">Submit</button>
      </form>

      <div id="scan-result" class="result"></div>
    </div>

    <div class="card">
      <a href="/api/export.csv"><button type="button" class="secondary">Export CSV</button></a>
      <table>
        <thead>
          <tr>
            <th>Time (UTC)</th>
            <th>QR Text</th>
            <th>Source</th>
          </tr>
        </thead>
        <tbody id="rows"></tbody>
      </table>
    </div>
  </div>

  <script src="https://cdn.jsdelivr.net/npm/jsqr@1.4.0/dist/jsQR.min.js"></script>
  <script>
    const video = document.getElementById('qr-video');
    const canvas = document.getElementById('qr-canvas');
    const canvasCtx = canvas.getContext('2d', { willReadFrequently: true });
    const resultBox = document.getElementById('scan-result');
    let stream = null;
    let detector = null;
    let detectorMode = null;
    let scanning = false;
    let lastSentText = '';
    let lastSentAt = 0;

    function setResult(text, isError = false) {
      resultBox.textContent = text;
      resultBox.className = isError ? 'result error' : 'result';
    }

    async function refreshRows() {
      const res = await fetch('/api/scans?limit=400');
      if (!res.ok) return;
      const rows = await res.json();
      const tbody = document.getElementById('rows');
      tbody.innerHTML = '';

      rows.forEach((row) => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td>${row.scanned_at_utc}</td>
          <td>${row.qr_text}</td>
          <td>${row.source}</td>
        `;
        tbody.appendChild(tr);
      });
    }

    async function submitScan(qrText, source) {
      const payload = (qrText || '').trim();
      if (!payload) {
        return;
      }

      const now = Date.now();
      if (payload === lastSentText && now - lastSentAt < 1600) {
        return;
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
        return;
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
        return;
      }

      lastSentText = payload;
      lastSentAt = now;
      setResult('Scan saved');
      await refreshRows();
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
        const qrText = await detectQrFromFrame();
        if (qrText) {
          await submitScan(qrText, 'CAMERA');
        }
      } catch (_) {
        // keep loop alive
      }

      if (scanning) {
        requestAnimationFrame(scanLoop);
      }
    }

    async function startCameraScan() {
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
      setResult(detectorMode === 'barcode' ? 'Camera scan started' : 'Camera scan started (fallback mode)');
      scanLoop();
    }

    function stopCameraScan() {
      scanning = false;
      if (stream) {
        stream.getTracks().forEach((track) => track.stop());
        stream = null;
      }
      video.srcObject = null;
      setResult('Camera scan stopped');
    }

    document.getElementById('start-scan').addEventListener('click', startCameraScan);
    document.getElementById('stop-scan').addEventListener('click', stopCameraScan);

    document.getElementById('manual-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const input = document.getElementById('manual-text');
      const text = input.value;
      await submitScan(text, 'MANUAL');
      input.value = '';
    });

    refreshRows();
    setInterval(refreshRows, 2500);
  </script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(INDEX_TEMPLATE)


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
