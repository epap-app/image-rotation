"""
keystore_merge.py

Host-side helpers for the per-UID keystore2 backup/restore path.
Captures rows from /data/misc/keystore/persistent.sqlite (filtered to
keyentry.domain=APP for a given set of UIDs) into a JSON document, and
re-inserts them into a target persistent.sqlite with the namespace remapped
to live UIDs.

Tables handled (Android 11+ keystore2):
    keyentry       parent
    blobentry      child of keyentry (and parent of blobmetadata)
    blobmetadata   child of blobentry
    keymetadata    child of keyentry
    keyparameter   child of keyentry
    grant          child of keyentry

keyentry.id and grant.id are 64-bit UNIQUE values, not autoincrement; we
preserve them on insert and remap on collision. blobentry.id is also UNIQUE.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import random
import sqlite3
from pathlib import Path
from typing import Iterable

DOMAIN_APP = 0  # android.system.keystore2.Domain.APP

KEYSTORE_TABLES = (
    "keyentry",
    "blobentry",
    "blobmetadata",
    "keymetadata",
    "keyparameter",
    "grant",
)


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _enc(v):
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"__b64__": base64.b64encode(bytes(v)).decode("ascii")}
    return v


def _dec(v):
    if isinstance(v, dict) and "__b64__" in v:
        return base64.b64decode(v["__b64__"])
    return v


def _row_dict(row: sqlite3.Row, cols: list[str]) -> dict:
    return {c: _enc(row[c]) for c in cols}


def schema_fingerprint(sqlite_path: Path) -> str:
    """SHA-256 over the CREATE TABLE statements of the keystore tables, with
    whitespace normalized so trivial reformatting doesn't perturb the hash."""
    with sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True) as c:
        rows = c.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' "
            "AND name IN (?,?,?,?,?,?) ORDER BY name",
            KEYSTORE_TABLES,
        ).fetchall()
    h = hashlib.sha256()
    for name, sql in rows:
        h.update((name or "").encode("utf-8"))
        h.update(b"\x00")
        norm = " ".join((sql or "").split())
        h.update(norm.encode("utf-8"))
        h.update(b"\x01")
    return h.hexdigest()


def dump_rows_for_uids(
    sqlite_path: Path,
    uids: Iterable[int],
    logger: logging.Logger,
) -> dict:
    uids_set = sorted({int(u) for u in uids})
    out: dict = {
        "schema_fingerprint": schema_fingerprint(sqlite_path),
        "tables": {t: {"columns": [], "rows": []} for t in KEYSTORE_TABLES},
    }
    if not uids_set:
        return out

    with sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True) as c:
        c.row_factory = sqlite3.Row

        if not _table_exists(c, "keyentry"):
            raise RuntimeError("persistent.sqlite has no keyentry table")

        ke_cols = _columns(c, "keyentry")
        for required in ("id", "domain", "namespace"):
            if required not in ke_cols:
                raise RuntimeError(f"keyentry schema missing column {required!r}")

        out["tables"]["keyentry"]["columns"] = ke_cols

        keyentry_ids: list = []
        ph_uids = ",".join("?" for _ in uids_set)
        for r in c.execute(
            f"SELECT * FROM keyentry WHERE domain=? AND namespace IN ({ph_uids})",
            (DOMAIN_APP, *uids_set),
        ):
            out["tables"]["keyentry"]["rows"].append(_row_dict(r, ke_cols))
            keyentry_ids.append(r["id"])

        blob_ids: list = []
        if keyentry_ids:
            ph_keids = ",".join("?" for _ in keyentry_ids)

            if _table_exists(c, "blobentry"):
                be_cols = _columns(c, "blobentry")
                out["tables"]["blobentry"]["columns"] = be_cols
                if "keyentryid" in be_cols:
                    for r in c.execute(
                        f"SELECT * FROM blobentry WHERE keyentryid IN ({ph_keids})",
                        keyentry_ids,
                    ):
                        out["tables"]["blobentry"]["rows"].append(_row_dict(r, be_cols))
                        if "id" in be_cols and r["id"] is not None:
                            blob_ids.append(r["id"])

            for tbl in ("keymetadata", "keyparameter"):
                if not _table_exists(c, tbl):
                    continue
                cols = _columns(c, tbl)
                out["tables"][tbl]["columns"] = cols
                if "keyentryid" in cols:
                    for r in c.execute(
                        f"SELECT * FROM {tbl} WHERE keyentryid IN ({ph_keids})",
                        keyentry_ids,
                    ):
                        out["tables"][tbl]["rows"].append(_row_dict(r, cols))

            if _table_exists(c, "grant"):
                g_cols = _columns(c, "grant")
                out["tables"]["grant"]["columns"] = g_cols
                if "keyentryid" in g_cols:
                    for r in c.execute(
                        f'SELECT * FROM "grant" WHERE keyentryid IN ({ph_keids})',
                        keyentry_ids,
                    ):
                        out["tables"]["grant"]["rows"].append(_row_dict(r, g_cols))

        if blob_ids and _table_exists(c, "blobmetadata"):
            bm_cols = _columns(c, "blobmetadata")
            out["tables"]["blobmetadata"]["columns"] = bm_cols
            if "blobentryid" in bm_cols:
                ph_blob = ",".join("?" for _ in blob_ids)
                for r in c.execute(
                    f"SELECT * FROM blobmetadata WHERE blobentryid IN ({ph_blob})",
                    blob_ids,
                ):
                    out["tables"]["blobmetadata"]["rows"].append(_row_dict(r, bm_cols))

    logger.info(
        "Captured keystore rows: keyentry=%d blobentry=%d blobmetadata=%d "
        "keymetadata=%d keyparameter=%d grant=%d (uids=%s)",
        len(out["tables"]["keyentry"]["rows"]),
        len(out["tables"]["blobentry"]["rows"]),
        len(out["tables"]["blobmetadata"]["rows"]),
        len(out["tables"]["keymetadata"]["rows"]),
        len(out["tables"]["keyparameter"]["rows"]),
        len(out["tables"]["grant"]["rows"]),
        ",".join(str(u) for u in uids_set),
    )
    return out


def _new_id_excluding(used: set) -> int:
    while True:
        candidate = random.getrandbits(63)
        if candidate and candidate not in used:
            return candidate


def merge_rows_into_db(
    target_sqlite_path: Path,
    snapshot: dict,
    uid_remap: dict,
    logger: logging.Logger,
) -> dict:
    """Merge `snapshot` rows into the keystore SQLite at `target_sqlite_path`.

    `uid_remap` maps snapshot-UID -> live-UID. Any snapshot UID not in the
    remap is left as-is (caller decides whether to allow that).
    """
    target_fp = schema_fingerprint(target_sqlite_path)
    src_fp = snapshot.get("schema_fingerprint")
    if src_fp != target_fp:
        raise RuntimeError(
            f"keystore schema fingerprint mismatch "
            f"(snapshot={(src_fp or '?')[:12]} target={target_fp[:12]}); refusing to merge"
        )

    tables = snapshot.get("tables") or {}
    ke_meta = tables.get("keyentry") or {}
    ke_cols = list(ke_meta.get("columns") or [])
    ke_rows = list(ke_meta.get("rows") or [])
    if not ke_rows:
        return {"deleted": 0, "inserted": 0, "remapped_keyentry": 0, "remapped_blob": 0, "remapped_grant": 0}

    if "id" not in ke_cols or "namespace" not in ke_cols or "domain" not in ke_cols:
        raise RuntimeError("snapshot keyentry rows are missing required columns")

    rows_by_src_uid: dict[int, list[dict]] = {}
    for row in ke_rows:
        ns_raw = row.get("namespace")
        ns = _dec(ns_raw)
        rows_by_src_uid.setdefault(int(ns), []).append(row)

    keyentry_id_remap: dict = {}
    blob_id_remap: dict = {}
    grant_id_remap: dict = {}

    inserted = 0
    deleted = 0

    with sqlite3.connect(target_sqlite_path) as conn:
        conn.row_factory = sqlite3.Row
        with conn:
            used_keyentry_ids = {r[0] for r in conn.execute("SELECT id FROM keyentry") if r[0] is not None}
            used_grant_ids = set()
            if _table_exists(conn, "grant"):
                used_grant_ids = {r[0] for r in conn.execute('SELECT id FROM "grant"') if r[0] is not None}
            used_blob_ids = set()
            if _table_exists(conn, "blobentry"):
                used_blob_ids = {r[0] for r in conn.execute("SELECT id FROM blobentry") if r[0] is not None}

            live_uids = sorted({uid_remap.get(u, u) for u in rows_by_src_uid.keys()})
            for live_uid in live_uids:
                cur = conn.execute(
                    "SELECT id FROM keyentry WHERE domain=? AND namespace=?",
                    (DOMAIN_APP, live_uid),
                )
                existing = [r["id"] for r in cur.fetchall()]
                if not existing:
                    continue
                ph = ",".join("?" for _ in existing)
                if _table_exists(conn, "blobentry"):
                    be_ids_to_drop = [
                        r[0] for r in conn.execute(
                            f"SELECT id FROM blobentry WHERE keyentryid IN ({ph})", existing
                        ).fetchall() if r[0] is not None
                    ]
                    if be_ids_to_drop and _table_exists(conn, "blobmetadata"):
                        bph = ",".join("?" for _ in be_ids_to_drop)
                        conn.execute(
                            f"DELETE FROM blobmetadata WHERE blobentryid IN ({bph})",
                            be_ids_to_drop,
                        )
                    for bid in be_ids_to_drop:
                        used_blob_ids.discard(bid)
                    conn.execute(f"DELETE FROM blobentry WHERE keyentryid IN ({ph})", existing)
                for tbl in ("keymetadata", "keyparameter"):
                    if _table_exists(conn, tbl):
                        conn.execute(f"DELETE FROM {tbl} WHERE keyentryid IN ({ph})", existing)
                if _table_exists(conn, "grant"):
                    gids = [
                        r[0] for r in conn.execute(
                            f'SELECT id FROM "grant" WHERE keyentryid IN ({ph})', existing
                        ).fetchall() if r[0] is not None
                    ]
                    for gid in gids:
                        used_grant_ids.discard(gid)
                    conn.execute(f'DELETE FROM "grant" WHERE keyentryid IN ({ph})', existing)
                conn.execute(f"DELETE FROM keyentry WHERE id IN ({ph})", existing)
                for kid in existing:
                    used_keyentry_ids.discard(kid)
                deleted += len(existing)

            ke_collist = ",".join(f'"{c}"' for c in ke_cols)
            ke_phs = ",".join("?" for _ in ke_cols)
            for src_uid, rows in rows_by_src_uid.items():
                live_uid = uid_remap.get(src_uid, src_uid)
                for row in rows:
                    src_id = _dec(row["id"])
                    new_id = src_id
                    if new_id in used_keyentry_ids:
                        new_id = _new_id_excluding(used_keyentry_ids)
                        keyentry_id_remap[src_id] = new_id
                    used_keyentry_ids.add(new_id)

                    values = []
                    for col in ke_cols:
                        if col == "id":
                            values.append(new_id)
                        elif col == "namespace":
                            values.append(live_uid)
                        elif col == "domain":
                            values.append(DOMAIN_APP)
                        else:
                            values.append(_dec(row.get(col)))
                    conn.execute(
                        f"INSERT INTO keyentry ({ke_collist}) VALUES ({ke_phs})",
                        values,
                    )
                    inserted += 1

            def remap_keyentry(kid):
                return keyentry_id_remap.get(kid, kid)

            be_meta = tables.get("blobentry") or {}
            be_cols = list(be_meta.get("columns") or [])
            be_rows = list(be_meta.get("rows") or [])
            if be_cols and be_rows and _table_exists(conn, "blobentry"):
                be_collist = ",".join(f'"{c}"' for c in be_cols)
                be_phs = ",".join("?" for _ in be_cols)
                for row in be_rows:
                    src_kid = _dec(row.get("keyentryid"))
                    new_kid = remap_keyentry(src_kid) if src_kid is not None else None

                    new_be_id = None
                    if "id" in be_cols:
                        src_be_id = _dec(row.get("id"))
                        new_be_id = src_be_id
                        if new_be_id is not None:
                            if new_be_id in used_blob_ids:
                                new_be_id = _new_id_excluding(used_blob_ids)
                                blob_id_remap[src_be_id] = new_be_id
                            used_blob_ids.add(new_be_id)

                    values = []
                    for col in be_cols:
                        if col == "id":
                            values.append(new_be_id)
                        elif col == "keyentryid":
                            values.append(new_kid)
                        else:
                            values.append(_dec(row.get(col)))
                    conn.execute(
                        f"INSERT INTO blobentry ({be_collist}) VALUES ({be_phs})",
                        values,
                    )

            bm_meta = tables.get("blobmetadata") or {}
            bm_cols = list(bm_meta.get("columns") or [])
            bm_rows = list(bm_meta.get("rows") or [])
            if bm_cols and bm_rows and _table_exists(conn, "blobmetadata"):
                used_bm_ids = {r[0] for r in conn.execute("SELECT id FROM blobmetadata") if r[0] is not None}
                bm_collist = ",".join(f'"{c}"' for c in bm_cols)
                bm_phs = ",".join("?" for _ in bm_cols)
                for row in bm_rows:
                    src_bid = _dec(row.get("blobentryid"))
                    new_bid = blob_id_remap.get(src_bid, src_bid)
                    new_bm_id = None
                    if "id" in bm_cols:
                        src_bm_id = _dec(row.get("id"))
                        new_bm_id = src_bm_id
                        if new_bm_id is not None:
                            if new_bm_id in used_bm_ids:
                                new_bm_id = _new_id_excluding(used_bm_ids)
                            used_bm_ids.add(new_bm_id)
                    values = []
                    for col in bm_cols:
                        if col == "id":
                            values.append(new_bm_id)
                        elif col == "blobentryid":
                            values.append(new_bid)
                        else:
                            values.append(_dec(row.get(col)))
                    conn.execute(
                        f"INSERT INTO blobmetadata ({bm_collist}) VALUES ({bm_phs})",
                        values,
                    )

            for tbl in ("keymetadata", "keyparameter"):
                meta = tables.get(tbl) or {}
                cols = list(meta.get("columns") or [])
                rows = list(meta.get("rows") or [])
                if not cols or not rows or not _table_exists(conn, tbl):
                    continue
                collist = ",".join(f'"{c}"' for c in cols)
                phs = ",".join("?" for _ in cols)
                for row in rows:
                    src_kid = _dec(row.get("keyentryid"))
                    new_kid = remap_keyentry(src_kid) if src_kid is not None else None
                    values = []
                    for col in cols:
                        if col == "keyentryid":
                            values.append(new_kid)
                        else:
                            values.append(_dec(row.get(col)))
                    conn.execute(
                        f"INSERT INTO {tbl} ({collist}) VALUES ({phs})",
                        values,
                    )

            gmeta = tables.get("grant") or {}
            gcols = list(gmeta.get("columns") or [])
            grows = list(gmeta.get("rows") or [])
            if gcols and grows and _table_exists(conn, "grant"):
                gcollist = ",".join(f'"{c}"' for c in gcols)
                gphs = ",".join("?" for _ in gcols)
                for row in grows:
                    src_kid = _dec(row.get("keyentryid"))
                    new_kid = remap_keyentry(src_kid) if src_kid is not None else None
                    new_gid = None
                    if "id" in gcols:
                        src_gid = _dec(row.get("id"))
                        new_gid = src_gid
                        if new_gid is not None:
                            if new_gid in used_grant_ids:
                                new_gid = _new_id_excluding(used_grant_ids)
                                grant_id_remap[src_gid] = new_gid
                            used_grant_ids.add(new_gid)
                    values = []
                    for col in gcols:
                        if col == "id":
                            values.append(new_gid)
                        elif col == "keyentryid":
                            values.append(new_kid)
                        else:
                            values.append(_dec(row.get(col)))
                    conn.execute(
                        f'INSERT INTO "grant" ({gcollist}) VALUES ({gphs})',
                        values,
                    )

            ic = conn.execute("PRAGMA integrity_check").fetchone()
            if ic and ic[0] != "ok":
                raise RuntimeError(f"integrity_check failed after merge: {ic[0]}")

            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.DatabaseError:
                pass

    logger.info(
        "Keystore merge: deleted %d, inserted %d (id-remaps: keyentry=%d blob=%d grant=%d)",
        deleted, inserted,
        len(keyentry_id_remap), len(blob_id_remap), len(grant_id_remap),
    )
    return {
        "deleted": deleted,
        "inserted": inserted,
        "remapped_keyentry": len(keyentry_id_remap),
        "remapped_blob": len(blob_id_remap),
        "remapped_grant": len(grant_id_remap),
    }


def write_snapshot(snapshot: dict, out_path: Path) -> None:
    out_path.write_text(
        json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def read_snapshot(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
