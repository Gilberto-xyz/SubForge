#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
MP4 → MKV + Renombrado
======================
Convierte archivos MP4 a MKV aplicando la misma lógica de selección de pistas
que ``Limpiar_audios_name.py``:

 - Conserva todas las pistas de audio originales; solo marca español como predeterminado si aún no lo está.
 - Mantiene subtítulos permitidos y prioriza los forzados cuando hay audio español.
- Renombra metadatos y normaliza los idiomas vía ``mkvpropedit``.

Requisitos: MKVToolNix (mkvmerge/mkvpropedit) instalado y accesible.
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
import shutil
import subprocess
import sys
from typing import Dict, List, Tuple

try:
    from tqdm import tqdm  # type: ignore
except Exception:  # pragma: no cover
    tqdm = None  # type: ignore

###############################################################################
# Configuración
###############################################################################

MKVPROPEDIT = r"C:\Program Files\MKVToolNix\mkvpropedit.exe"
MKVMERGE = r"C:\Program Files\MKVToolNix\mkvmerge.exe"

EXT_VIDEOS = {".mp4"}
OUTPUT_FOLDER = "mux_mp4_mkv"
DEFAULT_BRAND = "GDriveLatinoHD"
OUTPUT_SUFFIX = "WEB-DL"

COLOR_DELETE = "\033[95m" if sys.stdout.isatty() else ""
COLOR_RESET = "\033[0m" if sys.stdout.isatty() else ""

###############################################################################
# Normalización de idiomas (tomado de Limpiar_audios_name.py)
###############################################################################

FORCED_NAME_HINTS = (
    "forced",
    "forzado",
    "forzados",
    "signs",
    "songs",
    "signs & songs",
    "signs/songs",
)

ALLOWED_LANGS = {"spa", "eng", "jpn", "zho", "chi"}

SPANISH_NAME_HINTS = (
    "spanish",
    "español",
    "espanol",
    "castellano",
    "latam",
    "",
    "mex",
    "méx",
    "mexico",
)

SPANISH_EU_HINTS = (
    "castellano",
    "espana",
    "europeo",
    "eu",
)


def _norm_lang(lang: str, name: str) -> str:
    l = (lang or "und").lower().strip()
    n = (name or "").lower()

    if l.startswith("es"):
        return "spa"
    if l.startswith("en"):
        return "eng"
    if l.startswith("ja") or l == "jp":
        return "jpn"
    if l.startswith("zh"):
        return "zho"

    mapper = {
        "spa": "spa",
        "es": "spa",
        "eng": "eng",
        "en": "eng",
        "jpn": "jpn",
        "ja": "jpn",
        "jp": "jpn",
        "zho": "zho",
        "chi": "zho",
        "zh": "zho",
        "cmn": "zho",
        "yue": "zho",
    }
    if l in mapper:
        return mapper[l]

    if any(h in n for h in ("espanol", "español", "castellano", "espana", "españa")):
        return "spa"
    if any(h in n for h in ("english", "ingles", "inglés")):
        return "eng"
    if any(h in n for h in ("jap", "japon", "japonés", "japones", "japanese")):
        return "jpn"
    if any(h in n for h in ("chino", "chinese", "mandarin", "canton", "cantones", "cantonese")):
        return "zho"

    return l


def _is_forced(name: str, forced_flag: bool) -> bool:
    if forced_flag:
        return True
    n = (name or "").lower()
    return any(tag in n for tag in FORCED_NAME_HINTS)


def _best_spanish_ietf(lang_raw: str, lang_ietf: str | None, name: str) -> str:
    lraw = (lang_raw or "").lower()
    lietf = (lang_ietf or "").lower()
    name_l = (name or "").lower()
    if lietf.startswith("es-419"):
        return "es-419"
    if lietf.startswith("es-es"):
        return "es-ES"
    if lraw.startswith("es-419"):
        return "es-419"
    if lraw.startswith("es-es"):
        return "es-ES"
    if any(h in name_l for h in SPANISH_NAME_HINTS):
        return "es-419"
    if any(h in name_l for h in SPANISH_EU_HINTS):
        return "es-ES"
    return "es"


def _best_ietf(lang_base: str, lang_raw: str, lang_ietf: str | None, name: str) -> str:
    if lang_base == "spa":
        return _best_spanish_ietf(lang_raw, lang_ietf, name)
    if lang_base == "eng":
        lietf = (lang_ietf or "").lower()
        if lietf.startswith("en-us"):
            return "en-US"
        if lietf.startswith("en-gb") or "british" in (name or "").lower() or " uk" in (" " + (name or "").lower()):
            return "en-GB"
        return "en"
    if lang_base == "jpn":
        lietf = (lang_ietf or "").lower()
        return "ja-JP" if lietf.startswith("ja-jp") else "ja"
    if lang_base == "zho":
        lietf = (lang_ietf or "").lower()
        if lietf.startswith("zh-cn"):
            return "zh-CN"
        if lietf.startswith("zh-tw"):
            return "zh-TW"
        return "zh"
    return lang_base


###############################################################################
# Herramientas mkvmerge/mkvpropedit
###############################################################################

TrackInfo = Dict[str, object]


def _resolve_bin(pref: str, fallback: str) -> str:
    p = shutil.which(pref) if pathlib.Path(pref).name == pref else (pref if pathlib.Path(pref).exists() else None)
    if p:
        return p
    f = shutil.which(fallback)
    if f:
        return f
    return pref


def check_tools() -> None:
    if shutil.which(MKVPROPEDIT) is None and shutil.which("mkvpropedit") is None:
        sys.exit("No se encontró 'mkvpropedit'. Ajusta MKVPROPEDIT o añádelo al PATH.")
    if shutil.which(MKVMERGE) is None and shutil.which("mkvmerge") is None:
        sys.exit("No se encontró 'mkvmerge'. Ajusta MKVMERGE o añádelo al PATH.")


def get_tracks_info(path: pathlib.Path) -> Tuple[List[TrackInfo], List[TrackInfo]]:
    mkvmerge_bin = _resolve_bin(MKVMERGE, "mkvmerge")
    out = subprocess.check_output([mkvmerge_bin, "-J", str(path)], text=True, encoding="utf-8", errors="replace")
    data = json.loads(out)
    audios: List[TrackInfo] = []
    subs: List[TrackInfo] = []
    a_idx = 0
    s_idx = 0
    for t in data.get("tracks", []):
        props = t.get("properties", {})
        typ = t.get("type")
        name = props.get("track_name", "") or ""
        lang_raw = props.get("language", "und") or "und"
        lang_ietf = props.get("language_ietf") or props.get("language-ietf")
        forced_raw = bool(props.get("forced_track", 0))
        obj: TrackInfo = {
            "id": int(t.get("id", -1)),
            "type": typ,
            "name": name,
            "lang": _norm_lang(lang_raw, name),
            "lang_raw": lang_raw,
            "lang_ietf": lang_ietf,
            "forced": _is_forced(name, forced_raw),
        }
        if typ == "audio":
            a_idx += 1
            obj["pos"] = a_idx
            audios.append(obj)
        elif typ in {"subtitles", "subtitle"}:
            s_idx += 1
            obj["pos"] = s_idx
            subs.append(obj)
    return audios, subs


def parse_tracks_full(info_json: str):
    data = json.loads(info_json)
    videos: List[TrackInfo] = []
    audios: List[TrackInfo] = []
    subs: List[TrackInfo] = []
    for t in data.get("tracks", []):
        props = t.get("properties", {})
        name = props.get("track_name", "") or ""
        lang_raw = props.get("language", "und") or "und"
        lang_ietf = props.get("language_ietf") or props.get("language-ietf")
        forced_raw = bool(props.get("forced_track", 0))
        default_raw = bool(props.get("default_track", 0))
        tr: TrackInfo = {
            "id": str(t.get("id")),
            "type": t.get("type"),
            "lang": _norm_lang(lang_raw, name),
            "lang_raw": lang_raw,
            "lang_ietf": lang_ietf,
            "codec": (t.get("codec") or "?").split("/")[0],
            "forced": _is_forced(name, forced_raw),
            "default": default_raw,
            "name": name,
        }
        if tr["type"] == "video":
            videos.append(tr)
        elif tr["type"] == "audio":
            audios.append(tr)
        elif tr["type"] in {"subtitles", "subtitle"}:
            subs.append(tr)
    return videos, audios, subs


def select_tracks_fast(audio: List[TrackInfo], subs: List[TrackInfo]):
    a_allowed = [t for t in audio if t["lang"] in ALLOWED_LANGS or t["lang"] == "und"]
    s_allowed = [t for t in subs if t["lang"] in ALLOWED_LANGS or t["lang"] == "und"]

    audio_ids = [t["id"] for t in audio]

    def _is_es419(tr: TrackInfo) -> bool:
        lietf = str(tr.get("lang_ietf") or "").lower()
        lraw = str(tr.get("lang_raw") or "").lower()
        return lietf.startswith("es-419") or lraw.startswith("es-419")

    spanish_tracks = [t for t in audio if t["lang"] == "spa"]
    spanish_default = next((t for t in spanish_tracks if t.get("default")), None)
    if spanish_default:
        audio_default = spanish_default
    elif spanish_tracks:
        es419_a = [t for t in spanish_tracks if _is_es419(t)]
        if es419_a:
            audio_default = es419_a[0]
        else:
            latam = [
                t
                for t in spanish_tracks
                if any(h in (t.get("name") or "").lower() for h in SPANISH_NAME_HINTS)
            ]
            audio_default = latam[0] if latam else spanish_tracks[0]
    else:
        existing_default = next((t for t in audio if t.get("default")), None)
        if existing_default:
            audio_default = existing_default
        else:
            fallback_pool = a_allowed or audio
            audio_default = fallback_pool[0] if fallback_pool else None

    audio_default_id = audio_default["id"] if audio_default else None
    audio_default_is_spanish = bool(audio_default and audio_default["lang"] == "spa")

    sub_default_id = None
    if audio_default_is_spanish:
        spa_forced = [t for t in s_allowed if t["lang"] == "spa" and t["forced"]]
        s_ids = [t["id"] for t in spa_forced]
        spa_forced_es419 = [t for t in spa_forced if _is_es419(t)]
        sub_default_id = (spa_forced_es419[0]["id"] if spa_forced_es419 else (spa_forced[0]["id"] if spa_forced else None))
    else:
        spa_normal = [t for t in s_allowed if t["lang"] == "spa" and not t["forced"]]
        if spa_normal:
            es419_normal = [t for t in spa_normal if _is_es419(t)]
            if es419_normal:
                pool = es419_normal
            else:
                def _is_latam(tr: TrackInfo) -> bool:
                    name = (tr.get("name") or "").lower()
                    return any(h in name for h in SPANISH_NAME_HINTS)
                latam_normal = [t for t in spa_normal if _is_latam(t)]
                pool = latam_normal or spa_normal
            chosen = next((t for t in pool if t.get("default")), pool[0])
            s_ids = [t["id"] for t in spa_normal]
            sub_default_id = chosen["id"]
        else:
            s_ids = [t["id"] for t in s_allowed]
            s_default_track = next((t for t in s_allowed if t.get("default")), None)
            sub_default_id = (s_default_track["id"] if s_default_track else None)

    eng_sub_ids = [t["id"] for t in s_allowed if t["lang"] == "eng"]
    if eng_sub_ids:
        seen = set(s_ids)
        s_ids.extend(i for i in eng_sub_ids if i not in seen and not seen.add(i))

    a_langs_keep = {t["lang"] for t in a_allowed if t["lang"] in ALLOWED_LANGS}
    extra_sub_ids = [t["id"] for t in s_allowed if t["lang"] in a_langs_keep]
    if s_ids:
        seen = set(s_ids)
        s_ids.extend(i for i in extra_sub_ids if i not in seen and not seen.add(i))
    else:
        s_ids = extra_sub_ids

    return audio_ids, s_ids, audio_default_id, sub_default_id


###############################################################################
# Renombrado de pistas
###############################################################################

LANG_HUMAN = {
    "spa": "Español ",
    "eng": "Inglés",
    "jpn": "Japonés",
    "zho": "Chino",
}


def build_new_name(
    lang_base: str,
    lang_raw: str,
    lang_ietf: str | None,
    name_hint: str,
    brand: str,
    is_sub: bool,
    forced: bool,
) -> str:
    if lang_base == "spa":
        ietf = _best_spanish_ietf(lang_raw, lang_ietf, name_hint)
        if ietf.lower().startswith("es-419"):
            label = "Español Latino"
        elif ietf.lower().startswith("es-es"):
            label = "Español España"
        else:
            label = LANG_HUMAN.get(lang_base, lang_base.upper())
    else:
        label = LANG_HUMAN.get(lang_base, lang_base.upper())

    extra = " [Forzados]" if is_sub and forced else ""
    return f"{label}{extra} [{brand}]"


def mkvpropedit_rename(
    file_path: pathlib.Path,
    brand: str,
    set_title: bool = True,
    normalize_lang_ietf: bool = True,
    verbose: bool = False,
) -> bool:
    mkvpropedit_bin = _resolve_bin(MKVPROPEDIT, "mkvpropedit")

    audios, subs = get_tracks_info(file_path)
    if not audios and not subs:
        logging.info("%s: no hay pistas para renombrar", file_path.name)
        return True

    cmd: List[str] = [mkvpropedit_bin]
    if normalize_lang_ietf:
        cmd += ["--normalize-language-ietf", "canonical"]
    cmd.append(str(file_path))

    if set_title:
        cmd += ["--edit", "info", "--set", f"title=[{brand}]"]

    for a in audios:
        pos = int(a.get("pos", 0))
        lang_base = str(a.get("lang", "und"))
        lang_raw = str(a.get("lang_raw", ""))
        lang_ietf = a.get("lang_ietf")
        new_name = build_new_name(lang_base, lang_raw, lang_ietf, str(a.get("name", "")), brand, is_sub=False, forced=False)
        ietf = _best_ietf(lang_base, lang_raw, str(lang_ietf) if lang_ietf else None, str(a.get("name", "")))
        cmd += [
            "--edit",
            f"track:a{pos}",
            "--set",
            f"name={new_name}",
            "--set",
            f"language={'spa' if lang_base == 'spa' else ('eng' if lang_base == 'eng' else ('jpn' if lang_base == 'jpn' else ('zho' if lang_base == 'zho' else lang_base)))}",
            "--set",
            f"language-ietf={ietf}",
        ]

    for s in subs:
        pos = int(s.get("pos", 0))
        lang_base = str(s.get("lang", "und"))
        lang_raw = str(s.get("lang_raw", ""))
        lang_ietf = s.get("lang_ietf")
        forced = bool(s.get("forced", False))
        new_name = build_new_name(lang_base, lang_raw, lang_ietf, str(s.get("name", "")), brand, is_sub=True, forced=forced)
        ietf = _best_ietf(lang_base, lang_raw, str(lang_ietf) if lang_ietf else None, str(s.get("name", "")))
        cmd += [
            "--edit",
            f"track:s{pos}",
            "--set",
            f"name={new_name}",
            "--set",
            f"language={'spa' if lang_base == 'spa' else ('eng' if lang_base == 'eng' else ('jpn' if lang_base == 'jpn' else ('zho' if lang_base == 'zho' else lang_base)))}",
            "--set",
            f"language-ietf={ietf}",
        ]

    if verbose:
        print(f"Renombrando metadatos: {file_path.name}")
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace")
        if verbose:
            print("Metadatos actualizados.")
        return True
    except subprocess.CalledProcessError as exc:
        logging.error("mkvpropedit error (%s): %s", file_path.name, (exc.stderr or exc.stdout))
        return False


###############################################################################
# Lógica de Mux
###############################################################################

def mux_mp4_to_mkv(
    src: pathlib.Path,
    dst: pathlib.Path,
    video_ids: List[str],
    audio_ids: List[str],
    sub_ids: List[str],
    audio_default_id: str | None,
    sub_es_forzado_id: str | None,
) -> bool:
    cmd = [_resolve_bin(MKVMERGE, "mkvmerge"), "--no-global-tags", "-o", str(dst)]
    if video_ids:
        cmd += ["-d", ",".join(str(i) for i in video_ids)]
    if audio_ids:
        cmd += ["-a", ",".join(str(i) for i in audio_ids)]
        for aid in audio_ids:
            cmd += ["--default-track-flag", f"{aid}:{'yes' if aid == audio_default_id else 'no'}"]
    else:
        cmd.append("--no-audio")
    if sub_ids:
        cmd += ["-s", ",".join(str(i) for i in sub_ids)]
        for sid in sub_ids:
            cmd += ["--default-track-flag", f"{sid}:{'yes' if sid == sub_es_forzado_id else 'no'}"]
    else:
        cmd.append("--no-subtitles")
    cmd.append(str(src))

    logging.debug("mkvmerge CMD: %s", " ".join(str(x) for x in cmd))
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return True
    except subprocess.CalledProcessError as exc:
        logging.error("mkvmerge error (%s): %s", src.name, exc.stderr.decode("utf-8", "ignore"))
        if dst.exists():
            dst.unlink()
        return False


def list_target_files(folder: pathlib.Path):
    for p in folder.rglob("*"):
        if p.is_file() and p.suffix.lower() in EXT_VIDEOS:
            yield p


def build_output_path(src: pathlib.Path, output_root: pathlib.Path | None) -> pathlib.Path:
    target_dir = output_root if output_root else src.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    base = src.stem
    suffix = OUTPUT_SUFFIX.strip()
    if suffix:
        base = f"{base} {suffix}".strip()
    dst = target_dir / f"{base}.mkv"
    counter = 1
    while dst.exists():
        dst = target_dir / f"{base} ({counter}).mkv"
        counter += 1
    return dst


def delete_original_file(path: pathlib.Path, use_tqdm_writer: bool = False) -> bool:
    try:
        path.unlink()
        msg = f"{COLOR_DELETE}Original eliminado: {path.name}{COLOR_RESET}" if COLOR_DELETE else f"Original eliminado: {path.name}"
        if tqdm and use_tqdm_writer:
            tqdm.write(msg)
        else:
            print(msg)
        return True
    except FileNotFoundError:
        logging.warning("%s: el archivo original ya no existe para borrarlo.", path.name)
    except OSError as exc:
        logging.error("No se pudo borrar %s: %s", path.name, exc)
    return False


def process_mp4(
    path: pathlib.Path,
    workdir: pathlib.Path,
    output_in_root: bool,
    brand: str,
    verbose: bool,
) -> Tuple[pathlib.Path | None, str]:
    info_json = subprocess.check_output([_resolve_bin(MKVMERGE, "mkvmerge"), "-J", str(path)], text=True, encoding="utf-8", errors="replace")
    v_tracks, a_tracks, s_tracks = parse_tracks_full(info_json)
    if not v_tracks:
        return None, "sin_video"

    audio_ids, s_ids, audio_default_id, sub_es_forzado_id = select_tracks_fast(a_tracks, s_tracks)

    out_root = None if output_in_root else (workdir / OUTPUT_FOLDER)
    dst = build_output_path(path, out_root)

    mux_ok = mux_mp4_to_mkv(path, dst, [t["id"] for t in v_tracks], audio_ids, s_ids, audio_default_id, sub_es_forzado_id)
    if not mux_ok:
        return None, "mux_error"

    rename_ok = mkvpropedit_rename(dst, brand=brand, set_title=True, verbose=verbose)
    if not rename_ok:
        return dst, "rename_error"
    return dst, "ok"


###############################################################################
# CLI
###############################################################################

def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(format="[%(levelname)s] %(message)s", level=level)


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Convierte archivos MP4 a MKV filtrando pistas como Limpiar_audios_name.py\n"
            "- Mantiene solo idiomas permitidos y prioriza español.\n"
            "- Marca el audio español como predeterminado.\n"
            "- Renombra metadatos con la marca indicada."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("ruta", nargs="?", default=".", help="Archivo MP4 o carpeta a procesar")
    parser.add_argument("-b", "--brand", default=DEFAULT_BRAND, help="Texto de marca para títulos/pistas")
    parser.add_argument("-v", "--verbose", action="store_true", help="Salida detallada")
    args = parser.parse_args()

    setup_logging(args.verbose)
    check_tools()

    ruta = pathlib.Path(args.ruta).resolve()

    if ruta.is_file() and ruta.suffix.lower() in EXT_VIDEOS:
        files = [ruta]
        workdir = ruta.parent
    elif ruta.is_dir():
        files = list(list_target_files(ruta))
        workdir = ruta
    else:
        logging.error("La ruta indicada no es un MP4 válido ni una carpeta.")
        sys.exit(2)

    if not files:
        logging.info("No se encontraron archivos MP4.")
        return

    total = len(files)
    output_in_root = total <= 2

    steps_total = total * 2  # mux + rename
    pbar = tqdm(total=steps_total, desc="Procesando", unit="paso") if tqdm else None

    ok_total = 0
    for idx, f in enumerate(files, start=1):
        fname = f.name
        if args.verbose or not pbar:
            print(f"[{idx}/{total}] Mux: {fname}")
        if pbar:
            pbar.set_description(f"Mux: {fname[:40]}")
        dst, status = process_mp4(f, workdir=workdir, output_in_root=output_in_root, brand=args.brand, verbose=args.verbose)
        if pbar:
            pbar.update(1)
        if not dst and status != "mux_error":
            if args.verbose or not pbar:
                print(f"    No se pudo procesar {fname} ({status}).")
            if pbar:
                pbar.set_description(f"Meta: {fname[:40]}")
                pbar.update(1)
            continue

        use_tqdm_writer = bool(pbar and tqdm)
        if dst:
            if args.verbose or not pbar:
                print(f"[{idx}/{total}] Meta: {dst.name}")
            if pbar:
                pbar.set_description(f"Meta: {dst.name[:40]}")
            rename_status = status
            if status == "ok":
                ok_total += 1
                delete_original_file(f, use_tqdm_writer=use_tqdm_writer)
            elif status == "rename_error":
                logging.error("%s: mux OK pero falló el renombrado.", dst.name)
            else:
                logging.warning("%s: estado %s", dst.name, status)
        else:
            rename_status = status

        if pbar:
            pbar.update(1)
            msg = f"{status.upper()} {dst.name if dst else fname}"
            tqdm.write(msg) if tqdm else None
        else:
            print(f"    Resultado: {rename_status}")

    if pbar:
        pbar.close()

    print(f"Completado: {ok_total}/{total} archivos OK.")


if __name__ == "__main__":
    main()
