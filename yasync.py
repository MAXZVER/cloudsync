#!/usr/bin/env python3
"""Движок синхронизации Яндекс.Диска. Без зависимостей — только стандартная библиотека.

Саму синхронизацию делает rclone bisync: он находит изменения с обеих сторон,
удаления и конфликты. Наша задача — конфигурация, запуск в нужный момент и
разбор конфликтов.

Важно для батареи: никаких циклов опроса. Скрипт запускается, делает работу и
завершается. Решение «когда запускать» принимает приложение — по событиям ядра.

Команды:
  init                           создать корень и конфиг
  ls [путь] [--json]             показать папки на Диске
  add <папка>                    взять папку под синхронизацию (первый раз — resync)
  rm <папка>                     снять с синхронизации (локальные файлы остаются)
  sync [папка|--all] [--force]   синхронизировать (--force — подтвердить массовые изменения)
  status                         что настроено и когда синхронизировалось
  state                          то же одной строкой JSON — для приложения
  conflicts                      список конфликтов
  resolve [имя|номер]            открыть слияние конфликта в браузере
"""
import json
import os
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
RCLONE = os.path.join(HOME, "bin", "rclone")
SUPPORT = os.path.join(HOME, "Library", "Application Support", "YandexSync")
CONFIG = os.path.join(SUPPORT, "config.json")
WORKDIR = os.path.join(SUPPORT, "bisync")
LOGDIR = os.path.join(HOME, "Library", "Logs")
LOG = os.path.join(LOGDIR, "yandex-sync.log")
DEFAULT_ROOT = os.path.join(HOME, "YandexDisk")
REMOTE = "yandex:"

# Суффиксы проигравших при конфликте. С --conflict-resolve none rclone оставляет
# ОБЕ версии, переименовав их — это и есть вход для нашего диффа.
CONFLICT_LOCAL = "local"
CONFLICT_REMOTE = "remote"

# Снимок содержимого на момент последней удачной синхронизации — общий предок.
# Без него две разошедшиеся версии неразличимы: строка есть слева и нет справа —
# это либо «слева добавили», либо «справа удалили», и дифф эти случаи не делит.
# С предком они делятся точно, и автослияние становится честным.
BASEDIR = os.path.join(SUPPORT, "base")
BASE_MAX = 32 * 1024 * 1024   # большие файлы не копируем: их всё равно не сливают построчно
SKIP_SUFFIX = ("." + CONFLICT_LOCAL, "." + CONFLICT_REMOTE)


def log(msg):
    os.makedirs(LOGDIR, exist_ok=True)
    line = "[%s] %s" % (time.strftime("%F %T"), msg)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load():
    if not os.path.exists(CONFIG):
        return {"remote": REMOTE, "root": DEFAULT_ROOT, "folders": []}
    with open(CONFIG, encoding="utf-8") as f:
        return json.load(f)


def save(cfg):
    os.makedirs(SUPPORT, exist_ok=True)
    tmp = CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG)


def run(args, timeout=3600):
    """Запуск rclone. Возвращает (код, stdout, stderr)."""
    p = subprocess.run([RCLONE] + args, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def local_path(cfg, remote_folder):
    return os.path.join(cfg["root"], remote_folder.replace("/", os.sep))


def cmd_init(_):
    cfg = load()
    os.makedirs(cfg["root"], exist_ok=True)
    os.makedirs(WORKDIR, exist_ok=True)
    save(cfg)
    print("  корень:  %s" % cfg["root"])
    print("  конфиг:  %s" % CONFIG)
    print("  папок под синхронизацией: %d" % len(cfg["folders"]))


def cmd_ls(args):
    as_json = "--json" in args
    args = [a for a in args if a != "--json"]
    path = args[0].strip("/") if args else ""
    cfg = load()
    prefix = cfg["remote"] + path
    rc, out, err = run(["lsjson", "--dirs-only", prefix, "--timeout", "30s"], timeout=120)
    if rc != 0:
        msg = err.strip().splitlines()[-1] if err.strip() else "не удалось прочитать Диск"
        if as_json:
            print(json.dumps({"path": path, "error": msg}, ensure_ascii=False))
        else:
            print("  ошибка: %s" % msg)
        return 1
    taken = {f["remote"] for f in cfg["folders"]}
    items = []
    for d in sorted(json.loads(out), key=lambda x: x["Name"].lower()):
        full = (path + "/" + d["Name"]).lstrip("/")
        # Папка внутри уже синхронизируемой отдельно не нужна — она и так придёт.
        inside = next((t for t in taken if full.startswith(t + "/")), None)
        items.append({"name": d["Name"], "path": full,
                      "taken": full in taken, "inside": inside})
    if as_json:
        print(json.dumps({"path": path, "items": items}, ensure_ascii=False))
        return 0
    for it in items:
        mark = "[+]" if it["taken"] else ("[.]" if it["inside"] else "[ ]")
        print("  %s %s" % (mark, it["path"]))
    return 0


# Bisync сам прерывается, если изменилось подозрительно много файлов или все
# сразу. Это правильная страховка, и отключать её насовсем нельзя — мы
# спрашиваем подтверждение и только тогда добавляем --force.
SAFETY_MARKERS = ("all files were changed", "max-delete", "Safety abort")


def is_safety_abort(text):
    return any(m.lower() in text.lower() for m in SAFETY_MARKERS)


def bisync_args(cfg, folder, resync=False, force=False):
    lp = local_path(cfg, folder["remote"])
    rp = cfg["remote"] + folder["remote"]
    a = [
        "bisync", lp, rp,
        "--workdir", WORKDIR,
        "--conflict-resolve", "none",      # ничего не выбираем за пользователя
        "--conflict-loser", "pathname",    # обе версии остаются на диске
        "--conflict-suffix", "%s,%s" % (CONFLICT_LOCAL, CONFLICT_REMOTE),
        "--resilient",                     # не требовать resync после мелких сбоев
        "--recover",
        "--timeout", "60s",
        "--transfers", "4",
        "--checkers", "8",
        "--yandex-upload-wait", "2s",
        "--log-level", "INFO",
    ]
    if resync:
        a += ["--resync", "--resync-mode", "newer"]
    if force:
        a += ["--force"]
    return a


def cmd_add(args):
    if not args:
        print("  укажите папку, например: add Документы")
        return 1
    folder = args[0].strip("/")
    cfg = load()
    if any(f["remote"] == folder for f in cfg["folders"]):
        print("  уже синхронизируется")
        return 0
    entry = {"remote": folder}
    lp = local_path(cfg, folder)
    os.makedirs(lp, exist_ok=True)
    os.makedirs(WORKDIR, exist_ok=True)
    cfg["folders"].append(entry)
    save(cfg)
    print("  добавлена: %s -> %s" % (folder, lp))
    print("  первая синхронизация (resync, может занять время)...")
    return do_sync(cfg, entry, resync=True)


def cmd_rm(args):
    if not args:
        return 1
    folder = args[0].strip("/")
    cfg = load()
    before = len(cfg["folders"])
    cfg["folders"] = [f for f in cfg["folders"] if f["remote"] != folder]
    save(cfg)
    print("  снята с синхронизации: %s" % folder if len(cfg["folders"]) < before
          else "  такая папка не синхронизируется")
    print("  локальные файлы оставлены на месте")
    return 0


# ------------------------------------------------------------- общий предок
def base_dir(folder):
    return os.path.join(BASEDIR, folder["remote"].replace("/", "%2F"))


def base_manifest(folder):
    try:
        with open(base_dir(folder) + ".json", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def snapshot(cfg, folder):
    """Сохраняет содержимое папки как общего предка для следующего конфликта.

    Инкрементально: файлы сверяются с манифестом по размеру и времени, поэтому
    обычный запуск копирует только изменившееся. Копии сжаты gzip.
    """
    import gzip
    import shutil
    root = local_path(cfg, folder["remote"])
    dst_root = base_dir(folder)
    old = base_manifest(folder)
    new = {}
    keep = set()
    copied = 0
    for dirpath, _, names in os.walk(root):
        for n in names:
            if n.endswith(SKIP_SUFFIX) or n.startswith("."):
                # Файл в конфликте: самого его нет, есть две версии. Именно для
                # него старый снимок и нужен — не трогаем его.
                if n.endswith("." + CONFLICT_LOCAL):
                    keep.add(os.path.relpath(
                        os.path.join(dirpath, n[: -(len(CONFLICT_LOCAL) + 1)]), root))
                continue
            src = os.path.join(dirpath, n)
            try:
                st = os.stat(src)
            except OSError:
                continue
            if st.st_size > BASE_MAX:
                continue
            rel = os.path.relpath(src, root)
            new[rel] = [st.st_size, int(st.st_mtime)]
            if old.get(rel) == new[rel]:
                continue
            dst = os.path.join(dst_root, rel + ".gz")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            try:
                with open(src, "rb") as a, gzip.open(dst, "wb") as b:
                    shutil.copyfileobj(a, b)
                copied += 1
            except OSError:
                new.pop(rel, None)
    for rel in old:
        if rel in keep:
            new[rel] = old[rel]
        elif rel not in new:
            try:
                os.remove(os.path.join(dst_root, rel + ".gz"))
            except OSError:
                pass
    os.makedirs(BASEDIR, exist_ok=True)
    tmp = dst_root + ".json.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(new, f, ensure_ascii=False)
    os.replace(tmp, dst_root + ".json")
    if copied:
        log("снимок предка: %s, обновлено файлов %d" % (folder["remote"], copied))


def base_bytes(cfg, conflict):
    """Содержимое файла на момент последней синхронизации, если оно есть."""
    import gzip
    f = next((x for x in cfg["folders"] if x["remote"] == conflict["folder"]), None)
    if not f:
        return None
    try:
        with gzip.open(os.path.join(base_dir(f), conflict["base"] + ".gz"), "rb") as fh:
            return fh.read()
    except OSError:
        return None


# ------------------------------------------------------------ синхронизация
def do_sync(cfg, folder, resync=False, force=False):
    lp = local_path(cfg, folder["remote"])
    os.makedirs(lp, exist_ok=True)
    started = time.time()
    log("синхронизация: %s%s" % (folder["remote"], " (resync)" if resync else ""))
    try:
        rc, out, err = run(bisync_args(cfg, folder, resync, force))
    except subprocess.TimeoutExpired:
        folder["lastResult"] = "timeout"
        save(cfg)
        log("превышено время: %s" % folder["remote"])
        print("  %s: превышено время" % folder["remote"])
        return 1

    combined = (err or "") + (out or "")
    tail = combined.strip().splitlines()

    # Страховка сработала: не обходим её молча, а сообщаем наружу.
    if rc != 0 and not force and is_safety_abort(combined):
        reason = next((l for l in tail if is_safety_abort(l)), "слишком много изменений")
        folder["lastResult"] = "needs-confirm"
        folder["lastReason"] = reason.split("NOTICE:")[-1].strip()
        save(cfg)
        log("страховка остановила %s (%s)" % (folder["remote"], folder["lastReason"]))
        print("  %s: ОСТАНОВЛЕНО СТРАХОВКОЙ" % folder["remote"])
        print("    %s" % folder["lastReason"])
        print("    это защита от массовой порчи. Если изменения ожидаемы:")
        print("    yasync.py sync %s --force" % folder["remote"])
        return 2
    # Bisync требует --resync, если состояние потеряно. Делаем это сами, один раз.
    if rc != 0 and any("--resync" in l for l in tail[-15:]) and not resync:
        log("нет базового состояния для %s, повторяю с resync" % folder["remote"])
        print("  %s: нет базового состояния, делаю первичную сверку" % folder["remote"])
        return do_sync(cfg, folder, resync=True)

    folder["lastSync"] = time.strftime("%F %T")
    folder["lastResult"] = "ok" if rc == 0 else "error"
    folder.pop("lastReason", None)
    save(cfg)
    secs = time.time() - started
    if rc == 0:
        # Стороны свели — значит текущее состояние и есть общий предок для
        # следующего расхождения. Самое время его запомнить.
        try:
            snapshot(cfg, folder)
        except OSError as e:
            log("снимок предка не сделан (%s): %s" % (folder["remote"], e))
        n = len(find_conflicts(cfg, folder))
        log("готово: %s за %.0f с, конфликтов %d" % (folder["remote"], secs, n))
        print("  %s: готово за %.0f с%s" % (folder["remote"], secs,
              (", конфликтов: %d" % n) if n else ""))
    else:
        for l in tail[-5:]:
            log("  %s" % l)
        log("ошибка: %s" % folder["remote"])
        print("  %s: ОШИБКА" % folder["remote"])
        for l in tail[-3:]:
            print("    %s" % l)
    return 0 if rc == 0 else 1


def cmd_sync(args):
    force = "--force" in args
    args = [a for a in args if a != "--force"]
    cfg = load()
    if not cfg["folders"]:
        print("  ни одна папка не выбрана")
        return 0
    targets = cfg["folders"]
    if args and args[0] != "--all":
        targets = [f for f in cfg["folders"] if f["remote"] == args[0].strip("/")]
        if not targets:
            print("  такая папка не синхронизируется")
            return 1
    worst = 0
    for f in targets:
        worst = max(worst, do_sync(cfg, f, force=force))
    return worst


def find_conflicts(cfg, folder=None):
    """Файлы, которые rclone оставил в двух версиях."""
    folders = [folder] if folder else cfg["folders"]
    out = []
    for f in folders:
        root = local_path(cfg, f["remote"])
        for dirpath, _, names in os.walk(root):
            for n in names:
                if n.endswith("." + CONFLICT_LOCAL):
                    base = n[: -(len(CONFLICT_LOCAL) + 1)]
                    other = os.path.join(dirpath, base + "." + CONFLICT_REMOTE)
                    if os.path.exists(other):
                        out.append({
                            "folder": f["remote"],
                            "base": os.path.relpath(os.path.join(dirpath, base), root),
                            "local": os.path.join(dirpath, n),
                            "remote": other,
                        })
    return out


def cmd_conflicts(_):
    cfg = load()
    c = find_conflicts(cfg)
    if not c:
        print("  конфликтов нет")
        return 0
    print("  конфликтов: %d" % len(c))
    for x in c:
        print("    %s / %s" % (x["folder"], x["base"]))
        print("      этот Mac:      %s байт" % os.path.getsize(x["local"]))
        print("      Яндекс.Диск:   %s байт" % os.path.getsize(x["remote"]))
    return 0


def folder_size(path):
    size = files = 0
    for dp, _, ns in os.walk(path):
        for n in ns:
            try:
                size += os.path.getsize(os.path.join(dp, n))
                files += 1
            except OSError:
                pass
    return files, size


def cmd_status(_):
    cfg = load()
    print("  корень: %s" % cfg["root"])
    if not cfg["folders"]:
        print("  папок не выбрано")
        return 0
    for f in cfg["folders"]:
        files, size = folder_size(local_path(cfg, f["remote"]))
        print("  %-28s %s  %s  (%d файл., %.1f МБ)" % (
            f["remote"], f.get("lastSync") or "ни разу",
            f.get("lastResult") or "-", files, size / 1048576.0))
    print("  конфликтов: %d" % len(find_conflicts(cfg)))
    return 0


def cmd_state(_):
    """Всё состояние одной строкой JSON — это читает приложение в строке меню."""
    cfg = load()
    conflicts = find_conflicts(cfg)
    folders = []
    for f in cfg["folders"]:
        lp = local_path(cfg, f["remote"])
        files, size = folder_size(lp)
        folders.append({
            "remote": f["remote"], "local": lp,
            "lastSync": f.get("lastSync"), "lastResult": f.get("lastResult"),
            "lastReason": f.get("lastReason"),
            "files": files, "bytes": size,
            "conflicts": sum(1 for c in conflicts if c["folder"] == f["remote"]),
        })
    print(json.dumps({"root": cfg["root"], "log": LOG, "folders": folders,
                      "conflicts": [{"folder": c["folder"], "file": c["base"]}
                                    for c in conflicts]}, ensure_ascii=False))
    return 0


COMMANDS = {
    "init": cmd_init, "ls": cmd_ls, "add": cmd_add, "rm": cmd_rm,
    "sync": cmd_sync, "status": cmd_status, "state": cmd_state,
    "conflicts": cmd_conflicts,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        return 2
    return COMMANDS[sys.argv[1]](sys.argv[2:]) or 0


# ---------------------------------------------------------------- разрешение
def cmd_resolve(args):
    """Открывает интерфейс слияния для одного конфликта."""
    import http.server
    import json as _json
    import socket
    import threading
    import webbrowser
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import yadiff

    cfg = load()
    conflicts = find_conflicts(cfg)
    if not conflicts:
        print("  конфликтов нет")
        return 0
    idx = 0
    if args:
        want = args[0]
        found = [i for i, c in enumerate(conflicts) if c["base"] == want or str(i) == want]
        if not found:
            print("  нет такого конфликта; посмотрите: yasync.py conflicts")
            return 1
        idx = found[0]
    c = conflicts[idx]

    # Слева — то, что на сервере, справа — то, что на этой машине. Так же,
    # как в IDE: чужое слева, своё справа.
    left, mergeable_l, reader_l = yadiff.to_lines(c["remote"])
    right, mergeable_r, reader_r = yadiff.to_lines(c["local"])
    mergeable = bool(mergeable_l and mergeable_r)
    reader = reader_l if reader_l == reader_r else "%s / %s" % (reader_l, reader_r)
    if left is None or right is None:
        # Сравнивать нечем — только выбор версии целиком.
        left = left or ["(содержимое не прочитать: %s)" % reader_l]
        right = right or ["(содержимое не прочитать: %s)" % reader_r]
        mergeable = False

    # Общий предок. Читаем его тем же читателем, что и стороны, иначе
    # сопоставлять будет нечего.
    base_lines = None
    if mergeable:
        raw = base_bytes(cfg, c)
        if raw is not None:
            os.makedirs(WORKDIR, exist_ok=True)
            tmp = os.path.join(WORKDIR, "предок-временный" + os.path.splitext(c["base"])[1])
            try:
                with open(tmp, "wb") as f:
                    f.write(raw)
                base_lines, mb, _ = yadiff.to_lines(tmp)
                if not mb:
                    base_lines = None
            except OSError:
                base_lines = None
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    blocks = yadiff.build_blocks(left, right, base_lines)
    body = yadiff.page(c, blocks, mergeable, reader,
                       three_way=base_lines is not None).encode("utf-8")
    result = {"done": False}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            data = _json.loads(self.rfile.read(n).decode("utf-8"))
            msg = apply_resolution(cfg, c, blocks, data, mergeable)
            out = msg.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            result["done"] = True

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = http.server.HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/" % port
    print("  конфликт: %s / %s" % (c["folder"], c["base"]), flush=True)
    print("  слияние открыто: %s" % url, flush=True)
    print("  окно можно закрыть после применения", flush=True)
    webbrowser.open(url)
    try:
        while not result["done"]:
            time.sleep(0.3)
        time.sleep(0.5)
    except KeyboardInterrupt:
        print("  отменено")
    srv.shutdown()
    return 0


def apply_resolution(cfg, c, blocks, data, mergeable):
    """Записывает результат выбора и запускает синхронизацию."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import yadiff
    if data.get("action") != "apply":
        return "Отменено, файлы не тронуты."

    # picks — стороны, вошедшие в результат, В ПОРЯДКЕ НАЖАТИЯ. Порядок задаёт
    # порядок строк, поэтому отдельных кнопок «оба» не нужно.
    picks = data.get("picks") or []
    for b, p in zip(blocks, picks):
        b["picks"] = list(p)

    base = os.path.join(os.path.dirname(c["local"]),
                        os.path.basename(c["local"])[: -len("." + CONFLICT_LOCAL)])
    if mergeable:
        # Если пользователь правил результат руками — его текст главнее выбора блоков.
        text = data.get("text")
        if text is None:
            merged = yadiff.render_merged(blocks)
            text = "\n".join(merged) + ("\n" if merged else "")
            what = "слито %d строк" % len(merged)
        else:
            if text and not text.endswith("\n"):
                text += "\n"
            what = "сохранено %d строк" % text.count("\n")
        with open(base, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        # Офисный или двоичный файл: построчно не собрать, берём сторону целиком.
        # Слева — сервер, справа — эта машина.
        chosen = [s for b in blocks if b["tag"] == "diff" for s in b.get("picks", [])]
        side = "left" if chosen and all(s == "left" for s in chosen) else "right"
        src = c["remote"] if side == "left" else c["local"]
        with open(src, "rb") as a, open(base, "wb") as b2:
            b2.write(a.read())
        what = "взята версия: %s" % ("с Яндекс.Диска" if side == "left" else "с этого Mac")

    os.remove(c["local"])
    os.remove(c["remote"])
    log("конфликт разрешён: %s/%s — %s" % (c["folder"], c["base"], what))

    folder = next((f for f in cfg["folders"] if f["remote"] == c["folder"]), None)
    if folder:
        do_sync(cfg, folder)
    left = len(find_conflicts(cfg))
    return "Готово: %s. Синхронизировано.%s" % (
        what, ("  Осталось конфликтов: %d" % left) if left else "  Конфликтов больше нет.")


COMMANDS["resolve"] = cmd_resolve

if __name__ == "__main__":
    sys.exit(main())
