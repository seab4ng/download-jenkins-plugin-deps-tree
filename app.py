import io
import os
import tempfile
import threading
import uuid
import zipfile

import requests
from flask import Flask, jsonify, render_template, request, send_file

app = Flask(__name__)
jobs = {}  # job_id -> {status, log, zip_path, zip_name}

JENKINS_BASE = "https://updates.jenkins.io"


def get_plugin_url(name, version):
    if version.lower() == "latest":
        return f"{JENKINS_BASE}/latest/{name}.hpi"
    return f"{JENKINS_BASE}/download/plugins/{name}/{version}/{name}.hpi"


def parse_manifest(text):
    """Parse a MANIFEST.MF file handling RFC 822 line folding."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    unfolded = []
    for line in lines:
        if line.startswith(" ") and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    headers = {}
    for line in unfolded:
        if ":" in line:
            key, _, value = line.partition(":")
            headers[key.strip()] = value.strip()
    return headers


def parse_dependencies(dep_str):
    """Return list of (name, version, is_optional) from Plugin-Dependencies value."""
    deps = []
    if not dep_str:
        return deps
    for part in dep_str.split(","):
        part = part.strip()
        if not part:
            continue
        optional = ";resolution:=optional" in part
        clean = part.replace(";resolution:=optional", "").strip()
        pieces = clean.split(":")
        if len(pieces) >= 2:
            deps.append((pieces[0].strip(), pieces[1].strip(), optional))
    return deps


def download_plugin(name, version, downloaded, zf, log, include_optional):
    key = name.lower()
    if key in downloaded:
        return
    downloaded.add(key)

    url = get_plugin_url(name, version)
    log.append(f"↓ {name}  {version}  {url}")

    try:
        resp = requests.get(url, timeout=60, allow_redirects=True)
        resp.raise_for_status()
        hpi_data = resp.content
    except requests.HTTPError as exc:
        log.append(f"  ✗ HTTP {exc.response.status_code} — skipping {name}")
        return
    except Exception as exc:
        log.append(f"  ✗ {exc} — skipping {name}")
        return

    size_kb = len(hpi_data) // 1024

    actual_version = version
    deps = []
    try:
        with zipfile.ZipFile(io.BytesIO(hpi_data)) as inner:
            manifest_text = inner.read("META-INF/MANIFEST.MF").decode("utf-8", errors="replace")
        headers = parse_manifest(manifest_text)
        actual_version = headers.get("Plugin-Version", version)
        deps = parse_dependencies(headers.get("Plugin-Dependencies", ""))
    except Exception as exc:
        log.append(f"  ⚠ Could not read manifest for {name}: {exc}")

    zf.writestr(f"{name}.hpi", hpi_data)
    log.append(f"  ✓ {name} {actual_version}  ({size_kb} KB)")

    for dep_name, dep_version, optional in deps:
        if optional and not include_optional:
            log.append(f"  – skip optional: {dep_name} {dep_version}")
            continue
        download_plugin(dep_name, dep_version, downloaded, zf, log, include_optional)


def run_job(job_id, plugin_name, version, include_optional):
    job = jobs[job_id]
    log = job["log"]
    opt_label = " (+ optional deps)" if include_optional else ""
    log.append(f"Starting: {plugin_name}  version={version}{opt_label}\n")

    try:
        buf = io.BytesIO()
        downloaded = set()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            download_plugin(plugin_name, version, downloaded, zf, log, include_optional)

        buf.seek(0)
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
        tmp.write(buf.read())
        tmp.close()

        job["zip_path"] = tmp.name
        job["zip_name"] = f"{plugin_name}-plugins.zip"
        job["status"] = "done"
        log.append(f"\nDone — {len(downloaded)} plugin(s) packaged.")
    except Exception as exc:
        job["status"] = "error"
        log.append(f"\nFATAL: {exc}")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/start", methods=["POST"])
def start():
    data = request.get_json() or {}
    plugin_name = (data.get("plugin_name") or "").strip()
    version = (data.get("version") or "latest").strip() or "latest"
    include_optional = bool(data.get("include_optional", False))

    if not plugin_name:
        return jsonify({"error": "Plugin name is required"}), 400

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "running", "log": [], "zip_path": None, "zip_name": None}

    t = threading.Thread(target=run_job, args=(job_id, plugin_name, version, include_optional))
    t.daemon = True
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"status": job["status"], "log": job["log"]})


@app.route("/result/<job_id>")
def result(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done" or not job["zip_path"]:
        return jsonify({"error": "Not ready"}), 404
    return send_file(
        job["zip_path"],
        as_attachment=True,
        download_name=job["zip_name"],
        mimetype="application/zip",
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
