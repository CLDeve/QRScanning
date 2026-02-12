# QR Scanner App

Simple local web app for QR scanning only.

## Features

- Camera-based QR scan in browser
- Manual QR text submission fallback
- Scan history saved in SQLite
- CSV export of scan logs

## Data store

- SQLite file: `qr_scans.db`

## Setup

```bash
cd "/Users/charlesliew/Desktop/Certis/CAS/NFC Reader"
rm -rf .venv
/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## Run

```bash
PORT=5053 python app.py
```

Open:

- `http://127.0.0.1:5053`

## Notes

- Browser camera scan requires `BarcodeDetector` support (Chrome recommended).
- If camera scan is unavailable, use manual text submission.
