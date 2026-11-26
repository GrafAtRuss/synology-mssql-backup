#!/usr/bin/env python3
import json, os, sys, datetime, subprocess, re

VAR = "/var/packages/MSSQLBackup/var"
TARGET = "/var/packages/MSSQLBackup/target"
TASKS = os.path.join(VAR, "tasks.json")
LOG = os.path.join(VAR, "backup.log")
def sqlcmd_bin():
    arch = os.uname().machine
    name = "sqlcmd-linux-arm64" if arch in ("aarch64", "arm64") else "sqlcmd-linux-amd64"
    path = os.path.join(TARGET, "bin", name)
    return path if os.path.isfile(path) else ""


def log(msg):
    line = "%s %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def sql_ident(name):
    if not re.match(r"^[A-Za-z0-9_#$@][A-Za-z0-9_#$@ .-]{0,120}$", name or ""):
        raise ValueError("недопустимое имя базы: %s" % name)
    return name.replace("]", "]]")

def win_join(folder, filename):
    folder = (folder or r"C:\SQLBackups").rstrip("\\/")
    return folder + "\\" + filename

def parse_unc(unc):
    s = (unc or "").strip().replace("/", "\\")
    if s.startswith("\\\\"):
        s = s[2:]
    parts = [p for p in s.split("\\") if p]
    if len(parts) < 2:
        raise ValueError("SMB-шара должна быть вида \\\\сервер\\share")
    return parts[0], parts[1], parts[2:]

def smb_get(unc, user, password, filename, dest):
    host, share, subs = parse_unc(unc)
    remote = "/".join(subs + [filename]) if subs else filename
    smb = None
    for cand in ("/usr/bin/smbclient", "/bin/smbclient", "smbclient"):
        if cand == "smbclient" or os.path.isfile(cand):
            smb = cand
            break
    auth = "%s%%%s" % (user, password) if user else "%"
    cmd = [smb, "//%s/%s" % (host, share), "-U", auth, "-c", "get %s %s" % (remote, dest)]
    log("smbclient get %s -> %s" % (remote, dest))
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or "smbclient error").strip()[:800])

def backup_one(task, db):
    host = task.get("host") or ""
    port = str(task.get("port") or "1433")
    user = task.get("user") or ""
    password = task.get("pass") or ""
    sql_dir = task.get("sql_dir") or r"C:\SQLBackups"
    nas_dir = task.get("nas_dir") or "/volume1/SQLBackups"
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = "%s_%s.bak" % (re.sub(r"[^A-Za-z0-9_.-]", "_", db), stamp)
    remote = win_join(sql_dir, filename)
    ident = sql_ident(db)
    query = "BACKUP DATABASE [%s] TO DISK = N'%s' WITH INIT, STATS = 10" % (ident, remote.replace("'", "''"))
    sqlcmd = sqlcmd_bin()
    if not sqlcmd:
        raise RuntimeError("sqlcmd не найден в пакете")
    log("BACKUP %s -> %s" % (db, remote))
    cmd = [sqlcmd, "-S", "%s,%s" % (host, port), "-U", user, "-P", password, "-C", "-l", "30", "-b", "-Q", query]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=7200, env=dict(os.environ, HOME=VAR, USERPROFILE=VAR))
    out = ((p.stdout or "") + "\n" + (p.stderr or "")).strip()
    if out:
        log(out[-1500:])
    if p.returncode != 0:
        raise RuntimeError("BACKUP завершился с кодом %s" % p.returncode)
    os.makedirs(nas_dir, exist_ok=True)
    dest = os.path.join(nas_dir, filename)
    unc = (task.get("smb_share") or "").strip()
    if unc:
        smb_get(unc, task.get("smb_user") or "", task.get("smb_pass") or "", filename, dest)
        log("Файл скопирован на NAS: %s" % dest)
    else:
        log("SMB-шара не задана. Файл остался на SQL Server: %s" % remote)
    rotate(task, nas_dir, db)
def rotate(task, nas_dir, db):
    if task.get("rot_on") is False or not os.path.isdir(nas_dir):
        return
    prefix = re.sub(r"[^A-Za-z0-9_.-]", "_", db) + "_"
    files = []
    for name in os.listdir(nas_dir):
        path = os.path.join(nas_dir, name)
        if name.startswith(prefix) and name.endswith(".bak") and os.path.isfile(path):
            files.append(path)
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    mode = task.get("rot_mode")
    if not mode and "keep" not in task:
        days = int(task.get("ret") or 0)
        if days <= 0:
            return
        limit = datetime.datetime.now().timestamp() - days * 86400
        for path in files:
            if os.path.getmtime(path) < limit:
                os.remove(path)
                log("Удалён старый файл: %s" % os.path.basename(path))
        return
    if mode == "smart":
        keep = set(smart_keep(files))
    else:
        n = int(task.get("keep") or 30)
        if n <= 0:
            return
        keep = set(files[:n])
    for path in files:
        if path not in keep:
            os.remove(path)
            log("Ротация: удалён %s" % os.path.basename(path))

def smart_keep(files):
    now = datetime.datetime.now()
    chosen = []
    seen_day = set()
    seen_week = set()
    for path in files:
        moment = datetime.datetime.fromtimestamp(os.path.getmtime(path))
        age = now - moment
        if age.total_seconds() <= 86400:
            chosen.append(path)
            continue
        if age.days <= 31:
            key = moment.strftime("%Y-%m-%d")
            if key not in seen_day:
                seen_day.add(key)
                chosen.append(path)
            continue
        year, week, _ = moment.isocalendar()
        key = (year, week)
        if key not in seen_week:
            seen_week.add(key)
            chosen.append(path)
    return chosen

def main():
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        with open(TASKS, encoding="utf-8") as f:
            tasks = json.load(f)
    except Exception as e:
        log("Нет задач: %s" % e)
        return 1
    rc = 0
    for task in tasks:
        if only and str(task.get("id")) != only:
            continue
        name = task.get("name") or task.get("host") or "task"
        log("=== Задача %s ===" % name)
        for db in task.get("dbs") or []:
            try:
                backup_one(task, db)
            except Exception as e:
                rc = 1
                log("Ошибка %s: %s" % (db, e))
    log("Готово")
    return rc

if __name__ == "__main__":
    sys.exit(main())
