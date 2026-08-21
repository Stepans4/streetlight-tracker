# Street Light Outage Tracker

Local tracker for street light outages, damages, and cable thefts.

Built for city street-lighting work: circuit + light number, active call log, searchable history, and a downstream/duplicate check for series circuits.

## What it does

- Log calls by **circuit number** and **street light number**
- Types: Outage, Damage, Cable Theft, Other
- Searchable **active** call log
- Complete a call (moves it to history with timestamp + notes)
- Full **searchable history** (circuit, light, type, dates, keywords)
- Enter **LUB** (last unit burning) and **FUD** (first unit dark)
- Flag a new call if it looks like the same break (series fault or pedestal cut on parallel LED)
- Store circuit PDFs and extract their text for search
- Enter or import the **order of lights** on a circuit (needed for LUB/FUD overlap)

## What the PDF feature can and cannot do

Circuit map PDFs are usually drawings. Software cannot reliably read “this pole is downstream of that pole” from a scanned one-line or GIS print.

The app therefore:

1. Lets you upload PDFs and keep them with the circuit
2. Extracts any text so you can search it
3. Uses a **light sequence you define** (or import from CSV) for downstream logic

If your PDFs are actually numbered lists of lights in order from the source, you can type or paste that list into the circuit record.

## Run it

You need Python 3.10+ installed.

```bash
cd streetlight_tracker
python -m venv .venv

# Windows
.venv\Scripts\activate

# Mac / Linux
source .venv/bin/activate

pip install -r requirements.txt
streamlit run app.py
```

The browser will open. Without Turso credentials, data is stored in `data/tracker.db` on your machine. PDFs go in `data/pdfs/`.

## Turso cloud database (for iPad / Streamlit Cloud)

Local SQLite on Streamlit Community Cloud can be wiped when the app restarts. Turso keeps tickets permanently.

### 1. Create a Turso database
1. Sign up at [turso.tech](https://turso.tech) (GitHub login works).
2. Install the CLI (optional but easiest):
   ```bash
   curl -sSfL https://get.tur.so/install.sh | bash
   turso auth login
   turso db create streetlight-tracker
   turso db show streetlight-tracker --url
   turso db tokens create streetlight-tracker
   ```
3. Copy the **URL** (`libsql://...`) and **token**.

### 2. Add secrets on Streamlit Cloud
In your app on [share.streamlit.io](https://share.streamlit.io) → **Settings** → **Secrets**, paste:

```toml
TURSO_DATABASE_URL = "libsql://streetlight-tracker-YOURORG.turso.io"
TURSO_AUTH_TOKEN = "paste-token-here"
```

Redeploy (or reboot) the app. The sidebar should say **Database: Turso (cloud)**.

### 3. Local testing with Turso
Create `.streamlit/secrets.toml` (do not commit it):

```toml
TURSO_DATABASE_URL = "libsql://..."
TURSO_AUTH_TOKEN = "..."
```

Then `streamlit run app.py` uses Turso instead of the local file.

## Importing light order (CSV)

On the Circuits page you can import a CSV with columns:

```
circuit_number,light_number,sequence,location
123,1,1,N 27th & Capitol
123,2,2,N 27th mid-block
123,3,3,N 27th & Keefe
```

`sequence` is the order from the source (1 = first / upstream). Lower number = closer to the feed.

## Series vs multiple / parallel LED

Milwaukee has both:

- **Series** (legacy constant-current): a fault can take out every light after the break. The downstream check applies here.
- **Multiple / Parallel LED**: one fixture can fail and the rest stay on. The app will **not** treat a later light as automatically downstream of an earlier outage. It still warns if that same circuit already has an active ticket (shared cable cut, cabinet, fuse, etc.).

Mark each circuit’s type when you add it. If you leave it as Unknown, you only get the “other tickets on this circuit” warning.
