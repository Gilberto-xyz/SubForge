#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Renombrar Metadatos - Basado en Limpiar_Audios.py (Jul-2025)
===========================================================
Renombra los metadatos de pistas (audio/subtítulos) y el título del
segmento en contenedores Matroska ya filtrados.

Uso típico: ejecutar después de eliminar audios/subtítulos no deseados.

Requisitos: MKVToolNix (mkvmerge/mkvpropedit) instalado.
"""

from __future__ import annotations

###############################################################################
# Imports
###############################################################################

import argparse
import inspect
import json
import logging
import os
import pathlib
import re
import shutil
import subprocess
import sys
from typing import Callable, Dict, List, Tuple

# Barra de progreso (opcional)
try:
    from tqdm import tqdm  # type: ignore
except Exception:  # pragma: no cover
    tqdm = None  # type: ignore

# Colores de consola (opcional)
try:
    import colorama  # type: ignore
except Exception:  # pragma: no cover
    colorama = None  # type: ignore

def _enable_windows_ansi() -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        enabled = False
        for handle_id in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            handle = kernel32.GetStdHandle(handle_id)
            mode = ctypes.c_uint()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                if kernel32.SetConsoleMode(handle, mode.value | 0x0004):
                    enabled = True
        return enabled
    except Exception:
        return False


COLOR_ENABLED = False
if colorama:
    try:
        colorama.just_fix_windows_console()
        COLOR_ENABLED = True
    except Exception:
        COLOR_ENABLED = False
elif os.name != "nt":
    COLOR_ENABLED = True
else:
    COLOR_ENABLED = _enable_windows_ansi()

ANSI_RESET = "\x1b[0m"
ANSI_GREEN = "\x1b[32m"
ANSI_YELLOW = "\x1b[33m"
ANSI_CYAN = "\x1b[36m"
ANSI_MAGENTA = "\x1b[35m"
ANSI_RED = "\x1b[31m"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

###############################################################################
# Configuración
###############################################################################

MKVPROPEDIT = r"C:\\Program Files\\MKVToolNix\\mkvpropedit.exe"  # o "mkvpropedit" si está en PATH
MKVMERGE = r"C:\\Program Files\\MKVToolNix\\mkvmerge.exe"       # usado para inspección/filtrado
# Para filtrar aceptamos los mismos formatos que el script base.
EXT_VIDEOS = {".mkv", ".mp4", ".webm", ".avi"}
OUTPUT_FOLDER = "filtrados"
ORIGINALS_FOLDER = "ORIGINAL"

DEFAULT_BRAND = "GDriveLatinoHD"  # texto a insertar en nombres de pista

PROGRESS_RE = re.compile(r"(\d{1,3})%")


def _color(text: str, color: str) -> str:
    return f"{color}{text}{ANSI_RESET}" if COLOR_ENABLED else text


def _tqdm_supports_colour() -> bool:
    if not tqdm:
        return False
    try:
        return "colour" in inspect.signature(tqdm).parameters
    except (TypeError, ValueError):
        return False


def _visible_len(text: str) -> int:
    return len(ANSI_RE.sub("", text))


class SimpleBar:
    def __init__(
        self,
        total: int,
        desc: str,
        unit: str,
        inplace: bool,
        show_count: bool,
        width: int = 24,
        stream=None,
    ) -> None:
        self.total = max(int(total), 0)
        self.n = 0
        self.desc = desc
        self.unit = unit
        self.width = max(int(width), 10)
        self.inplace = inplace
        self.show_count = show_count
        self.stream = stream or sys.stdout
        self.last_len = 0
        self.enabled = True
        if hasattr(self.stream, "isatty") and not self.stream.isatty():
            self.inplace = False

    def set_description(self, desc: str) -> None:
        self.desc = desc

    def _render(self) -> str:
        if self.total <= 0:
            current = max(self.n, 0)
            pct = 0
            filled = 0
        else:
            current = min(max(self.n, 0), self.total)
            pct = int((current / self.total) * 100)
            filled = int((current / self.total) * self.width)
        bar = ("#" * filled) + ("-" * (self.width - filled))
        if self.show_count:
            suffix = f"{current}/{self.total} {self.unit}".strip()
            return f"{self.desc} [{bar}] {suffix} ({pct:3d}%)"
        return f"{self.desc} [{bar}] {pct:3d}%"

    def refresh(self) -> None:
        if not self.enabled:
            return
        msg = self._render()
        visible_len = _visible_len(msg)
        if self.inplace:
            pad = max(self.last_len - visible_len, 0)
            self.stream.write("\r" + msg + (" " * pad))
            self.stream.flush()
            self.last_len = max(self.last_len, visible_len)
        else:
            self.stream.write(msg + "\n")
            self.stream.flush()

    def update(self, n: int) -> None:
        self.n = min(self.total, self.n + int(n))
        self.refresh()

    def close(self) -> None:
        if self.inplace and self.enabled:
            self.stream.write("\n")
            self.stream.flush()

###############################################################################
# Utilidades de normalización (tomadas/adaptadas de Limpiar_Audios.py)
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

# Idiomas permitidos como en el script base
ALLOWED_LANGS = {"spa", "eng", "jpn", "zho", "chi"}
# ALLOWED_LANGS = {"kor"}

# Heurística para preferir LATAM cuando sea posible
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


# Indicadores de español de España / Europeo
SPANISH_EU_HINTS = (
    "castellano",
    "espana",
    "europeo",
    "eu",
)

def _norm_lang(lang: str, name: str) -> str:
    """Normaliza el código de idioma a iso3 reducido: spa/eng/jpn/zho/und."""
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
    """Decide entre es/es-419/es-ES conservando variantes si existen."""
    lraw = (lang_raw or "").lower()
    lietf = (lang_ietf or "").lower()
    name_l = (name or "").lower()
    # Preferir lo que ya venga definido
    if lietf.startswith("es-419"):
        return "es-419"
    if lietf.startswith("es-es"):
        return "es-ES"
    if lraw.startswith("es-419"):
        return "es-419"
    if lraw.startswith("es-es"):
        return "es-ES"
    # Inferir por nombre
    if any(h in name_l for h in SPANISH_NAME_HINTS):
        return "es-419"
    if any(h in name_l for h in SPANISH_EU_HINTS):
        return "es-ES"
    return "es"


def _best_ietf(lang_base: str, lang_raw: str, lang_ietf: str | None, name: str) -> str:
    """Devuelve el mejor código IETF para escribir en 'language-ietf'."""
    if lang_base == "spa":
        return _best_spanish_ietf(lang_raw, lang_ietf, name)
    if lang_base == "eng":
        lietf = (lang_ietf or "").lower()
        if lietf.startswith("en-us"):
            return "en-US"
        if lietf.startswith("en-gb") or "british" in (name or "").lower() or " uk" in (" "+(name or "").lower()):
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
# Inspección de pistas via mkvmerge -J y selección como el script base
###############################################################################

TrackInfo = Dict[str, object]


def check_tools() -> None:
    if shutil.which(MKVPROPEDIT) is None and shutil.which("mkvpropedit") is None:
        sys.exit("No se encontró 'mkvpropedit'. Añádelo al PATH o ajusta MKVPROPEDIT en el script.")
    if shutil.which(MKVMERGE) is None and shutil.which("mkvmerge") is None:
        sys.exit("No se encontró 'mkvmerge'. Añádelo al PATH o ajusta MKVMERGE en el script.")


def _resolve_bin(pref: str, fallback: str) -> str:
    """Devuelve ruta ejecutable existente: preferido o por nombre si está en PATH."""
    p = shutil.which(pref) if pathlib.Path(pref).name == pref else (pref if pathlib.Path(pref).exists() else None)
    if p:
        return p
    f = shutil.which(fallback)
    if f:
        return f
    return pref  # dejar que falle de forma explícita


def get_tracks_info(path: pathlib.Path) -> Tuple[List[TrackInfo], List[TrackInfo]]:
    """Devuelve (audios, subs) con orden de aparición y propiedades relevantes."""
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
            obj["pos"] = a_idx  # posición relativa dentro del tipo (1..n)
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
    """Selección automática con preferencia a es-419 para subtítulos."""
    a_allowed = [t for t in audio if t["lang"] in ALLOWED_LANGS or t["lang"] == "und"]
    s_allowed = [t for t in subs if t["lang"] in ALLOWED_LANGS or t["lang"] == "und"]

    a_ids = [t["id"] for t in a_allowed]

    def _is_es419(tr: TrackInfo) -> bool:
        lietf = str(tr.get("lang_ietf") or "").lower()
        lraw = str(tr.get("lang_raw") or "").lower()
        return lietf.startswith("es-419") or lraw.startswith("es-419")

    audio_es = [t for t in a_allowed if t["lang"] == "spa"]
    if audio_es:
        es419_a = [t for t in audio_es if _is_es419(t)]
        if es419_a:
            audio_default = next((t for t in es419_a if t.get("default")), es419_a[0])
        else:
            latam = [t for t in audio_es if any(h in (t.get("name") or "").lower() for h in SPANISH_NAME_HINTS)]
            if latam:
                audio_default = next((t for t in latam if t.get("default")), latam[0])
            else:
                audio_default = next((t for t in audio_es if t.get("default")), audio_es[0])
    else:
        audio_default = next((t for t in a_allowed if t.get("default")), (a_allowed[0] if a_allowed else None))

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
                def _is_latam(t: TrackInfo) -> bool:
                    name = (t.get("name") or "").lower()
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

    # Añadir siempre subtítulos EN inglés
    eng_sub_ids = [t["id"] for t in s_allowed if t["lang"] == "eng"]
    if eng_sub_ids:
        seen = set(s_ids)
        s_ids.extend(i for i in eng_sub_ids if i not in seen and not seen.add(i))

    # Mantener subs de los mismos idiomas que audios permitidos
    a_langs_keep = {t["lang"] for t in a_allowed if t["lang"] in ALLOWED_LANGS}
    extra_sub_ids = [t["id"] for t in s_allowed if t["lang"] in a_langs_keep]
    if s_ids:
        seen = set(s_ids)
        s_ids.extend(i for i in extra_sub_ids if i not in seen and not seen.add(i))
    else:
        s_ids = extra_sub_ids

    return a_ids, s_ids, audio_default_id, sub_default_id


def run_mkvmerge(
    src: pathlib.Path,
    dst: pathlib.Path,
    video_ids: List[str],
    audio_ids: List[str],
    sub_ids: List[str],
    audio_es_id: str | None = None,
    sub_es_forzado_id: str | None = None,
    progress_cb: Callable[[int], None] | None = None,
) -> bool:
    cmd = [_resolve_bin(MKVMERGE, "mkvmerge"), "--no-global-tags", "-o", str(dst)]
    if video_ids:
        cmd += ["-d", ",".join(str(i) for i in video_ids)]
    if audio_ids:
        cmd += ["-a", ",".join(str(i) for i in audio_ids)]
        for aid in audio_ids:
            cmd += ["--default-track-flag", f"{aid}:{'yes' if aid == audio_es_id else 'no'}"]
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
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        output: List[str] = []
        last_pct = -1
        if progress_cb:
            progress_cb(0)
        if proc.stdout:
            for line in proc.stdout:
                output.append(line)
                match = PROGRESS_RE.search(line)
                if match:
                    try:
                        pct = int(match.group(1))
                    except ValueError:
                        continue
                    if 0 <= pct <= 100 and pct != last_pct:
                        last_pct = pct
                        if progress_cb:
                            progress_cb(pct)
        ret = proc.wait()
        if ret == 0:
            if progress_cb and last_pct < 100:
                progress_cb(100)
            return True
        logging.error("mkvmerge error (%s): %s", src.name, "".join(output).strip())
        return False
    except OSError as exc:
        logging.error("mkvmerge error (%s): %s", src.name, exc)
        return False


###############################################################################
# Lógica de renombrado
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
    # Etiqueta humana con variantes para español
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
    """Construye y ejecuta un comando mkvpropedit para renombrar en sitio."""
    mkvpropedit_bin = _resolve_bin(MKVPROPEDIT, "mkvpropedit")

    audios, subs = get_tracks_info(file_path)
    if not audios and not subs:
        logging.info("%s: no hay pistas de audio/subtítulos que renombrar", file_path.name)
        return True

    cmd: List[str] = [mkvpropedit_bin]
    if normalize_lang_ietf:
        cmd += ["--normalize-language-ietf", "canonical"]
    cmd.append(str(file_path))

    # Título del segmento
    if set_title:
        cmd += ["--edit", "info", "--set", f"title=[{brand}]"]

    # Pistas de audio (track:aN)
    for a in audios:
        pos = int(a.get("pos", 0))
        lang_base = str(a.get("lang", "und"))
        lang_raw = str(a.get("lang_raw", ""))
        lang_ietf = a.get("lang_ietf")
        new_name = build_new_name(lang_base, lang_raw, lang_ietf, str(a.get("name", "")), brand, is_sub=False, forced=False)
        ietf = _best_ietf(lang_base, lang_raw, str(lang_ietf) if lang_ietf else None, str(a.get("name", "")))
        cmd += [
            "--edit", f"track:a{pos}",
            "--set", f"name={new_name}",
            "--set", f"language={'spa' if lang_base=='spa' else ('eng' if lang_base=='eng' else ('jpn' if lang_base=='jpn' else ('zho' if lang_base=='zho' else lang_base)))}",
            "--set", f"language-ietf={ietf}",
        ]

    # Pistas de subtítulos (track:sN)
    for s in subs:
        pos = int(s.get("pos", 0))
        lang_base = str(s.get("lang", "und"))
        lang_raw = str(s.get("lang_raw", ""))
        lang_ietf = s.get("lang_ietf")
        forced = bool(s.get("forced", False))
        new_name = build_new_name(lang_base, lang_raw, lang_ietf, str(s.get("name", "")), brand, is_sub=True, forced=forced)
        ietf = _best_ietf(lang_base, lang_raw, str(lang_ietf) if lang_ietf else None, str(s.get("name", "")))
        cmd += [
            "--edit", f"track:s{pos}",
            "--set", f"name={new_name}",
            "--set", f"language={'spa' if lang_base=='spa' else ('eng' if lang_base=='eng' else ('jpn' if lang_base=='jpn' else ('zho' if lang_base=='zho' else lang_base)))}",
            "--set", f"language-ietf={ietf}",
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
# Recorrido de archivos y CLI
###############################################################################


def list_target_files(folder: pathlib.Path):
    for p in folder.rglob("*"):
        if p.is_file() and p.suffix.lower() in EXT_VIDEOS:
            yield p


def process_video(
    path: pathlib.Path,
    workdir: pathlib.Path,
    output_in_root: bool,
    progress_cb: Callable[[int], None] | None = None,
) -> Tuple[pathlib.Path | None, str]:
    """Filtra pistas como el script base y devuelve el destino (sin renombrar metadatos)."""
    originals = workdir / ORIGINALS_FOLDER
    originals.mkdir(exist_ok=True)

    # Mover original
    original_dest = originals / path.name
    if path.parent != originals:
        if not original_dest.exists():
            shutil.move(path, original_dest)  # type: ignore[arg-type]
        src = original_dest
    else:
        src = path

    # Inspeccionar
    info_json = subprocess.check_output([_resolve_bin(MKVMERGE, "mkvmerge"), "-J", str(src)], text=True, encoding="utf-8", errors="replace")
    v_tracks, a_tracks, s_tracks = parse_tracks_full(info_json)

    # Selección (automática fast)
    a_ids, s_ids, audio_es_id, sub_es_forzado_id = select_tracks_fast(a_tracks, s_tracks)

    # Destino filtrado
    if output_in_root:
        dst = workdir / f"{src.stem} (filtered){src.suffix}"
    else:
        out_root = workdir / OUTPUT_FOLDER
        out_root.mkdir(exist_ok=True)
        dst = out_root / f"{src.stem} (filtered){src.suffix}"

    ok = run_mkvmerge(
        src,
        dst,
        [t["id"] for t in v_tracks],
        a_ids,
        s_ids,
        audio_es_id,
        sub_es_forzado_id,
        progress_cb=progress_cb,
    )
    if not ok:
        return None, "error"
    return dst, "filtrado"


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(format="[%(levelname)s] %(message)s", level=level)


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Filtra pistas como el script base y luego renombra metadatos.\n"
            "- Filtrado: misma lógica (prioridad español y forzados).\n"
            "- Renombrado: normaliza language y nombra pistas con la marca."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("ruta", nargs="?", default=".", help="Archivo o carpeta a procesar")
    parser.add_argument("-b", "--brand", default=DEFAULT_BRAND, help="Texto de marca a añadir, p.ej. 'GDriveLatinoHD'")
    parser.add_argument("-v", "--verbose", action="store_true", help="Salida detallada")
    args = parser.parse_args()

    setup_logging(args.verbose)
    check_tools()

    ruta = pathlib.Path(args.ruta).resolve()

    files: List[pathlib.Path] = []
    if ruta.is_file() and ruta.suffix.lower() in EXT_VIDEOS:
        files = [ruta]
        workdir = ruta.parent
    elif ruta.is_dir():
        files = list(list_target_files(ruta))
        workdir = ruta
    else:
        logging.error("La ruta indicada no es un archivo/carpeta válido.")
        sys.exit(2)

    if not files:
        logging.info("No se encontraron videos que procesar.")
        return

    total_files = len(files)
    # Política de salida: si solo 1 archivo => raíz; si más de 2 => carpeta
    output_in_root = total_files <= 2

    steps_total = total_files  # avance global por archivo
    use_tqdm = bool(tqdm)
    if use_tqdm and hasattr(sys.stderr, "isatty") and not sys.stderr.isatty():
        use_tqdm = False
    tqdm_colour_ok = _tqdm_supports_colour() if use_tqdm else False
    pbar_kwargs = {"colour": "cyan"} if tqdm_colour_ok else {}
    if use_tqdm:
        pbar = tqdm(
            total=steps_total,
            desc=_color("Global", ANSI_CYAN),
            unit="archivo",
            **pbar_kwargs,
        )
    else:
        pbar = SimpleBar(
            total=steps_total,
            desc=_color("Global", ANSI_CYAN),
            unit="archivo",
            inplace=False,
            show_count=True,
        )
        pbar.refresh()

    ok_total = 0
    for idx, f in enumerate(files, start=1):
        name = f.name
        if args.verbose or not pbar:
            print(f"[{idx}/{total_files}] {_color('Mux', ANSI_MAGENTA)}: {name}")
        if pbar and use_tqdm:
            pbar.set_description(_color(f"Mux: {name[:40]}", ANSI_CYAN))

        mux_pbar = None
        if use_tqdm:
            mux_kwargs = {"colour": "magenta"} if tqdm_colour_ok else {}
            mux_pbar = tqdm(
                total=100,
                desc=_color(f"Mux: {name[:40]}", ANSI_MAGENTA),
                unit="%",
                leave=False,
                **mux_kwargs,
            )
        else:
            mux_pbar = SimpleBar(
                total=100,
                desc=_color(f"Mux: {name[:40]}", ANSI_MAGENTA),
                unit="%",
                inplace=True,
                show_count=False,
            )

        def _mux_progress(pct: int):
            if mux_pbar:
                mux_pbar.n = pct
                mux_pbar.refresh()
            elif args.verbose:
                print(f"    {_color('Progreso mux', ANSI_MAGENTA)}: {pct}%")

        dst, status = process_video(
            f,
            workdir=workdir,
            output_in_root=output_in_root,
            progress_cb=_mux_progress,
        )
        if mux_pbar:
            if status == "filtrado":
                mux_pbar.n = 100
                mux_pbar.refresh()
            mux_pbar.close()
        if status != "filtrado" or not dst:
            if args.verbose or not pbar:
                print(f"    {_color('Error', ANSI_RED)} al filtrar: {name}")
            # aún contamos paso de rename para no desbalancear la barra
            if pbar:
                if use_tqdm:
                    pbar.set_description(_color(f"Meta: {name[:40]}", ANSI_CYAN))
                pbar.update(1)
            continue

        # Renombrar metadatos (solo MKV/WEBM)
        rename_ok = True
        if dst.suffix.lower() in {".mkv", ".webm"}:
            if args.verbose or not pbar:
                print(f"[{idx}/{total_files}] {_color('Meta', ANSI_YELLOW)}: {dst.name}")
            if pbar and use_tqdm:
                pbar.set_description(_color(f"Meta: {dst.name[:40]}", ANSI_CYAN))
            rename_ok = mkvpropedit_rename(dst, brand=args.brand, set_title=True, verbose=False)
        else:
            logging.info("%s: contenedor no Matroska; se omitió renombrado de metadatos.", dst.name)
        if pbar:
            pbar.update(1)
        ok_total += 1 if rename_ok else 0

        # Notificación breve por archivo
        msg = (
            f"{_color('OK', ANSI_GREEN)} {dst.name}"
            if rename_ok
            else f"{_color('ERROR', ANSI_RED)} {dst.name}"
        )
        if pbar and use_tqdm:
            tqdm.write(msg)
        else:
            print(msg)

    if pbar:
        pbar.close()

    print(f"Completado: {ok_total}/{total_files} archivos OK.")


if __name__ == "__main__":
    main()


