#!/usr/bin/env python3
"""
Отчёт о резервной копии для страницы /backup (11.10, просьба Alex: «полная информация о бэкапах, с графиками»).
Сайт работает в контейнере и диска с копиями не видит — поэтому backup_db.sh после каждой копии вызывает этот скрипт
(Python сервера, только стандартная библиотека), и он пишет в data/:
  backup_history.jsonl  — по строке на каждый запуск: время, итог, длительность, размер, по каждой базе исходный и сжатый
                          размер и проверка копии (PRAGMA quick_check), место на диске копий;
  backup_inventory.json — что лежит на диске копий сейчас (папки, файлы, размеры), что в копию НЕ входит (с размерами),
                          правила хранения.
Запуск: backup_report.py <папка копии> <ok|fail> <секунд> "<сообщение>" (вызывает backup_db.sh; файл .files.tsv в папке копии).
Без аргументов — только опись (backup_inventory.json), например после ручной уборки.
"""
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

LAB = Path(__file__).resolve().parent
DATA = LAB / "data"
DEST = Path("/mnt/personal_data/Backups/Projects/Weather Polymarket")
HISTORY = DATA / "backup_history.jsonl"
INVENTORY = DATA / "backup_inventory.json"
# что в копию не входит и почему (размеры считаются при каждом запуске)
# (11.10, решение Alex: data/books и скрипты/итоги исследований теперь в копии — books.sqlite3.zst, research.tar.zst)
NOT_IN = [("data/research/research.sqlite3", "копия базы для исследований — пересоздаётся из базы"),
          ("data/research/bench/c.duckdb", "проба DuckDB для скорости — пересоздаётся"),
          ("data/ml", "обученные модели — переобучаются каждую ночь (05:20)"),
          ("data/logs", "журналы скриптов"),
          ("app", "код — в GitHub (github.com/AllexisO/weather-polymarket)")]


def du(p):
    if "*" in str(p):   # «папка/*.py, *.log» — сумма файлов по маскам в этой папке
        head, _, masks = str(p).rpartition("/")
        base = Path(head)
        while "*" in base.name:
            base = base.parent
        masks = [m.strip().split("/")[-1] for m in (str(p)[len(str(base)) + 1:]).split(",")]
        return sum(f.stat().st_size for m in masks for f in base.glob(m) if f.is_file())
    p = Path(p)
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.exists() else 0


def inventory():
    copies = []
    for d in sorted(DEST.glob("20??-??-??")):
        files = sorted(({"name": f.name, "bytes": f.stat().st_size} for f in d.iterdir() if f.is_file() and not f.name.startswith(".")),
                       key=lambda x: -x["bytes"])
        copies.append({"date": d.name, "bytes": sum(f["bytes"] for f in files), "files": files,
                       "weekday": datetime.strptime(d.name, "%Y-%m-%d").isoweekday()})
    total, used, free = shutil.disk_usage(DEST if DEST.exists() else "/")
    src = sorted(({"name": f.name, "bytes": f.stat().st_size} for f in (DATA / "db").glob("*.sqlite3")), key=lambda x: -x["bytes"])
    inv = {"at": datetime.now().astimezone().isoformat(timespec="seconds"), "dest": str(DEST), "copies": copies,
           "disk": {"total": total, "used": used, "free": free},
           "sources": src, "not_in": [{"path": p, "why": w, "bytes": du(LAB / p)} for p, w in NOT_IN],
           "keep": {"days": 7, "weeks": 4, "time": "03:10"}}
    tmp = INVENTORY.with_suffix(".tmp")
    tmp.write_text(json.dumps(inv, ensure_ascii=False))
    tmp.replace(INVENTORY)
    return inv


def main():
    inv = inventory()
    if len(sys.argv) < 5:
        return
    out, status, took, msg = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]), sys.argv[4]
    files = []
    tsv = out / ".files.tsv"
    if tsv.exists():
        for line in tsv.read_text().splitlines():
            name, src, zst, qc = (line.split("\t") + ["", "", "", ""])[:4]
            files.append({"name": name, "src_bytes": int(src or 0), "zst_bytes": int(zst or 0), "check": qc})
        tsv.unlink()
    row = {"at": datetime.now().astimezone().isoformat(timespec="seconds"), "status": status, "took_s": took, "msg": msg,
           "dir": out.name, "bytes": sum(f["bytes"] for f in next((c["files"] for c in inv["copies"] if c["date"] == out.name), [])),
           "files": files, "disk_free": inv["disk"]["free"], "disk_total": inv["disk"]["total"]}
    with HISTORY.open("a") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.chmod(HISTORY, 0o664)


if __name__ == "__main__":
    main()
