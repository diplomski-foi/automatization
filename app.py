

"""Streamlit UI for form_filler.py.

Run:  streamlit run app.py      (or run run_ui.py from PyCharm)
"""
import json
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd
import streamlit as st

from inputs import load_table, load_logins, parse_urls


def _under_streamlit() -> bool:
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        return get_script_run_ctx(suppress_warning=True) is not None
    except Exception:
        return False


if not _under_streamlit():
    # Started as a plain script (e.g. PyCharm "Run app.py"): relaunch via `streamlit run`.
    from streamlit.web import cli as stcli
    sys.argv = ["streamlit", "run", str(Path(__file__).resolve())]
    sys.exit(stcli.main())

BASE = Path(__file__).resolve().parent
RUNS = BASE / "runs"
DEFAULT_URLS = (BASE / "test_form.html").as_uri()

SAMPLE_DATA = (BASE / "samples" / "data.csv").read_text(encoding="utf-8") \
    if (BASE / "samples" / "data.csv").exists() else "first_name,last_name,email\n"
SAMPLE_LOGINS = "login_url,username,password\nhttps://example.com/login,user1,secret1\n"
SAMPLE_URLS = "# one URL per line\nhttps://example.com/form\nhttps://example.com/contact\n"

st.set_page_config(page_title="Form Filler", page_icon="📝", layout="wide")
st.title("📝 Form Filler")
st.caption("Fill web forms automatically from CSV/Excel data. "
           "Use it only on sites you own or are allowed to test.")


def save_upload(upload, folder: Path) -> Path:
    path = folder / upload.name
    path.write_bytes(upload.getvalue())
    return path


def preview_table(upload) -> list[dict]:
    tmp = RUNS / "_preview"
    tmp.mkdir(parents=True, exist_ok=True)
    return load_table(save_upload(upload, tmp))


# ---------------------------------------------------------------- inputs ----
col_a, col_b = st.columns(2)

with col_a:
    st.subheader("1. Target links")
    urls_text = st.text_area("Form URLs (one per line)", value=DEFAULT_URLS, height=140,
                             help="Blank lines and lines starting with # are ignored.")
    urls_file = st.file_uploader("...or upload a list (.txt / .csv with a url column)",
                                 type=["txt", "csv"], key="urls")
    st.download_button("Sample urls.txt", SAMPLE_URLS, "urls.txt")

with col_b:
    st.subheader("2. Form data")
    data_file = st.file_uploader("CSV / Excel / JSON, one row = one submission",
                                 type=["csv", "xlsx", "json"], key="data")
    fake_rows = st.number_input("...or generate N rows of fake data (no file)",
                                min_value=0, max_value=1000, value=0, step=1)
    if data_file is not None:
        try:
            rows = preview_table(data_file)
            st.write(f"{len(rows)} row(s)")
            st.dataframe(pd.DataFrame(rows).head(10), width='stretch')
        except Exception as e:  # noqa: BLE001
            st.error(f"Cannot read data file: {e}")
    st.download_button("Sample data.csv", SAMPLE_DATA, "data.csv")

st.subheader("3. Login (optional)")
col_c, col_d = st.columns(2)
with col_c:
    logins_file = st.file_uploader(
        "Accounts CSV/Excel (columns: username, password, optional login_url)",
        type=["csv", "xlsx", "json"], key="logins")
    st.download_button("Sample logins.csv", SAMPLE_LOGINS, "logins.csv")
with col_d:
    login_url = st.text_input("Default login URL (for accounts without login_url)")
    if logins_file is not None:
        try:
            tmp = RUNS / "_preview"
            tmp.mkdir(parents=True, exist_ok=True)
            accounts = load_logins(save_upload(logins_file, tmp))
            masked = [{"account": a["label"], "login_url": a["login_url"] or "(default)",
                       "fields": ", ".join(a["creds"])} for a in accounts]
            st.write(f"{len(accounts)} account(s) — passwords are not shown")
            st.dataframe(pd.DataFrame(masked), width='stretch')
        except Exception as e:  # noqa: BLE001
            st.error(f"Cannot read accounts file: {e}")
    st.caption("Each account runs in its own isolated browser session. "
               "Use test accounts only.")

# --------------------------------------------------------------- options ----
st.subheader("4. Options")
o1, o2, o3, o4 = st.columns(4)
with o1:
    mode = st.selectbox("Combine links and rows", ["all", "pair"],
                        help="all: every link x every row.  pair: link 1 with row 1, ...")
    submit = st.checkbox("Click submit", value=True)
with o2:
    fake_missing = st.checkbox("Generate fake data for empty fields", value=False)
    retries = st.number_input("Retries after validation errors", 0, 5, 1)
with o3:
    headless = st.checkbox("Headless (no browser window)", value=False,
                           help="CAPTCHAs need a visible window so you can solve them.")
    slow = st.number_input("Slow down actions (ms)", 0, 3000, 600, step=100,
                           help="Delay between every browser action so you can follow it.")
    pause = st.number_input("Pause after each job (s)", 0, 60, 3,
                            help="Time to look at the result before the next job.")
    hold = st.number_input("Keep browser open at the end (s)", 0, 600, 30)
with o4:
    workers = st.number_input("Parallel workers", 1, 8, 1,
                              help="Runs several browsers at once. CAPTCHAs cannot be "
                                   "solved in parallel mode.")
    next_text = st.text_input("Extra 'next' button texts (comma separated)")
    cookies = st.selectbox("Cookie banners", ["accept", "reject", "ignore"],
                           help="accept: click Accept all.  reject: click Reject / "
                                "Necessary only.  ignore: do nothing.")
submit_selector = st.text_input("Submit button CSS selector (optional)")
next_selector = st.text_input("Next button CSS selector (optional)")
with st.expander("Field detection tools"):
    debug = st.checkbox("Debug: log the signals and top candidate keys of every field")
    field_map_text = st.text_area(
        "Manual field map (JSON) - overrides automatic matching",
        placeholder='{"input[name=\'entry.123\']": "=blue", "#phone": "phone"}',
        help='Keys are CSS selectors. Values are a data column name, or "=text" for a '
             'fixed literal value.')

# ------------------------------------------------------------------ run -----
b1, b2 = st.columns(2)
run_clicked = b1.button("▶ Run", type="primary")
inspect_clicked = b2.button("🔍 Inspect fields only (nothing is filled)")
if run_clicked or inspect_clicked:
    urls = parse_urls(urls_text)
    run_dir = RUNS / time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    if urls_file is not None:
        p = save_upload(urls_file, run_dir)
        from inputs import load_urls
        urls += load_urls(p)
    if not urls:
        st.error("Add at least one URL.")
        st.stop()

    (run_dir / "urls.txt").write_text("\n".join(urls), encoding="utf-8")
    cmd = [sys.executable, "-u", str(BASE / "form_filler.py"),
           "--urls-file", str(run_dir / "urls.txt"), "--out", str(run_dir),
           "--mode", mode, "--retries", str(retries), "--workers", str(workers),
           "--slow", str(slow), "--cookies", cookies,
           "--pause", str(pause), "--hold", str(hold)]
    if data_file is not None:
        cmd += ["--data", str(save_upload(data_file, run_dir))]
    elif fake_rows > 0:
        cmd += ["--rows", str(int(fake_rows))]
    else:
        st.error("Upload a data file or choose how many fake rows to generate.")
        st.stop()
    if logins_file is not None:
        cmd += ["--logins", str(save_upload(logins_file, run_dir))]
        if login_url:
            cmd += ["--login-url", login_url]
    if not submit:
        cmd.append("--no-submit")
    if fake_missing:
        cmd.append("--fake-missing")
    if headless:
        cmd.append("--headless")
    for t in [x.strip() for x in next_text.split(",") if x.strip()]:
        cmd += ["--next-text", t]
    if submit_selector:
        cmd += ["--submit-selector", submit_selector]
    if next_selector:
        cmd += ["--next-selector", next_selector]
    if debug:
        cmd.append("--debug")
    if inspect_clicked:
        cmd.append("--inspect")
        if not headless:
            cmd.append("--headless")
    if field_map_text.strip():
        try:
            json.loads(field_map_text)
        except json.JSONDecodeError as e:
            st.error(f"Field map is not valid JSON: {e}")
            st.stop()
        (run_dir / "map.json").write_text(field_map_text, encoding="utf-8")
        cmd += ["--map", str(run_dir / "map.json")]

    st.info(f"Run folder: `{run_dir}`")
    log_box = st.empty()
    lines: list[str] = []
    proc = subprocess.Popen(cmd, cwd=BASE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        lines.append(line)
        log_box.code("".join(lines[-40:]), language="text")
    code = proc.wait()
    (run_dir / "log.txt").write_text("".join(lines), encoding="utf-8")
    st.session_state["last_run"] = str(run_dir)
    (st.success if code == 0 else st.error)(f"Finished with exit code {code}")

# --------------------------------------------------------------- results ----
last = st.session_state.get("last_run")
if last:
    run_dir = Path(last)
    report = run_dir / "report.json"
    if report.exists():
        results = json.loads(report.read_text(encoding="utf-8"))
        st.subheader("Results")
        df = pd.DataFrame([{
            "job": r["job"], "account": r.get("account"), "url": r["url"],
            "row": r["row"], "status": r["status"],
            "filled": len(r.get("filled", [])), "attempts": r.get("attempts"),
            "invalid": ", ".join(f["field"] for f in r.get("invalid_fields", [])),
            "error": r.get("error"),
        } for r in results])
        st.dataframe(df, width='stretch')
        st.download_button("Download report.json", report.read_bytes(), "report.json")
        for r in results:
            with st.expander(f"Job {r['job']} — {r['status']} — {r['url']}"):
                if r.get("fields"):
                    st.caption("Detected fields and the best matching data key")
                    st.dataframe(pd.DataFrame([{
                        "kind": f["kind"], "name": f["name"], "id": f["id"],
                        "label": f["label"], "label from": f["label_source"],
                        "class": f["class"][:40],
                        "best key": f["top"][0]["key"] if f["top"] else "",
                        "score": f["top"][0]["score"] if f["top"] else 0,
                        "via": f["top"][0]["source"] if f["top"] else "",
                        "date part": f.get("part") or "",
                    } for f in r["fields"]]), width='stretch')
                if r.get("filled"):
                    st.dataframe(pd.DataFrame(r["filled"]), width='stretch')
                if r.get("page_errors"):
                    st.warning("Page errors: " + "; ".join(r["page_errors"]))
                shot = run_dir / r.get("screenshot", "")
                if r.get("screenshot") and shot.exists():
                    st.image(str(shot))
