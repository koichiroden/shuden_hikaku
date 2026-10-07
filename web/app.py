# -*- coding: utf-8 -*-
"""
ブラウザから「詰み度」動画を生成するための簡単なWebページ。
自分のPC(`python3 web/app.py`)でも、Renderなどのホスティング上でも同じように動く。

- 原稿ツール(Claude Artifact)の「設定をコピー」で得たJSONを貼り付けて生成する。
- stations/*.json に保存済みの設定からも生成できる。
- 進み具合はポーリングで表示し、終わったら mp4・原稿・字幕をダウンロードできる。

ローカルで試す場合:
    pip install -r requirements.txt && playwright install chromium
    python3 web/app.py   ->  http://127.0.0.1:8080
"""
import json
import os
import re
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request, send_file

BASE = Path(__file__).resolve().parent.parent
STATIONS = BASE / "stations"
OUTPUT = BASE / "output"
OUTPUT.mkdir(exist_ok=True)
JOB_TIMEOUT_SEC = 3600
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
FILES = {"mp4": ".mp4", "txt": ".txt", "srt": ".srt", "lines": "_lines.txt", "sources": "_sources.txt"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
jobs, lock = {}, threading.Lock()
busy = threading.Lock()          # 同時に走らせる生成は1本だけ(メモリ節約)


@app.after_request
def no_cache(resp):
    resp.headers["Cache-Control"] = "no-store"
    return resp


def set_job(job_id, **kw):
    with lock:
        jobs[job_id].update(kw)


def run_job(job_id, name, fps):
    with busy:
        set_job(job_id, state="running", note="準備中")
        cmd = [sys.executable, "-u", str(BASE / "render.py"), str(OUTPUT / f"{name}.json"), "--out", str(OUTPUT), "--fps", str(fps)]
        log = []
        try:
            proc = subprocess.Popen(cmd, cwd=BASE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
            timer = threading.Timer(JOB_TIMEOUT_SEC, proc.kill)
            timer.start()
            buf = ""
            while True:
                ch = proc.stdout.read(1)
                if not ch:
                    break
                if ch in "\r\n":
                    line, buf = buf.strip(), ""
                    if not line:
                        continue
                    m = re.match(r"(\d+)%", line)
                    if m:
                        set_job(job_id, percent=int(m.group(1)), note=line)
                    else:
                        log.append(line)
                        if "点「" in line:
                            set_job(job_id, summary=line)
                else:
                    buf += ch
            timer.cancel()
            ok = proc.wait() == 0 and (OUTPUT / f"{name}.mp4").exists()
            set_job(job_id, state="done" if ok else "error", percent=100 if ok else jobs[job_id].get("percent", 0),
                    log="\n".join(log[-12:]))
        except Exception as e:      # 起動に失敗した場合など
            set_job(job_id, state="error", log=str(e))


PAGE = """<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>詰み度 動画ジェネレーター</title><style>
body{font-family:"Hiragino Sans","Yu Gothic",Meiryo,sans-serif;background:#0e1120;color:#e8eaf4;margin:0;padding:20px 16px 48px;line-height:1.7}
main{max-width:860px;margin:0 auto;display:flex;flex-direction:column;gap:16px}
h1{font-size:22px;margin:0;color:#ffb02e} h2{font-size:15px;margin:0 0 8px}
section{background:#171b2e;border:1px solid #2c3250;border-radius:6px;padding:14px}
textarea,select,input{width:100%;box-sizing:border-box;font:inherit;font-size:14px;color:#e8eaf4;background:#0e1120;border:1px solid #2c3250;border-radius:4px;padding:8px}
textarea{min-height:200px;font-family:ui-monospace,monospace;font-size:12px}
.row{display:flex;flex-wrap:wrap;gap:10px;align-items:end;margin-top:10px}.row>label{flex:1;min-width:160px;font-size:12px;color:#9aa1bd}
button{font:inherit;font-weight:700;background:#ffb02e;color:#0e1120;border:0;border-radius:4px;padding:9px 18px;cursor:pointer}button:disabled{opacity:.5;cursor:default}
.bar{height:10px;background:#0e1120;border-radius:5px;overflow:hidden;margin:8px 0}.bar i{display:block;height:100%;width:0;background:#ffb02e}
a{color:#5ec8ff} p{margin:0;font-size:13px;color:#9aa1bd} pre{white-space:pre-wrap;font-size:12px;color:#ff7d9a;margin:8px 0 0}
#links a{display:inline-block;margin-right:14px;font-weight:700}
</style></head><body><main>
<h1>終電寝過ごし「詰み度」動画ジェネレーター</h1>
<section><h2>設定(JSON)</h2>
<p>原稿ツールの「設定をコピー」で得たJSONを貼り付けます。保存済みの設定から読み込むこともできます。</p>
<div class="row"><label>保存済みの設定<select id="saved"><option value="">(選択しない)</option>{% for s in saved %}<option>{{s}}</option>{% endfor %}</select></label></div>
<div class="row"><textarea id="json" placeholder='{"station": "大月", ...}'></textarea></div>
<div class="row"><label>出力ファイル名(半角英数字)<input id="name" placeholder="otsuki"></label>
<label>画質<select id="fps"><option value="30">本番(30コマ/秒)</option><option value="10">確認用(10コマ/秒・速い)</option></select></label>
<button id="go">動画を生成</button></div></section>
<section id="status" hidden><h2 id="title">生成中</h2><p id="summary"></p><div class="bar"><i id="fill"></i></div><p id="note"></p><div id="links"></div><pre id="log"></pre></section>
</main><script>
const $=id=>document.getElementById(id);
$("saved").addEventListener("change",async e=>{if(!e.target.value)return;const r=await fetch("/station/"+e.target.value);$("json").value=await r.text();$("name").value=e.target.value});
$("go").addEventListener("click",async()=>{$("go").disabled=true;$("status").hidden=false;$("title").textContent="生成中";$("links").innerHTML="";$("log").textContent="";$("fill").style.width="0";$("note").textContent="受付中";$("summary").textContent="";
  const r=await fetch("/generate",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({json:$("json").value,name:$("name").value,fps:+$("fps").value})}),j=await r.json();
  if(!r.ok){$("title").textContent="生成できませんでした";$("log").textContent=j.error;$("go").disabled=false;return}
  const t=setInterval(async()=>{const s=await (await fetch("/status/"+j.job)).json();$("fill").style.width=(s.percent||0)+"%";$("note").textContent=s.state=="queued"?"順番待ち":(s.note||"");$("summary").textContent=s.summary||"";
    if(s.state=="done"||s.state=="error"){clearInterval(t);$("go").disabled=false;$("title").textContent=s.state=="done"?"完成しました":"失敗しました";$("log").textContent=s.state=="error"?(s.log||""):"";
      if(s.state=="done")$("links").innerHTML=[["mp4","動画(MP4)"],["txt","原稿"],["srt","字幕(SRT)"],["sources","出典"]].map(([k,l])=>`<a href="/download/${j.name}/${k}">${l}</a>`).join("")}},2000)});
</script></body></html>"""


@app.route("/")
def index():
    return render_template_string(PAGE, saved=sorted(p.stem for p in STATIONS.glob("*.json")))


@app.route("/station/<name>")
def station(name):
    path = STATIONS / f"{name}.json"
    if not NAME_RE.match(name) or not path.exists():
        return "not found", 404
    return send_file(path, mimetype="application/json")


@app.route("/generate", methods=["POST"])
def generate():
    body = request.get_json(silent=True) or {}
    try:
        cfg = json.loads(body.get("json") or "")
        if not isinstance(cfg, dict):
            raise ValueError
    except ValueError:
        return jsonify(error="JSONとして読み取れません。原稿ツールの「設定をコピー」の内容をそのまま貼り付けてください。"), 400
    name = (body.get("name") or "").strip() or "video"
    if not NAME_RE.match(name):
        return jsonify(error="出力ファイル名は半角の英数字、ハイフン、アンダースコアで指定してください。"), 400
    fps = 10 if body.get("fps") == 10 else 30
    (OUTPUT / f"{name}.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    mp4 = OUTPUT / f"{name}.mp4"
    if mp4.exists():
        mp4.unlink()
    job_id = uuid.uuid4().hex
    with lock:
        jobs[job_id] = {"state": "queued", "percent": 0, "name": name}
    threading.Thread(target=run_job, args=(job_id, name, fps), daemon=True).start()
    return jsonify(job=job_id, name=name)


@app.route("/status/<job_id>")
def status(job_id):
    with lock:
        job = dict(jobs.get(job_id) or {})
    return (jsonify(job), 200) if job else (jsonify(error="not found"), 404)


@app.route("/download/<name>/<kind>")
def download(name, kind):
    if not NAME_RE.match(name) or kind not in FILES:
        return "not found", 404
    path = OUTPUT / f"{name}{FILES[kind]}"
    if not path.exists():
        return "not found", 404
    return send_file(path, as_attachment=True, download_name=path.name)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), threaded=True)
