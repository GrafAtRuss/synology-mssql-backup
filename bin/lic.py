#!/usr/bin/env python3
"""Лицензия через com_applicenses на ип-ермаков.рф. Серийный номер берётся из DSM."""
import json, os, ssl, time, urllib.error, urllib.request

VAR = "/var/packages/MSSQLBackup/var"
STATE = os.path.join(VAR, "license.json")
APP = "mssqlbackup"
CHECK_URL = "https://ип-ермаков.рф/index.php?option=com_applicenses&task=activate.check&format=json"
INTERVAL = 3600
DENY = {"invalid_key", "revoked", "expired", "app_required", "app_mismatch", "serial_mismatch", "bad_request"}

MESSAGES = {
    "activated": "Лицензия активирована",
    "valid": "Лицензия подтверждена",
    "invalid_key": "Ключ не найден",
    "revoked": "Ключ отозван",
    "expired": "Срок лицензии истёк",
    "app_required": "У ключа в Лицензиях приложений не выбрано приложение",
    "app_mismatch": "Ключ выдан другому приложению. Нужен ключ для mssqlbackup",
    "serial_mismatch": "Ключ уже привязан к другому серийному номеру",
    "bad_request": "Нужны лицензионный ключ и серийный номер DSM",
    "offline": "Сайт лицензий недоступен. Ранее активированная лицензия сохранена",
    "none": "Лицензия не активирована. Можно создать одно задание",
}


def dsm_serial():
    for path in ("/etc/synoinfo.conf", "/etc.defaults/synoinfo.conf"):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith("serial="):
                        value = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if value:
                            return value
        except OSError:
            continue
    try:
        import subprocess
        p = subprocess.run(
            ["/usr/syno/bin/synogetkeyvalue", "/etc/synoinfo.conf", "serial"],
            capture_output=True, text=True, timeout=5)
        value = (p.stdout or "").strip()
        if p.returncode == 0 and value:
            return value
    except Exception:
        pass
    return ""


def _read():
    try:
        with open(STATE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write(data):
    os.makedirs(VAR, exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE)


def _public(data, serial):
    activated = bool(data.get("activated"))
    code = data.get("code") or ("valid" if activated else "none")
    return {
        "activated": activated,
        "limited": not activated,
        "serial": serial or data.get("serial") or "",
        "key_set": bool(data.get("key")),
        "code": code,
        "issued_to": data.get("issued_to") or "",
        "expires_at": data.get("expires_at") or "",
        "message": data.get("message") or MESSAGES.get(code) or MESSAGES["none"],
        "checked_at": int(data.get("checked_at") or 0),
        "app": APP,
    }


def _remote(key, serial):
    body = json.dumps({
        "key": key,
        "serial": serial,
        "app": APP,
        "client": "Synology Microsoft SQL Backup & Restore",
    }).encode("utf-8")
    req = urllib.request.Request(
        CHECK_URL, data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
        raw = resp.read().decode("utf-8", "replace")
        status = resp.status
    parsed = json.loads(raw or "{}")
    if not isinstance(parsed, dict):
        raise ValueError("пустой ответ")
    parsed["_http"] = status
    return parsed


def refresh(force=False, key=None):
    serial = dsm_serial()
    data = _read()
    if key is not None:
        data["key"] = (key or "").strip()
    now = int(time.time())
    fresh = now - int(data.get("checked_at") or 0) < INTERVAL
    if not force and fresh:
        return _public(data, serial)
    stored_key = (data.get("key") or "").strip()
    if not stored_key or not serial:
        data.update({
            "serial": serial,
            "activated": False,
            "code": "bad_request" if stored_key and not serial else "none",
            "message": MESSAGES["bad_request"] if stored_key and not serial else MESSAGES["none"],
            "checked_at": now,
        })
        _write(data)
        return _public(data, serial)
    try:
        result = _remote(stored_key, serial)
    except Exception:
        data["serial"] = serial
        data["checked_at"] = now
        if data.get("activated"):
            data["code"] = "offline"
            data["message"] = MESSAGES["offline"]
        else:
            data["code"] = "offline"
            data["message"] = "Сайт лицензий недоступен. Без активации можно создать одно задание"
        _write(data)
        return _public(data, serial)
    code = str(result.get("code") or "")
    ok = bool(result.get("ok")) and code in ("activated", "valid")
    data["serial"] = serial
    data["checked_at"] = now
    if ok:
        data["activated"] = True
        data["code"] = code
        data["issued_to"] = result.get("issued_to") or (result.get("license") or {}).get("issued_to") or ""
        data["expires_at"] = result.get("expires_at") or (result.get("license") or {}).get("expires_at") or ""
        data["last_ok_at"] = now
        data["message"] = MESSAGES.get(code, "Лицензия активирована")
    elif code in DENY:
        data["activated"] = False
        data["code"] = code
        data["message"] = MESSAGES.get(code, "Лицензия отклонена")
    elif data.get("activated"):
        data["code"] = "offline"
        data["message"] = MESSAGES["offline"]
    else:
        data["activated"] = False
        data["code"] = code or "error"
        data["message"] = "Не удалось подтвердить лицензию"
    _write(data)
    return _public(data, serial)


def allows_task_list(new_tasks, old_tasks):
    state = refresh(False)
    if state.get("activated"):
        return True, ""
    old_ids = {str(t.get("id")) for t in old_tasks}
    new_ids = {str(t.get("id")) for t in new_tasks}
    added = new_ids - old_ids
    if added and len(new_tasks) > 1:
        return False, "Без активации можно создать только одно задание. Откройте «Лицензия» и введите ключ."
    if not added and len(new_tasks) <= max(len(old_tasks), 1):
        return True, ""
    if len(new_tasks) > 1 and len(new_tasks) > len(old_tasks):
        return False, "Без активации можно создать только одно задание. Откройте «Лицензия» и введите ключ."
    return True, ""
