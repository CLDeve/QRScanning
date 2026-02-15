# Gate Scanner App

Simple web app for QR scanning only.

## Features

- Camera-based QR scan in browser
- Scan history saved in SQLite
- CSV export of scan logs
- Office dashboard for backend monitoring (`/office`)
- Gate setup page for backend officers (`/office/gates`) with 2-6 doors per gate

## Local Run

```bash
git clone https://github.com/CLDeve/QRScanning.git
cd QRScanning
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
PORT=5053 python app.py
```

Open:

- `http://127.0.0.1:5053`
- `http://127.0.0.1:5053/office`
- `http://127.0.0.1:5053/office/gates`

## Deploy on Render (for phone testing)

This repo includes `render.yaml` and is ready for Render deploy.

1. Go to https://render.com and sign in.
2. Click `New +` -> `Blueprint`.
3. Connect your GitHub account and select repo `CLDeve/QRScanning`.
4. Confirm deploy.

Render will use:

- Build: `pip install -r requirements.txt`
- Start: `python app.py`
- DB path: `/tmp/qr_scans.db`

After deploy, Render gives an HTTPS URL like:

- `https://qr-scanner-xxxx.onrender.com`

Use that URL on your phone.

## Phone Camera Notes

- Use Chrome/Safari latest version.
- Grant camera permission when prompted.
- HTTPS is required for camera access (Render URL is HTTPS).
- App uses native `BarcodeDetector` with `jsQR` fallback for broader phone support.

## Data Store

- SQLite file: local `qr_scans.db`, Render `/tmp/qr_scans.db` (via `DB_PATH`).

Note: On free cloud hosting, SQLite may reset when the service restarts.
