#!/usr/bin/env python3
import json, os, sys, datetime, subprocess, re, time, threading

VAR = "/var/packages/MSSQLBackup/var"
TARGET = "/var/packages/MSSQLBackup/target"
TASKS = os.path.join(VAR, "tasks.json")
LOG = os.path.join(VAR, "backup.log")
def sqlcmd_bin():
    arch = os.uname().machine
    name = "sqlcmd-linux-arm64" if arch in ("aarch64", "arm64") else "sqlcmd-linux-amd64"
    path = os.path.join(TARGET, "bin", name)
    return path if os.path.isfile(path) else ""


STATUS = os.path.join(VAR, "status.json")
_status_lock = threading.Lock()

def set_status(**kw):
    data = {
        "running": bool(kw.get("running")),
        "task_id": str(kw.get("task_id") or ""),
        "db": kw.get("db") or "",
        "percent": int(kw.get("percent") or 0),
        "phase": kw.get("phase") or "",
        "message": kw.get("message") or "",
        "updated": time.time(),
    }
    try:
        os.makedirs(VAR, exist_ok=True)
        with _status_lock:
            tmp = STATUS + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, STATUS)
    except Exception:
        pass

def mark(task, db, index, total, frac, phase, message):
    total = max(int(total or 1), 1)
    overall = int(round(((index + max(0.0, min(1.0, frac))) / total) * 100))
    set_status(running=True, task_id=task.get("id"), db=db, percent=overall, phase=phase, message=message)

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

def smb_bin():
    for cand in ("/usr/bin/smbclient", "/bin/smbclient", "smbclient"):
        if cand == "smbclient" or os.path.isfile(cand):
            return cand
    return ""

def smb_conf():
    os.makedirs(VAR, exist_ok=True)
    path = os.path.join(VAR, "smb.conf")
    content = "[global]\nclient min protocol = SMB2\nclient max protocol = SMB3\n"
    try:
        current = open(path, encoding="utf-8").read()
    except Exception:
        current = ""
    if current != content:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    return path

def smb_noise(text):
    kept = []
    for line in (text or "").splitlines():
        if "ldap ssl ads" in line or "smb.reserved.conf" in line or "lpcfg_do_global_parameter" in line:
            continue
        kept.append(line)
    return "\n".join(kept).strip()

def smb_run(host, share, user, password, command):
    smb = smb_bin()
    if not smb:
        raise RuntimeError("smbclient не найден в DSM")
    auth = "%s%%%s" % (user, password) if user else "%"
    env = dict(os.environ, HOME=VAR, USERPROFILE=VAR)
    cmd = [smb, "//%s/%s" % (host, share), "-s", smb_conf(), "-U", auth, "-c", command]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=3600, env=env)
    raw = ((p.stdout or "") + "\n" + (p.stderr or "")).strip()
    return p.returncode, smb_noise(raw), raw

def smb_get(unc, user, password, filename, dest):
    host, share, subs = parse_unc(unc)
    remote = "/".join(subs + [filename]) if subs else filename
    log("smbclient get //%s/%s/%s -> %s" % (host, share, remote, dest))
    rc, clean, raw = smb_run(host, share, user, password, 'get "%s" "%s"' % (remote, dest))
    if os.path.isfile(dest) and os.path.getsize(dest) > 0 and "NT_STATUS_" not in raw:
        return
    raise RuntimeError(clean or "smbclient не скопировал файл")

def backup_one(task, db, index=0, total=1):
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
    folder = sql_dir.rstrip("\\/").replace("'", "''")
    mark(task, db, index, total, 0.02, "prepare", "Подготовка %s" % db)
    code, out = sql_exec(host, port, user, password, "EXEC master.dbo.xp_create_subdir N'%s'" % folder, timeout=60)
    if code != 0:
        log("Папку %s заранее создать не удалось: %s" % (sql_dir, (out or "")[-400:]))
    else:
        log("Папка на SQL Server готова: %s" % sql_dir.rstrip("\\/"))
    query_plain = "BACKUP DATABASE [%s] TO DISK = N'%s' WITH INIT, STATS = 10" % (ident, remote.replace("'", "''"))
    query_comp = "BACKUP DATABASE [%s] TO DISK = N'%s' WITH INIT, COMPRESSION, STATS = 10" % (ident, remote.replace("'", "''"))
    sqlcmd = sqlcmd_bin()
    if not sqlcmd:
        raise RuntimeError("sqlcmd не найден в пакете")
    log("BACKUP %s -> %s" % (db, remote))
    mark(task, db, index, total, 0.05, "backup", "Резервное копирование %s" % db)
    pct_rx = re.compile(r"(\d+)\s*(?:проц|percent)", re.I)

    def on_line(line):
        found = pct_rx.search(line or "")
        if not found:
            return
        frac = 0.05 + (int(found.group(1)) / 100.0) * 0.75
        mark(task, db, index, total, frac, "backup", "%s — %s%%" % (db, found.group(1)))

    cmd = [sqlcmd, "-S", "%s,%s" % (host, port), "-U", user, "-P", password, "-C", "-l", "30", "-b", "-Q"]
    rc, out = run_logged(cmd + [query_comp], 7200, on_line)
    if rc != 0 and ("1844" in (out or "") or "COMPRESSION" in (out or "").upper() or "сжат" in (out or "").lower()):
        log("Редакция SQL не умеет COMPRESSION, повтор без сжатия на сервере")
        rc, out = run_logged(cmd + [query_plain], 7200, on_line)
    if rc != 0:
        text = out or ""
        if "3201" in text or "операционной системы 3" in text.lower() or "cannot find the path" in text.lower():
            raise RuntimeError("На сервере %s нет пути %s. Создайте эту папку на локальном диске SQL Server и дайте службе SQL право записи, либо укажите уже существующий каталог." % (host, sql_dir))
        raise RuntimeError("BACKUP завершился с кодом %s" % rc)
    os.makedirs(nas_dir, exist_ok=True)
    dest = os.path.join(nas_dir, filename)
    unc = (task.get("smb_share") or "").strip()
    if unc:
        mark(task, db, index, total, 0.86, "copy", "Копирование %s на NAS" % db)
        smb_get(unc, task.get("smb_user") or "", task.get("smb_pass") or "", filename, dest)
        log("Файл скопирован на NAS: %s" % dest)
        mark(task, db, index, total, 0.94, "archive", "Архивация %s" % db)
        archive_copy(dest)
    else:
        log("SMB-шара не задана. Файл остался на SQL Server: %s" % remote)
    rotate(task, nas_dir, db)
    mark(task, db, index, total, 1.0, "done", "База %s готова" % db)
def rotate(task, nas_dir, db):
    if task.get("rot_on") is False or not os.path.isdir(nas_dir):
        return
    prefix = re.sub(r"[^A-Za-z0-9_.-]", "_", db) + "_"
    files = []
    for name in os.listdir(nas_dir):
        path = os.path.join(nas_dir, name)
        if name.startswith(prefix) and (name.endswith(".bak") or name.endswith(".bak.gz")) and os.path.isfile(path):
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
        chosen = smart_keep(files)
        n = int(task.get("keep") or 256)
        if n > 0:
            chosen = sorted(chosen, key=lambda p: os.path.getmtime(p), reverse=True)[:n]
        keep = set(chosen)
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

def archive_copy(dest):
    gz = dest + ".gz"
    log("Архивация %s" % os.path.basename(dest))
    with open(gz, "wb") as out:
        p = subprocess.run(["gzip", "-c", dest], stdout=out, stderr=subprocess.PIPE, timeout=7200)
    if p.returncode != 0:
        if os.path.exists(gz):
            os.remove(gz)
        err = (p.stderr or b"").decode("utf-8", "replace")[:300]
        raise RuntimeError("не удалось заархивировать копию: %s" % err)
    os.remove(dest)
    log("Архив на NAS: %s" % os.path.basename(gz))

def unpack_archive(path):
    if not path.endswith(".gz"):
        return path, False
    raw = os.path.join(VAR, "restore-%s" % os.path.basename(path)[:-3])
    log("Распаковка %s" % os.path.basename(path))
    with open(raw, "wb") as out:
        p = subprocess.run(["gzip", "-dc", path], stdout=out, stderr=subprocess.PIPE, timeout=7200)
    if p.returncode != 0:
        err = (p.stderr or b"").decode("utf-8", "replace")[:300]
        raise RuntimeError("не удалось распаковать архив: %s" % err)
    return raw, True

def run_logged(cmd, timeout, on_line=None):
    env = dict(os.environ, HOME=VAR, USERPROFILE=VAR)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    chunks = []

    def reader():
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            chunks.append(line)
            log(line)
            if on_line:
                try:
                    on_line(line)
                except Exception:
                    pass

    threading.Thread(target=reader, daemon=True).start()
    started = time.time()
    deadline = started + timeout
    while True:
        try:
            rc = proc.wait(timeout=20)
            break
        except subprocess.TimeoutExpired:
            if time.time() > deadline:
                proc.kill()
                raise RuntimeError("превышено время ожидания, %d с" % timeout)
            log("идёт копирование, прошло %d с" % int(time.time() - started))
    time.sleep(0.3)
    return rc, "\n".join(chunks)

def smb_put(unc, user, password, local, filename):
    host, share, subs = parse_unc(unc)
    remote = "/".join(subs + [filename]) if subs else filename
    log("smbclient put %s -> //%s/%s/%s" % (os.path.basename(local), host, share, remote))
    rc, clean, raw = smb_run(host, share, user, password, 'put "%s" "%s"' % (local, remote))
    if rc != 0 or "NT_STATUS_" in raw:
        raise RuntimeError(clean or "smbclient не записал файл")

def sql_exec(host, port, user, password, query, timeout=7200):
    sqlcmd = sqlcmd_bin()
    if not sqlcmd:
        raise RuntimeError("sqlcmd не найден в пакете")
    cmd = [sqlcmd, "-S", "%s,%s" % (host, port), "-U", user, "-P", password,
           "-C", "-l", "30", "-b", "-s", "|", "-W", "-h", "-1", "-y", "0", "-Q", query]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=dict(os.environ, HOME=VAR, USERPROFILE=VAR))
    out = ((p.stdout or "") + "\n" + (p.stderr or "")).strip()
    return p.returncode, out

def parse_filelist(text):
    rows = []
    for line in (text or "").splitlines():
        parts = [x.strip() for x in line.split("|")]
        if len(parts) < 3:
            continue
        kind = parts[2].upper()
        if kind not in ("D", "L"):
            continue
        logical = parts[0]
        if not logical or logical.lower() == "logicalname":
            continue
        rows.append((logical, kind))
    return rows

def restore_job(job):
    bak = os.path.realpath(job.get("bak") or "")
    if not bak.startswith("/volume") or not os.path.isfile(bak):
        raise RuntimeError("файл копии недоступен")
    if not (bak.lower().endswith(".bak") or bak.lower().endswith(".bak.gz")):
        raise RuntimeError("файл копии недоступен")
    host = job.get("host") or ""
    port = str(job.get("port") or "1433")
    user = job.get("user") or ""
    password = job.get("pass") or ""
    dbname = job.get("dbname") or ""
    sql_dir = (job.get("sql_dir") or r"C:\SQLBackups").rstrip("\\/")
    ident = sql_ident(dbname)
    local, temporary = unpack_archive(bak)
    filename = os.path.basename(local)
    if not filename.lower().endswith(".bak"):
        filename += ".bak"
    log("=== Восстановление %s на %s ===" % (os.path.basename(bak), host))
    try:
        smb_put(job.get("smb_share") or "", job.get("smb_user") or "", job.get("smb_pass") or "", local, filename)
    finally:
        if temporary:
            try:
                os.remove(local)
            except OSError:
                pass
    remote = win_join(sql_dir, filename)
    code, out = sql_exec(host, port, user, password,
                         "RESTORE FILELISTONLY FROM DISK = N'%s'" % remote.replace("'", "''"), timeout=600)
    log(out[-1500:] if out else "FILELISTONLY без вывода")
    if code != 0:
        raise RuntimeError("FILELISTONLY завершился с кодом %s" % code)
    files = parse_filelist(out)
    if not files:
        raise RuntimeError("Не удалось прочитать состав копии")
    moves = []
    data_i = 0
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", dbname) or "db"
    for logical, kind in files:
        if kind == "L":
            ext = "_log.ldf"
        elif data_i == 0:
            ext = ".mdf"
            data_i += 1
        else:
            ext = "_%d.ndf" % data_i
            data_i += 1
        dest = sql_dir + "\\" + safe + ext
        moves.append("MOVE N'%s' TO N'%s'" % (logical.replace("'", "''"), dest.replace("'", "''")))
    disk = remote.replace("'", "''")
    batch = (
        "IF DB_ID(N'%s') IS NOT NULL ALTER DATABASE [%s] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; "
        "RESTORE DATABASE [%s] FROM DISK = N'%s' WITH %s, REPLACE, STATS = 10; "
        "IF DB_ID(N'%s') IS NOT NULL ALTER DATABASE [%s] SET MULTI_USER;"
    ) % (ident.replace("'", "''"), ident, ident, disk, ", ".join(moves), ident.replace("'", "''"), ident)
    log("RESTORE DATABASE [%s]" % ident)
    code, out = sql_exec(host, port, user, password, batch, timeout=7200)
    if out:
        log(out[-2000:])
    if code != 0:
        raise RuntimeError("RESTORE завершился с кодом %s" % code)
    log("Восстановление завершено: %s на %s" % (dbname, host))

def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--restore":
        try:
            with open(sys.argv[2], encoding="utf-8") as f:
                job = json.load(f)
            restore_job(job)
            return 0
        except Exception as e:
            log("Ошибка восстановления: %s" % e)
            return 1
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
        dbs = task.get("dbs") or []
        log("=== Задача %s ===" % name)
        set_status(running=True, task_id=task.get("id"), percent=0, phase="start", message="Запуск «%s»" % name)
        for i, db in enumerate(dbs):
            try:
                backup_one(task, db, i, len(dbs) or 1)
            except Exception as e:
                rc = 1
                log("Ошибка %s: %s" % (db, e))
    set_status(running=False, task_id=only, percent=0 if rc else 100, phase="error" if rc else "done", message="Завершено с ошибкой" if rc else "Готово")
    log("Готово")
    return rc

if __name__ == "__main__":
    sys.exit(main())
