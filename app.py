import io
import os
import tempfile
import threading
import uuid
import zipfile

import requests
from flask import Flask, jsonify, render_template, request, send_file

app = Flask(__name__)
jobs = {}

JENKINS_BASE = "https://updates.jenkins.io"
HEADERS = {"User-Agent": "jenkins-plugin-manager/2.12.0"}


# ---------------------------------------------------------------------------
# Update-center helpers
# ---------------------------------------------------------------------------

def fetch_update_center(jenkins_version=None):
    """Return (plugins_dict, source_url) from the appropriate update center."""
    urls = []
    if jenkins_version:
        urls.append(f"{JENKINS_BASE}/{jenkins_version}/update-center.actual.json")
    urls.append(f"{JENKINS_BASE}/current/update-center.actual.json")

    for url in urls:
        try:
            resp = requests.get(url, timeout=30, headers=HEADERS)
            resp.raise_for_status()
            data = resp.json()
            return data.get("plugins", {}), url
        except Exception:
            continue
    return {}, None


def uc_lookup(uc_plugins, name):
    """Look up a plugin in the update-center dict, trying alternate name forms."""
    return (
        uc_plugins.get(name)
        or uc_plugins.get(name.replace("-", "_"))
        or uc_plugins.get(name.replace("_", "-"))
    )


# ---------------------------------------------------------------------------
# MANIFEST.MF helpers (fallback when plugin not in update center)
# ---------------------------------------------------------------------------

def parse_manifest(text):
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


def parse_manifest_deps(dep_str):
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


# ---------------------------------------------------------------------------
# Recursive downloader
# ---------------------------------------------------------------------------

def download_plugin(name, version, uc_plugins, downloaded, downloaded_list, zf, log, include_optional):
    key = name.lower()
    if key in downloaded:
        return
    downloaded.add(key)

    uc_info = uc_lookup(uc_plugins, name)

    # Resolve version
    resolved_version = version
    if version.lower() == "latest" and uc_info:
        resolved_version = uc_info["version"]

    # Pick download URL
    using_uc = uc_info and resolved_version == uc_info.get("version")
    url = uc_info["url"] if using_uc else f"{JENKINS_BASE}/download/plugins/{name}/{resolved_version}/{name}.hpi"

    log.append(f"↓ {name}  {resolved_version}")

    try:
        resp = requests.get(url, timeout=60, allow_redirects=True, headers=HEADERS)
        resp.raise_for_status()
        hpi_data = resp.content
    except requests.HTTPError as exc:
        log.append(f"  ✗ HTTP {exc.response.status_code} — skipping {name}")
        return
    except Exception as exc:
        log.append(f"  ✗ {exc} — skipping {name}")
        return

    size_kb = len(hpi_data) // 1024
    actual_version = resolved_version
    deps = []

    if using_uc:
        for dep in uc_info.get("dependencies", []):
            deps.append((dep["name"], dep["version"], dep.get("optional", False)))
    else:
        try:
            with zipfile.ZipFile(io.BytesIO(hpi_data)) as inner:
                manifest_text = inner.read("META-INF/MANIFEST.MF").decode("utf-8", errors="replace")
            h = parse_manifest(manifest_text)
            actual_version = h.get("Plugin-Version", resolved_version)
            deps = parse_manifest_deps(h.get("Plugin-Dependencies", ""))
        except Exception as exc:
            log.append(f"  ⚠ Could not read manifest for {name}: {exc}")

    zf.writestr(f"{name}.hpi", hpi_data)
    downloaded_list.append({"name": name, "version": actual_version, "size_kb": size_kb})
    log.append(f"  ✓ {name} {actual_version}  ({size_kb} KB)")

    for dep_name, dep_version, optional in deps:
        if optional and not include_optional:
            log.append(f"  – skip optional: {dep_name} {dep_version}")
            continue
        download_plugin(dep_name, dep_version, uc_plugins, downloaded, downloaded_list, zf, log, include_optional)


# ---------------------------------------------------------------------------
# Job runner
# ---------------------------------------------------------------------------

def run_job(job_id, plugin_name, version, jenkins_version, include_optional):
    job = jobs[job_id]
    log = job["log"]
    jv_label = f", Jenkins {jenkins_version}" if jenkins_version else " (latest Jenkins)"
    log.append(f"Starting: {plugin_name}  version={version}{jv_label}\n")

    log.append(f"Fetching update center{' for Jenkins ' + jenkins_version if jenkins_version else ''}...")
    uc_plugins, uc_url = fetch_update_center(jenkins_version)
    if uc_url:
        log.append(f"  Loaded {len(uc_plugins)} plugins from {uc_url}\n")
    else:
        log.append("  ⚠ Could not load update center — using direct URLs\n")

    try:
        buf = io.BytesIO()
        downloaded = set()
        downloaded_list = []
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            download_plugin(plugin_name, version, uc_plugins, downloaded, downloaded_list, zf, log, include_optional)

        buf.seek(0)
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
        tmp.write(buf.read())
        tmp.close()

        job["zip_path"] = tmp.name
        job["zip_name"] = f"{plugin_name}-plugins.zip"
        job["plugins"] = sorted(downloaded_list, key=lambda x: x["name"])
        job["status"] = "done"
        log.append(f"\nDone — {len(downloaded)} plugin(s) packaged.")
    except Exception as exc:
        job["status"] = "error"
        log.append(f"\nFATAL: {exc}")


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/start", methods=["POST"])
def start():
    data = request.get_json() or {}
    plugin_name = (data.get("plugin_name") or "").strip()
    version = (data.get("version") or "latest").strip() or "latest"
    jenkins_version = (data.get("jenkins_version") or "").strip() or None
    include_optional = bool(data.get("include_optional", False))

    if not plugin_name:
        return jsonify({"error": "Plugin name is required"}), 400

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "running", "log": [], "zip_path": None, "zip_name": None, "plugins": []}

    t = threading.Thread(target=run_job, args=(job_id, plugin_name, version, jenkins_version, include_optional))
    t.daemon = True
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"status": job["status"], "log": job["log"], "plugins": job.get("plugins", [])})


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
