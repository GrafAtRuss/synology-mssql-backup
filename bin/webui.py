#!/usr/bin/env python3
import json, os, subprocess, datetime, re, glob, time, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

VAR = "/var/packages/MSSQLBackup/var"
TARGET = "/var/packages/MSSQLBackup/target"
TASKS = os.path.join(VAR, "tasks.json")
LOG = os.path.join(VAR, "backup.log")
def sqlcmd_bin():
    arch = os.uname().machine
    name = "sqlcmd-linux-arm64" if arch in ("aarch64", "arm64") else "sqlcmd-linux-amd64"
    path = os.path.join(TARGET, "bin", name)
    return path if os.path.isfile(path) else ""

PORT = 8088

os.makedirs(VAR, exist_ok=True)

def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass

def read_tasks():
    if not os.path.exists(TASKS):
        return []
    try:
        with open(TASKS, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []

def write_tasks(tasks):
    tmp = TASKS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=2)
    os.replace(tmp, TASKS)

def browse(path):
    skip_prefix = ("@", ".", "#")
    path = path or "/"
    if path in ("", "/"):
        items = []
        for vol in sorted(glob.glob("/volume*")):
            if os.path.isdir(vol) and not os.path.islink(vol):
                items.append({"name": os.path.basename(vol), "path": vol})
        return {"path": "/", "parent": "", "items": items}
    path = os.path.realpath(path)
    if not path.startswith("/volume"):
        return {"path": "/", "parent": "", "items": [], "error": "Можно выбирать только папки на томах"}
    parent = os.path.dirname(path)
    if not parent.startswith("/volume"):
        parent = "/"
    if not os.path.isdir(path):
        return {"path": path, "parent": parent, "items": [], "error": "Папка не найдена"}
    try:
        names = sorted(os.listdir(path), key=str.lower)
    except OSError as e:
        return {"path": path, "parent": parent, "items": [], "error": "Нет доступа: %s" % e}
    items = []
    for name in names:
        if name.startswith(skip_prefix) or name in ("lost+found",):
            continue
        full = os.path.join(path, name)
        if os.path.isdir(full) and not os.path.islink(full):
            items.append({"name": name, "path": full})
    return {"path": path, "parent": parent, "items": items}

def list_baks(path):
    path = path or "/"
    if path in ("", "/"):
        return {"path": "/", "files": []}
    path = os.path.realpath(path)
    if not path.startswith("/volume"):
        return {"path": path, "files": [], "error": "Можно выбирать только файлы на томах"}
    if not os.path.isdir(path):
        return {"path": path, "files": [], "error": "Папка не найдена"}
    files = []
    try:
        names = os.listdir(path)
    except OSError as e:
        return {"path": path, "files": [], "error": "Нет доступа: %s" % e}
    for name in names:
        if not (name.lower().endswith(".bak") or name.lower().endswith(".bak.gz")):
            continue
        full = os.path.join(path, name)
        if not os.path.isfile(full):
            continue
        st = os.stat(full)
        files.append({"name": name, "path": full, "size": st.st_size, "mtime": int(st.st_mtime)})
    files.sort(key=lambda x: x["mtime"], reverse=True)
    return {"path": path, "files": files}

def list_versions(directory, db):
    directory = os.path.realpath(directory or "")
    if not directory.startswith("/volume") or not os.path.isdir(directory):
        return {"versions": [], "error": "Папка задачи недоступна"}
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", db or "")
    rx = re.compile(r"^" + re.escape(safe) + r"_(\d{8})_(\d{6})\.bak(\.gz)?$")
    versions = []
    try:
        names = os.listdir(directory)
    except OSError as e:
        return {"versions": [], "error": "Нет доступа: %s" % e}
    for name in names:
        match = rx.match(name)
        if not match:
            continue
        full = os.path.join(directory, name)
        if not os.path.isfile(full):
            continue
        ymd, hms = match.group(1), match.group(2)
        label = "%s.%s.%s %s:%s" % (ymd[6:8], ymd[4:6], ymd[0:4], hms[0:2], hms[2:4])
        versions.append({
            "path": full,
            "label": label,
            "size": os.path.getsize(full),
            "stamp": ymd + hms,
        })
    versions.sort(key=lambda item: item["stamp"], reverse=True)
    return {"versions": versions}

def make_dir(parent, name):
    parent = os.path.realpath(parent or "")
    if not parent.startswith("/volume") or not os.path.isdir(parent):
        return {"ok": False, "msg": "Сначала откройте папку на томе"}
    name = (name or "").strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name or len(name) > 64:
        return {"ok": False, "msg": "Недопустимое имя папки"}
    dest = os.path.join(parent, name)
    try:
        os.makedirs(dest, exist_ok=True)
    except OSError as e:
        return {"ok": False, "msg": "Не удалось создать папку: %s" % e}
    return {"ok": True, "path": dest}

def js_day(now):
    return (now.weekday() + 1) % 7

def task_due(task, now):
    if task.get("sched_on") is False or task.get("enabled") is False:
        return False
    days = task.get("days")
    if not isinstance(days, list) or not days:
        if task.get("sched") == "weekly":
            days = [0]
        else:
            days = [0, 1, 2, 3, 4, 5, 6]
    if js_day(now) not in [int(d) for d in days]:
        return False
    hour = int(task.get("hour") or 0)
    minute = int(task.get("min") or 0)
    repeat = int(task.get("repeat") or 0)
    if now.minute != minute:
        return False
    if repeat <= 0:
        return now.hour == hour
    if now.hour < hour:
        return False
    return (now.hour - hour) % repeat == 0

def scheduler_loop():
    last_file = os.path.join(VAR, "lastrun.json")
    while True:
        time.sleep(20)
        try:
            now = datetime.datetime.now()
            slot = now.strftime("%Y%m%d%H%M")
            try:
                with open(last_file, encoding="utf-8") as f:
                    done = json.load(f)
            except Exception:
                done = {}
            changed = False
            for task in read_tasks():
                if not task_due(task, now):
                    continue
                key = "%s:%s" % (task.get("id"), slot)
                if done.get(key):
                    continue
                done[key] = True
                changed = True
                log("Расписание: запуск «%s»" % (task.get("name") or task.get("id")))
                subprocess.Popen(
                    [os.path.join(TARGET, "bin/backup.sh"), str(task.get("id") or "")],
                    env=sql_env())
            if changed:
                keys = list(done)[-200:]
                done = {k: done[k] for k in keys}
                tmp = last_file + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(done, f)
                os.replace(tmp, last_file)
        except Exception as e:
            log("scheduler: %s" % e)

def sql_env():
    env = os.environ.copy()
    env["HOME"] = VAR
    env["USERPROFILE"] = VAR
    return env

def parse_dbs(text):
    rx = re.compile(r"^[A-Za-z0-9_#$@][A-Za-z0-9_#$@ .\-]{0,120}$")
    dbs = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("(") or "rows affected" in line.lower():
            continue
        if rx.match(line):
            dbs.append(line)
    return dbs

def sql_ident(name):
    if not re.match(r"^[A-Za-z0-9_#$@][A-Za-z0-9_#$@ .-]{0,120}$", name or ""):
        raise ValueError("bad database name")
    return name.replace("]", "]]")

def run_sql(host, port, user, password, query, timeout=25):
    path = sqlcmd_bin()
    if not path:
        return 127, "", "sqlcmd не найден в пакете"
    cmd = [path, "-S", "%s,%s" % (host, port or "1433"), "-U", user, "-P", password,
           "-C", "-l", "15", "-Q", query, "-h", "-1", "-W", "-b"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=sql_env())
    except subprocess.TimeoutExpired:
        return 124, "", "Таймаут подключения к SQL Server"
    except Exception as e:
        return 1, "", str(e)
    return p.returncode, p.stdout or "", (p.stderr or p.stdout or "").strip()

class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(200, {"ok": True})

    def _body(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/tasks":
            safe = []
            for t in read_tasks():
                c = dict(t)
                if c.get("pass"):
                    c["pass"] = "********"
                if c.get("smb_pass"):
                    c["smb_pass"] = "********"
                safe.append(c)
            self._send(200, {"tasks": safe, "sqlcmd": bool(sqlcmd_bin())})
            return
        if path == "/api/logs":
            text = ""
            if os.path.exists(LOG):
                with open(LOG, encoding="utf-8", errors="replace") as f:
                    text = f.read()[-30000:]
            self._send(200, {"logs": text})
            return
        if path == "/api/browse":
            q = parse_qs(urlparse(self.path).query)
            raw = (q.get("path") or ["/"])[0]
            self._send(200, browse(raw))
            return
        if path == "/api/files":
            q = parse_qs(urlparse(self.path).query)
            raw = (q.get("path") or ["/"])[0]
            self._send(200, list_baks(raw))
            return
        if path == "/api/versions":
            q = parse_qs(urlparse(self.path).query)
            self._send(200, list_versions((q.get("dir") or [""])[0], (q.get("db") or [""])[0]))
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        data = self._body()
        if path == "/api/list_dbs":
            code, out, err = run_sql(
                data.get("host", ""), data.get("port", "1433"),
                data.get("user", ""), data.get("pass", ""),
                "SET NOCOUNT ON; SELECT name FROM sys.databases WHERE database_id > 4 AND state = 0 ORDER BY name;")
            if code != 0:
                self._send(200, {"dbs": [], "error": err or out or ("код %s" % code)})
                return
            dbs = parse_dbs(out)
            self._send(200, {"dbs": dbs})
            return
        if path == "/api/tasks":
            tasks = data.get("tasks")
            if not isinstance(tasks, list):
                self._send(400, {"error": "tasks required"})
                return
            prev = {t.get("id"): t for t in read_tasks()}
            cleaned = []
            for t in tasks:
                tid = str(t.get("id") or "")
                if t.get("pass") == "********" and tid in prev:
                    t["pass"] = prev[tid].get("pass", "")
                if t.get("smb_pass") == "********" and tid in prev:
                    t["smb_pass"] = prev[tid].get("smb_pass", "")
                cleaned.append(t)
            write_tasks(cleaned)
            self._send(200, {"ok": True, "schedule": "Расписание выполняет служба пакета. Планировщик DSM не нужен."})
            return
        if path == "/api/backup":
            subprocess.Popen([os.path.join(TARGET, "bin/backup.sh"), str(data.get("id") or "")])
            self._send(200, {"ok": True, "msg": "Резервное копирование запущено"})
            return
        if path == "/api/install":
            script = os.path.join(TARGET, "bin/install_sqlcmd.sh")
            try:
                p = subprocess.run([script], capture_output=True, text=True, timeout=20, env=sql_env())
                msg = (p.stdout or p.stderr or "sqlcmd входит в состав пакета").strip()
                self._send(200, {"ok": p.returncode == 0, "msg": msg})
            except Exception as e:
                self._send(200, {"ok": False, "msg": str(e)})
            return
        if path == "/api/restore":
            bak = os.path.realpath(data.get("bak") or "")
            low = bak.lower()
            if not bak.startswith("/volume") or not os.path.isfile(bak) or not (low.endswith(".bak") or low.endswith(".bak.gz")):
                self._send(200, {"ok": False, "msg": "Выберите файл .bak на томе NAS"})
                return
            if not (data.get("host") and data.get("user") and data.get("dbname") and data.get("smb_share") and data.get("sql_dir")):
                self._send(200, {"ok": False, "msg": "Заполните сервер, имя базы, папку SQL и SMB-шару"})
                return
            req = os.path.join(VAR, "restore-%s.json" % int(time.time()))
            with open(req, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            subprocess.Popen(["/usr/bin/python3", os.path.join(TARGET, "bin/backup.py"), "--restore", req], env=sql_env())
            self._send(200, {"ok": True, "msg": "Восстановление запущено. Ход — в журнале задачи."})
            return
        if path == "/api/mkdir":
            self._send(200, make_dir(data.get("path") or "", data.get("name") or ""))
            return
        self._send(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        return

if __name__ == "__main__":
    threading.Thread(target=scheduler_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
