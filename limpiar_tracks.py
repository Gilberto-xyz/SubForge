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
import base64
import inspect
import json
import logging
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
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

# Salida rica (opcional)
try:
    from rich.console import Console  # type: ignore
    from rich.panel import Panel  # type: ignore
except Exception:  # pragma: no cover
    Console = None  # type: ignore
    Panel = None  # type: ignore

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


def _stream_is_tty(stream) -> bool:
    try:
        return bool(stream is not None and hasattr(stream, "isatty") and stream.isatty())
    except Exception:
        return False


COLOR_ENABLED = False
HAS_TTY_OUTPUT = _stream_is_tty(sys.stdout) or _stream_is_tty(sys.stderr)
if HAS_TTY_OUTPUT:
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
RICH_CONSOLE = Console(stderr=True) if Console is not None else None

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
DEFAULT_LOCK_ACTION = "close-qbittorrent"
DEFAULT_LOCK_RETRY_SECONDS = 8
DEFAULT_QB_URL = "http://127.0.0.1:8080"


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
AUDIO_BASE_LANGS = {"spa", "eng"}
PROGRESS_MILESTONES = (10, 20, 30, 40, 50, 60, 70, 80, 90, 100)

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

    return _resolve_canonical_language_code(lang_ietf, lang_raw, lang_base) or lang_base


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


def inspect_tracks(path: pathlib.Path):
    info_json = subprocess.check_output(
        [_resolve_bin(MKVMERGE, "mkvmerge"), "-J", str(path)],
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return parse_tracks_full(info_json)


def _track_id(track: TrackInfo) -> str:
    return str(track.get("id", ""))


def _is_es419_track(tr: TrackInfo) -> bool:
    lietf = str(tr.get("lang_ietf") or "").lower()
    lraw = str(tr.get("lang_raw") or "").lower()
    return lietf.startswith("es-419") or lraw.startswith("es-419")


def _pick_preferred_spanish_audio(audio: List[TrackInfo]) -> TrackInfo | None:
    audio_es = [t for t in audio if t["lang"] == "spa"]
    if not audio_es:
        return None

    es419_a = [t for t in audio_es if _is_es419_track(t)]
    if es419_a:
        return next((t for t in es419_a if t.get("default")), es419_a[0])

    latam = [
        t
        for t in audio_es
        if any(h in (t.get("name") or "").lower() for h in SPANISH_NAME_HINTS)
    ]
    if latam:
        return next((t for t in latam if t.get("default")), latam[0])

    return next((t for t in audio_es if t.get("default")), audio_es[0])


def select_audio_tracks(audio: List[TrackInfo]) -> Tuple[List[TrackInfo], str | None]:
    """Conserva audios base (spa/eng) y respeta siempre el audio original por defecto."""
    if not audio:
        return [], None

    source_default = next((t for t in audio if t.get("default")), None)
    keep_ids = {
        _track_id(t)
        for t in audio
        if str(t.get("lang", "")).lower() in AUDIO_BASE_LANGS
    }

    if source_default is not None:
        keep_ids.add(_track_id(source_default))

    if not keep_ids:
        keep_ids.add(_track_id(source_default or audio[0]))

    selected_audio = [t for t in audio if _track_id(t) in keep_ids]
    if not selected_audio:
        selected_audio = [source_default or audio[0]]

    if source_default is not None and any(_track_id(t) == _track_id(source_default) for t in selected_audio):
        audio_default = source_default
    else:
        audio_default = _pick_preferred_spanish_audio(selected_audio)
        if audio_default is None:
            audio_default = next((t for t in selected_audio if t.get("default")), selected_audio[0])

    return selected_audio, (_track_id(audio_default) if audio_default is not None else None)


def parse_keep_track_ids(raw_value: str | None) -> List[str]:
    if raw_value is None:
        return []

    normalized_raw = raw_value.strip()
    if not normalized_raw or normalized_raw == "__none__":
        return []

    parts = re.split(r"[\s,]+", normalized_raw)
    ordered: List[str] = []
    seen = set()
    for part in parts:
        value = part.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _normalize_selection_lang_code(value: object | None) -> str:
    text = str(value or "").strip().lower().replace("_", "-")
    return text or "und"


def _normalize_track_name_for_match(name: object | None) -> str:
    text = str(name or "").strip().lower()
    while text.endswith("]"):
        updated = re.sub(r"\s*\[[^\]]+\]\s*$", "", text).strip()
        if updated == text:
            break
        text = updated
    return re.sub(r"\s+", " ", text).strip()


def _signature_value(item: Dict[str, object], *keys: str) -> object | None:
    for key in keys:
        if key in item:
            return item[key]

        if not key:
            continue

        pascal = key[0].upper() + key[1:]
        if pascal in item:
            return item[pascal]

    lowered = {str(existing_key).lower(): value for existing_key, value in item.items()}
    for key in keys:
        lowered_key = key.lower()
        if lowered_key in lowered:
            return lowered[lowered_key]

    return None


def parse_signature_payload(raw_value: str | None) -> List[Dict[str, object]]:
    if not raw_value:
        return []
    try:
        decoded = base64.b64decode(raw_value.encode("ascii")).decode("utf-8")
        data = json.loads(decoded)
    except Exception as exc:
        logging.warning("No se pudo decodificar la selección por firma: %s", exc)
        return []

    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []

    signatures: List[Dict[str, object]] = []
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            continue

        try:
            order = int(_signature_value(item, "order") or idx)
        except (TypeError, ValueError):
            order = idx

        language_code = _normalize_selection_lang_code(
            _signature_value(item, "languageCode", "language_code")
        )
        canonical_language_code = _normalize_selection_lang_code(
            _signature_value(item, "canonicalLanguageCode", "canonical_language_code")
            or _resolve_canonical_language_code(language_code)
            or language_code
        )
        signatures.append(
            {
                "track_id": str(_signature_value(item, "trackId", "track_id") or "").strip(),
                "language_code": language_code,
                "canonical_language_code": canonical_language_code,
                "name": _normalize_track_name_for_match(_signature_value(item, "name")),
                "is_default": bool(_signature_value(item, "isDefault", "is_default")),
                "is_forced": bool(_signature_value(item, "isForced", "is_forced")),
                "order": order,
            }
        )

    signatures.sort(key=lambda sig: (int(sig.get("order", 0)), str(sig.get("track_id") or "")))
    if signatures and all(
        not str(signature.get("track_id") or "").strip()
        and str(signature.get("language_code") or "und") == "und"
        and not str(signature.get("name") or "").strip()
        for signature in signatures
    ):
        logging.warning(
            "La selección por firma se decodificó, pero no trae campos útiles (track/language/name). "
            "Se ignorará y el filtro caerá al fallback por IDs."
        )
    return signatures


def summarize_signature_selection(signatures: List[Dict[str, object]]) -> str:
    labels: List[str] = []
    for signature in signatures:
        lang = (
            _resolve_language_label(
                str(signature.get("language_code") or ""),
                str(signature.get("canonical_language_code") or ""),
            )
            or str(signature.get("language_code") or "und").upper()
        )
        name = str(signature.get("name") or "").strip()
        flags: List[str] = []
        if signature.get("is_forced"):
            flags.append("forced")
        if signature.get("is_default"):
            flags.append("default")

        label = lang
        if name:
            label += f": {name}"
        if flags:
            label += f" ({', '.join(flags)})"
        labels.append(label)

    return ", ".join(labels) if labels else "sin pistas"


def _track_signature_match_score(
    track: TrackInfo,
    signature: Dict[str, object],
    *,
    is_subtitle: bool,
) -> int | None:
    track_lang_code = _normalize_selection_lang_code(
        track.get("lang_ietf") or track.get("lang_raw") or track.get("lang")
    )
    track_canonical_lang = _normalize_selection_lang_code(
        _resolve_canonical_language_code(
            str(track.get("lang_ietf") or ""),
            str(track.get("lang_raw") or ""),
            str(track.get("lang") or ""),
        )
        or track_lang_code
    )
    desired_lang_code = _normalize_selection_lang_code(signature.get("language_code"))
    desired_canonical_lang = _normalize_selection_lang_code(signature.get("canonical_language_code"))

    if desired_lang_code != "und" and track_lang_code == desired_lang_code:
        score = 90
    elif desired_canonical_lang != "und" and track_canonical_lang == desired_canonical_lang:
        score = 55
    else:
        return None

    if is_subtitle:
        track_forced = bool(track.get("forced"))
        desired_forced = bool(signature.get("is_forced"))
        if track_forced == desired_forced:
            score += 18
        elif desired_forced:
            return None

    desired_name = str(signature.get("name") or "")
    track_name = _normalize_track_name_for_match(track.get("name"))
    if desired_name:
        if track_name == desired_name:
            score += 24
        elif desired_name in track_name or track_name in desired_name:
            score += 10

    if str(track.get("id") or "") == str(signature.get("track_id") or ""):
        score += 12
    if bool(track.get("default")) == bool(signature.get("is_default")):
        score += 4
    return score


def resolve_manual_selection_by_signatures(
    tracks: List[TrackInfo],
    signatures: List[Dict[str, object]],
    preferred_default_id: str | None,
    default_signature: Dict[str, object] | None,
    *,
    is_subtitle: bool,
) -> Dict[str, object] | None:
    if not signatures:
        return None

    remaining_tracks = list(tracks)
    selected_tracks: List[TrackInfo] = []
    missing_signatures: List[Dict[str, object]] = []
    remapped = False

    for signature in signatures:
        best_track = None
        best_score = -1
        for track in remaining_tracks:
            score = _track_signature_match_score(track, signature, is_subtitle=is_subtitle)
            if score is None or score <= best_score:
                continue
            best_score = score
            best_track = track

        if best_track is None:
            missing_signatures.append(signature)
            continue

        remaining_tracks.remove(best_track)
        selected_tracks.append(best_track)
        remapped = remapped or str(best_track.get("id") or "") != str(signature.get("track_id") or "")

    if not selected_tracks:
        return None

    default_track = None
    if default_signature:
        best_default_score = -1
        for track in selected_tracks:
            score = _track_signature_match_score(track, default_signature, is_subtitle=is_subtitle)
            if score is None or score <= best_default_score:
                continue
            best_default_score = score
            default_track = track

    if default_track is None and preferred_default_id:
        default_track = next(
            (track for track in selected_tracks if str(track.get("id") or "") == str(preferred_default_id)),
            None,
        )
    if default_track is None:
        default_track = next((track for track in selected_tracks if track.get("default")), None)
    if default_track is None and is_subtitle:
        default_track = next((track for track in selected_tracks if track.get("forced")), None)
    if default_track is None:
        default_track = selected_tracks[0]

    return {
        "tracks": selected_tracks,
        "ids": [_track_id(track) for track in selected_tracks],
        "default_id": _track_id(default_track),
        "missing_signatures": missing_signatures,
        "remapped": remapped,
    }


def summarize_audio_selection(audio: List[TrackInfo], selected_audio_ids: List[str], audio_default_id: str | None) -> str:
    selected_set = {str(track_id) for track_id in selected_audio_ids}
    labels: List[str] = []
    for track in audio:
        track_id = _track_id(track)
        if track_id not in selected_set:
            continue
        lang = (
            _resolve_language_label(track.get("lang_ietf"), track.get("lang_raw"), track.get("lang"))
            or str(track.get("lang") or "und").upper()
        )
        name = str(track.get("name") or "").strip()
        flags: List[str] = []
        if track_id == str(audio_default_id):
            flags.append("default")
        elif track.get("default"):
            flags.append("source-default")

        label = lang
        if name:
            label += f": {name}"
        if flags:
            label += f" ({', '.join(flags)})"
        labels.append(label)

    return ", ".join(labels) if labels else "sin pistas"


def summarize_subtitle_selection(subs: List[TrackInfo], selected_subtitle_ids: List[str], sub_default_id: str | None) -> str:
    selected_set = {str(track_id) for track_id in selected_subtitle_ids}
    labels: List[str] = []
    for track in subs:
        track_id = _track_id(track)
        if track_id not in selected_set:
            continue

        lang = (
            _resolve_language_label(track.get("lang_ietf"), track.get("lang_raw"), track.get("lang"))
            or str(track.get("lang") or "und").upper()
        )
        name = str(track.get("name") or "").strip()
        flags: List[str] = []
        if track.get("forced"):
            flags.append("forced")
        if track_id == str(sub_default_id):
            flags.append("default")
        elif track.get("default"):
            flags.append("source-default")

        label = lang
        if name:
            label += f": {name}"
        if flags:
            label += f" ({', '.join(flags)})"
        labels.append(label)

    return ", ".join(labels) if labels else "sin pistas"


def select_subtitle_tracks(selected_audio: List[TrackInfo], subs: List[TrackInfo], audio_default_id: str | None):
    a_ids = [_track_id(t) for t in selected_audio]
    selected_audio_langs = {
        str(t.get("lang", "")).lower()
        for t in selected_audio
        if str(t.get("lang", "")).lower() not in {"", "und"}
    }
    s_allowed = [
        t
        for t in subs
        if t["lang"] in ALLOWED_LANGS or t["lang"] == "und" or str(t["lang"]).lower() in selected_audio_langs
    ]

    audio_default = next((t for t in selected_audio if _track_id(t) == str(audio_default_id)), None)
    audio_default_is_spanish = bool(audio_default and audio_default["lang"] == "spa")

    sub_default_id = None
    if audio_default_is_spanish:
        spa_forced = [t for t in s_allowed if t["lang"] == "spa" and t["forced"]]
        s_ids = [t["id"] for t in spa_forced]
        spa_forced_es419 = [t for t in spa_forced if _is_es419_track(t)]
        sub_default_id = (spa_forced_es419[0]["id"] if spa_forced_es419 else (spa_forced[0]["id"] if spa_forced else None))
    else:
        spa_normal = [t for t in s_allowed if t["lang"] == "spa" and not t["forced"]]
        if spa_normal:
            es419_normal = [t for t in spa_normal if _is_es419_track(t)]
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

    # Mantener subs de los mismos idiomas que los audios realmente conservados.
    a_langs_keep = {
        str(t["lang"]).lower()
        for t in selected_audio
        if str(t["lang"]).lower() not in {"", "und"}
    }
    extra_sub_ids = [t["id"] for t in s_allowed if t["lang"] in a_langs_keep]
    if s_ids:
        seen = set(s_ids)
        s_ids.extend(i for i in extra_sub_ids if i not in seen and not seen.add(i))
    else:
            s_ids = extra_sub_ids

    return s_ids, sub_default_id


def select_tracks_with_audio_selection(
    selected_audio: List[TrackInfo],
    subs: List[TrackInfo],
    audio_default_id: str | None,
):
    a_ids = [_track_id(t) for t in selected_audio]
    s_ids, sub_default_id = select_subtitle_tracks(selected_audio, subs, audio_default_id)
    return a_ids, s_ids, audio_default_id, sub_default_id


def select_tracks_for_audio_ids(
    audio: List[TrackInfo],
    subs: List[TrackInfo],
    selected_audio_ids: List[str],
    preferred_audio_default_id: str | None = None,
):
    selected_set = {str(track_id) for track_id in selected_audio_ids}
    selected_audio = [t for t in audio if _track_id(t) in selected_set]
    if not selected_audio:
        return [], [], None, None

    audio_default = next((t for t in selected_audio if _track_id(t) == str(preferred_audio_default_id)), None)
    if audio_default is None:
        audio_default = next((t for t in selected_audio if t.get("default")), None)
    if audio_default is None:
        audio_default = _pick_preferred_spanish_audio(selected_audio)
    if audio_default is None:
        audio_default = selected_audio[0]

    return select_tracks_with_audio_selection(
        selected_audio,
        subs,
        _track_id(audio_default) if audio_default is not None else None,
    )


def select_subtitles_for_ids(
    subs: List[TrackInfo],
    selected_subtitle_ids: List[str],
    preferred_subtitle_default_id: str | None = None,
):
    if not selected_subtitle_ids:
        return [], None

    selected_set = {str(track_id) for track_id in selected_subtitle_ids}
    selected_subs = [t for t in subs if _track_id(t) in selected_set]
    if not selected_subs:
        return [], None

    sub_default = next((t for t in selected_subs if _track_id(t) == str(preferred_subtitle_default_id)), None)
    if sub_default is None:
        sub_default = next((t for t in selected_subs if t.get("default")), None)
    if sub_default is None:
        sub_default = next((t for t in selected_subs if t.get("forced")), None)
    if sub_default is None:
        sub_default = selected_subs[0]

    return [_track_id(t) for t in selected_subs], _track_id(sub_default)


def select_tracks_fast(audio: List[TrackInfo], subs: List[TrackInfo]):
    """Conserva ES/EN, respeta el audio default original y preserva subs prioritarios."""
    selected_audio, audio_default_id = select_audio_tracks(audio)
    return select_tracks_with_audio_selection(selected_audio, subs, audio_default_id)


def _format_warning_panel(title: str, lines: List[str]) -> None:
    body = "\n".join(lines)
    if RICH_CONSOLE is not None and Panel is not None:
        RICH_CONSOLE.print(
            Panel.fit(
                body,
                title=f"[bold yellow]{title}[/bold yellow]",
                border_style="yellow",
            )
        )
        return
    logging.warning("%s | %s", title, " | ".join(lines))


def spanish_subtitle_warning(
    file_name: str,
    audios: List[TrackInfo],
    subs: List[TrackInfo],
    selected_sub_ids: List[str],
) -> List[str]:
    """Devuelve líneas de advertencia cuando no hay subs en español en la salida."""
    selected_ids = {str(sid) for sid in selected_sub_ids}
    spa_in_source = [s for s in subs if str(s.get("lang", "")) == "spa"]
    spa_in_selected = [
        s
        for s in subs
        if str(s.get("lang", "")) == "spa" and str(s.get("id", "")) in selected_ids
    ]
    has_audio_spa = any(str(a.get("lang", "")) == "spa" for a in audios)
    und_sub_count = sum(1 for s in subs if str(s.get("lang", "")) == "und")

    if spa_in_selected:
        return []

    lines: List[str] = [f"Archivo: {file_name}"]
    if not subs:
        lines.append("No se encontraron pistas de subtítulos en el archivo.")
    elif not spa_in_source:
        lines.append("No se detectaron subtítulos en español (spa/es) en el origen.")
    else:
        lines.append("Se detectaron subtítulos en español, pero ninguno quedó en la salida final.")

    if has_audio_spa:
        lines.append("Audio español: sí.")
    else:
        lines.append("Audio español: no (esto no bloquea el proceso).")

    if und_sub_count > 0:
        lines.append(f"Subtítulos con idioma indefinido (und): {und_sub_count}.")
    lines.append(f"Subtítulos seleccionados para mux: {len(selected_sub_ids)}.")
    return lines


def _is_file_in_use_error(exc: BaseException) -> bool:
    if not isinstance(exc, PermissionError):
        return False
    if getattr(exc, "winerror", None) == 32:
        return True
    return "WinError 32" in str(exc)


class QBittorrentWebClient:
    def __init__(self, base_url: str, username: str, password: str, timeout: int = 10) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def _post(self, path: str, payload: Dict[str, str]) -> str:
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(
            url=f"{self.base_url}{path}",
            data=data,
            method="POST",
            headers={"User-Agent": "SubForge/1.0"},
        )
        with self.opener.open(req, timeout=self.timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")

    def _get_json(self, path: str) -> object:
        req = urllib.request.Request(
            url=f"{self.base_url}{path}",
            method="GET",
            headers={"User-Agent": "SubForge/1.0"},
        )
        with self.opener.open(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    def login(self) -> None:
        out = self._post(
            "/api/v2/auth/login",
            {"username": self.username, "password": self.password},
        )
        if "Ok." not in out:
            raise RuntimeError("qBittorrent WebUI login failed")

    def _torrent_matches_path(self, torrent: Dict[str, object], file_path: pathlib.Path) -> bool:
        target = os.path.normcase(os.path.normpath(str(file_path.resolve())))
        name = str(torrent.get("name") or "")
        content_path = str(torrent.get("content_path") or "")
        save_path = str(torrent.get("save_path") or "")

        candidates: List[str] = []
        if content_path:
            candidates.append(content_path)
        if save_path and name:
            candidates.append(os.path.join(save_path, name))
        if name:
            candidates.append(name)

        for c in candidates:
            norm_c = os.path.normcase(os.path.normpath(c))
            if norm_c == target:
                return True
            if os.path.basename(norm_c) == os.path.normcase(file_path.name):
                return True
            if target.startswith(norm_c + os.sep):
                return True
        return False

    def find_hashes_by_file(self, file_path: pathlib.Path) -> List[str]:
        data = self._get_json("/api/v2/torrents/info?filter=all")
        if not isinstance(data, list):
            return []
        hashes: List[str] = []
        for row in data:
            if not isinstance(row, dict):
                continue
            if self._torrent_matches_path(row, file_path):
                h = str(row.get("hash") or "").strip()
                if h:
                    hashes.append(h)
        return hashes

    def delete_torrents(self, hashes: List[str], delete_files: bool) -> None:
        if not hashes:
            return
        self._post(
            "/api/v2/torrents/delete",
            {
                "hashes": "|".join(hashes),
                "deleteFiles": "true" if delete_files else "false",
            },
        )


def _close_qbittorrent_process() -> Tuple[bool, str]:
    if os.name == "nt":
        cmd = ["taskkill", "/IM", "qbittorrent.exe", "/F"]
    else:
        cmd = ["pkill", "-f", "qbittorrent"]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace")
    msg = (proc.stdout or "") + (proc.stderr or "")
    msg_l = msg.lower()
    if proc.returncode == 0:
        return True, "qBittorrent cerrado correctamente."
    if "not found" in msg_l or "no se está ejecutando ninguna instancia" in msg_l:
        return True, "qBittorrent no estaba en ejecución (se continuará con reintento)."
    return False, msg.strip() or "No se pudo cerrar qBittorrent."


def try_release_locked_file(
    file_path: pathlib.Path,
    *,
    action: str,
    qb_url: str,
    qb_user: str | None,
    qb_pass: str | None,
) -> Tuple[bool, List[str]]:
    details = [f"Archivo bloqueado: {file_path}"]
    if action == "skip":
        details.append("Acción configurada: skip (se omite desbloqueo automático).")
        return False, details

    if action == "close-qbittorrent":
        ok, close_msg = _close_qbittorrent_process()
        details.append("Acción: cerrar qBittorrent.")
        details.append(f"Resultado: {close_msg}")
        return ok, details

    delete_files = action == "remove-torrent-and-data"
    if not qb_user or not qb_pass:
        details.append("Acción: borrar torrent.")
        details.append("Falta configuración WebUI: usa --qb-user y --qb-pass.")
        return False, details

    try:
        client = QBittorrentWebClient(qb_url, qb_user, qb_pass)
        client.login()
        hashes = client.find_hashes_by_file(file_path)
        if not hashes:
            details.append("Acción: borrar torrent.")
            details.append("No se encontró torrent asociado a este archivo.")
            return False, details
        client.delete_torrents(hashes, delete_files=delete_files)
        details.append(
            f"Acción: borrar torrent ({'con datos' if delete_files else 'sin borrar datos'})."
        )
        details.append(f"Torrents eliminados: {len(hashes)}.")
        return True, details
    except Exception as exc:
        details.append(f"Error al operar con qBittorrent WebUI: {exc}")
        return False, details


def run_mkvmerge(
    src: pathlib.Path,
    dst: pathlib.Path,
    video_ids: List[str],
    audio_ids: List[str],
    sub_ids: List[str],
    audio_default_id: str | None = None,
    sub_default_id: str | None = None,
    progress_cb: Callable[[int], None] | None = None,
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
            cmd += ["--default-track-flag", f"{sid}:{'yes' if sid == sub_default_id else 'no'}"]
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

FALLBACK_LANG_NAMES = {
    "und": "Indefinido",
    "zxx": "Sin contenido lingüístico",
    "mul": "Múltiples idiomas",
    "spa": "Español",
    "es": "Español",
    "es-419": "Español (Latinoamérica)",
    "es-es": "Español (España)",
    "eng": "Inglés",
    "en": "Inglés",
    "jpn": "Japonés",
    "ja": "Japonés",
    "zho": "Chino",
    "zh": "Chino",
    "chi": "Chino",
    "fra": "Francés",
    "fre": "Francés",
    "fr": "Francés",
    "deu": "Alemán",
    "ger": "Alemán",
    "de": "Alemán",
    "ita": "Italiano",
    "it": "Italiano",
    "por": "Portugués",
    "pt": "Portugués",
    "rus": "Ruso",
    "ru": "Ruso",
    "fas": "Persa",
    "per": "Persa",
    "fa": "Persa",
    "tha": "Tailandés",
    "th": "Tailandés",
}

FALLBACK_CANONICAL_CODES = {
    "und": "und",
    "zxx": "zxx",
    "mul": "mul",
    "spa": "es",
    "es": "es",
    "es-419": "es-419",
    "es-la": "es-419",
    "es-es": "es-es",
    "eng": "en",
    "en": "en",
    "jpn": "ja",
    "ja": "ja",
    "zho": "zh",
    "zh": "zh",
    "chi": "zh",
    "fra": "fr",
    "fre": "fr",
    "fr": "fr",
    "deu": "de",
    "ger": "de",
    "de": "de",
    "ita": "it",
    "it": "it",
    "por": "pt",
    "pt": "pt",
    "rus": "ru",
    "ru": "ru",
    "fas": "fa",
    "per": "fa",
    "fa": "fa",
    "tha": "th",
    "th": "th",
}


def _normalize_catalog_lang_code(code: str | None) -> str:
    if not code:
        return "und"
    normalized = str(code).strip().lower().replace("_", "-")
    return normalized or "und"


def _load_language_catalog() -> tuple[Dict[str, str], Dict[str, str]]:
    language_names = dict(FALLBACK_LANG_NAMES)
    canonical_codes = dict(FALLBACK_CANONICAL_CODES)
    catalog_path = pathlib.Path(__file__).with_name("track_languages.json")

    if not catalog_path.exists():
        return language_names, canonical_codes

    try:
        raw = json.loads(catalog_path.read_text(encoding="utf-8"))
        display_names = raw.get("displayNames", {})
        if isinstance(display_names, dict):
            for code, label in display_names.items():
                if not isinstance(code, str) or not isinstance(label, str):
                    continue
                normalized = _normalize_catalog_lang_code(code)
                clean_label = label.strip()
                if normalized and clean_label:
                    language_names[normalized] = clean_label

        raw_canonical_codes = raw.get("canonicalBaseCodes", {})
        if isinstance(raw_canonical_codes, dict):
            for code, canonical in raw_canonical_codes.items():
                if not isinstance(code, str) or not isinstance(canonical, str):
                    continue
                normalized = _normalize_catalog_lang_code(code)
                normalized_canonical = _normalize_catalog_lang_code(canonical)
                if normalized and normalized_canonical:
                    canonical_codes[normalized] = normalized_canonical
    except Exception:
        return language_names, canonical_codes

    return language_names, canonical_codes


LANG_CATALOG_NAMES, LANG_CATALOG_CANONICAL_CODES = _load_language_catalog()


def _resolve_language_label(*codes: str | None) -> str | None:
    for raw_code in codes:
        normalized = _normalize_catalog_lang_code(raw_code)
        candidates = [
            normalized,
            LANG_CATALOG_CANONICAL_CODES.get(normalized),
        ]

        base_code = normalized.split("-")[0]
        if base_code:
            candidates.extend(
                [
                    base_code,
                    LANG_CATALOG_CANONICAL_CODES.get(base_code),
                ])

        for candidate in candidates:
            if candidate and candidate in LANG_CATALOG_NAMES:
                return LANG_CATALOG_NAMES[candidate]

    return None


def _resolve_canonical_language_code(*codes: str | None) -> str | None:
    for raw_code in codes:
        normalized = _normalize_catalog_lang_code(raw_code)
        canonical = LANG_CATALOG_CANONICAL_CODES.get(normalized)
        if canonical:
            return canonical

        base_code = normalized.split("-")[0]
        if base_code:
            canonical = LANG_CATALOG_CANONICAL_CODES.get(base_code)
            if canonical:
                return canonical

    return None


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
        label = (
            _resolve_language_label(ietf)
            or ("Español (Latinoamérica)" if ietf.lower().startswith("es-419")
                else ("Español (España)" if ietf.lower().startswith("es-es")
                      else LANG_HUMAN.get(lang_base, lang_base.upper())))
        )
    else:
        label = (
            _resolve_language_label(lang_ietf, lang_raw, lang_base)
            or LANG_HUMAN.get(lang_base, lang_base.upper())
        )

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
    file_in_use_action: str,
    keep_audio_ids: List[str],
    audio_default_id: str | None,
    keep_audio_signatures: List[Dict[str, object]],
    audio_default_signature: Dict[str, object] | None,
    keep_subtitle_ids: List[str],
    subtitle_default_id: str | None,
    keep_subtitle_signatures: List[Dict[str, object]],
    subtitle_default_signature: Dict[str, object] | None,
    manual_subtitle_selection: bool,
    delete_originals: bool,
    lock_retry_seconds: int,
    qb_url: str,
    qb_user: str | None,
    qb_pass: str | None,
    progress_cb: Callable[[int], None] | None = None,
) -> Tuple[pathlib.Path | None, str, List[str]]:
    """Filtra pistas como el script base y devuelve el destino (sin renombrar metadatos)."""
    originals = workdir / ORIGINALS_FOLDER
    originals.mkdir(exist_ok=True)

    # Mover original
    original_dest = originals / path.name
    if path.parent != originals:
        if not original_dest.exists():
            try:
                shutil.move(path, original_dest)  # type: ignore[arg-type]
            except PermissionError as exc:
                if not _is_file_in_use_error(exc):
                    raise
                _format_warning_panel(
                    "Archivo en uso",
                    [
                        f"Archivo: {path}",
                        "No se puede mover porque está en uso (WinError 32).",
                    ],
                )
                released, details = try_release_locked_file(
                    path,
                    action=file_in_use_action,
                    qb_url=qb_url,
                    qb_user=qb_user,
                    qb_pass=qb_pass,
                )
                _format_warning_panel("Desbloqueo automático", details)
                if not released:
                    return None, "locked", []
                time.sleep(max(1, int(lock_retry_seconds)))
                try:
                    shutil.move(path, original_dest)  # type: ignore[arg-type]
                except PermissionError as retry_exc:
                    if _is_file_in_use_error(retry_exc):
                        _format_warning_panel(
                            "Archivo en uso",
                            [
                                f"Archivo: {path}",
                                "Sigue bloqueado tras intento de desbloqueo automático.",
                            ],
                        )
                        return None, "locked", []
                    raise
        src = original_dest
    else:
        src = path

    # Inspeccionar
    v_tracks, a_tracks, s_tracks = inspect_tracks(src)
    selection_warning_lines: List[str] = []

    manual_audio_ids = [str(track_id) for track_id in keep_audio_ids if str(track_id).strip()]
    if manual_audio_ids:
        audio_resolution = resolve_manual_selection_by_signatures(
            a_tracks,
            keep_audio_signatures,
            audio_default_id,
            audio_default_signature,
            is_subtitle=False,
        )
        if audio_resolution is not None:
            selected_audio_tracks = list(audio_resolution["tracks"])
            a_ids, auto_sub_ids, audio_default_id, auto_sub_default_id = select_tracks_with_audio_selection(
                selected_audio_tracks,
                s_tracks,
                str(audio_resolution["default_id"] or ""),
            )
            if audio_resolution.get("remapped"):
                logging.info(
                    "%s: audios reasignados por firma compatible -> %s",
                    src.name,
                    summarize_audio_selection(a_tracks, [str(track_id) for track_id in a_ids], audio_default_id),
                )
            missing_audio_signatures = list(audio_resolution.get("missing_signatures") or [])
            if missing_audio_signatures:
                selection_warning_lines.append(
                    "Faltaron algunos audios seleccionados al resolver el caso especial por firma."
                )
                selection_warning_lines.append(
                    f"Audios no encontrados: {summarize_signature_selection(missing_audio_signatures)}."
                )
        else:
            a_ids, auto_sub_ids, audio_default_id, auto_sub_default_id = select_tracks_for_audio_ids(
                a_tracks,
                s_tracks,
                manual_audio_ids,
                audio_default_id,
            )
        if not a_ids:
            warning_lines = [
                f"Archivo: {src.name}",
                "La selección manual de audios no coincide con ninguna pista del archivo.",
                f"IDs solicitados: {', '.join(manual_audio_ids)}.",
            ]
            if keep_audio_signatures:
                warning_lines.append(
                    f"Firmas solicitadas: {summarize_signature_selection(keep_audio_signatures)}."
                )
            logging.error("%s: selección manual inválida de audios -> %s", src.name, ", ".join(manual_audio_ids))
            return None, "error", warning_lines

        logging.info(
            "%s: audios conservados (selección manual) -> %s",
            src.name,
            summarize_audio_selection(a_tracks, [str(track_id) for track_id in a_ids], audio_default_id),
        )
    else:
        # Selección automática fast
        a_ids, auto_sub_ids, audio_default_id, auto_sub_default_id = select_tracks_fast(a_tracks, s_tracks)
        logging.info(
            "%s: audios conservados -> %s",
            src.name,
            summarize_audio_selection(a_tracks, [str(track_id) for track_id in a_ids], audio_default_id),
        )

    manual_sub_ids = [str(track_id) for track_id in keep_subtitle_ids if str(track_id).strip()]
    if manual_subtitle_selection:
        subtitle_resolution = resolve_manual_selection_by_signatures(
            s_tracks,
            keep_subtitle_signatures,
            subtitle_default_id,
            subtitle_default_signature,
            is_subtitle=True,
        )
        if subtitle_resolution is not None:
            s_ids = [str(track_id) for track_id in subtitle_resolution["ids"]]
            sub_default_id = str(subtitle_resolution["default_id"] or "")
            if subtitle_resolution.get("remapped"):
                logging.info(
                    "%s: subtítulos reasignados por firma compatible -> %s",
                    src.name,
                    summarize_subtitle_selection(s_tracks, [str(track_id) for track_id in s_ids], sub_default_id),
                )
            missing_subtitle_signatures = list(subtitle_resolution.get("missing_signatures") or [])
            if missing_subtitle_signatures:
                selection_warning_lines.append(
                    "Faltaron algunos subtítulos seleccionados al resolver el caso especial por firma."
                )
                selection_warning_lines.append(
                    f"Subtítulos no encontrados: {summarize_signature_selection(missing_subtitle_signatures)}."
                )
        else:
            s_ids, sub_default_id = select_subtitles_for_ids(s_tracks, manual_sub_ids, subtitle_default_id)
        if manual_sub_ids and not s_ids:
            warning_lines = [
                f"Archivo: {src.name}",
                "La selección manual de subtítulos no coincide con ninguna pista del archivo.",
                f"IDs solicitados: {', '.join(manual_sub_ids)}.",
            ]
            if keep_subtitle_signatures:
                warning_lines.append(
                    f"Firmas solicitadas: {summarize_signature_selection(keep_subtitle_signatures)}."
                )
            logging.error("%s: selección manual inválida de subtítulos -> %s", src.name, ", ".join(manual_sub_ids))
            return None, "error", warning_lines

        logging.info(
            "%s: subtítulos conservados (selección manual) -> %s",
            src.name,
            summarize_subtitle_selection(s_tracks, [str(track_id) for track_id in s_ids], sub_default_id),
        )
    else:
        s_ids, sub_default_id = auto_sub_ids, auto_sub_default_id

    allow_delete_originals = delete_originals and len(a_tracks) > 1
    if delete_originals and not allow_delete_originals:
        logging.info(
            "%s: se omite eliminar el original porque el archivo solo tiene una pista de audio.",
            src.name,
        )

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
        audio_default_id,
        sub_default_id,
        progress_cb=progress_cb,
    )
    warning_lines = list(selection_warning_lines)
    warning_lines.extend(spanish_subtitle_warning(src.name, a_tracks, s_tracks, s_ids))
    if not ok:
        return None, "error", warning_lines

    if a_tracks:
        _, output_audio_tracks, _ = inspect_tracks(dst)
        if not output_audio_tracks:
            logging.warning(
                "%s: la salida quedó sin audio. Reintentando en modo seguro conservando todos los audios del origen.",
                src.name,
            )
            try:
                dst.unlink()
            except OSError:
                pass

            fallback_audio_ids = [t["id"] for t in a_tracks]
            fallback_audio_default_id = (
                next((_track_id(t) for t in a_tracks if t.get("default")), None)
                or _track_id(a_tracks[0])
            )
            ok = run_mkvmerge(
                src,
                dst,
                [t["id"] for t in v_tracks],
                fallback_audio_ids,
                s_ids,
                fallback_audio_default_id,
                sub_default_id,
                progress_cb=progress_cb,
            )
            if not ok:
                return None, "error", warning_lines

            _, output_audio_tracks, _ = inspect_tracks(dst)
            if not output_audio_tracks:
                warning_lines = list(warning_lines)
                warning_lines.append(
                    "La validación final detectó una salida sin audio; se abortó el archivo."
                )
                logging.error("%s: validación final falló; la salida sigue sin audio.", src.name)
                try:
                    dst.unlink()
                except OSError:
                    pass
                return None, "error", warning_lines

            logging.info(
                "%s: reintento de seguridad OK; se conservaron todos los audios del origen.",
                src.name,
            )

    if allow_delete_originals and src.exists() and src.parent == originals:
        try:
            src.unlink()
            try:
                originals.rmdir()
            except OSError:
                pass
        except OSError as exc:
            logging.warning("No se pudo eliminar el original movido %s: %s", src, exc)

    return dst, "filtrado", warning_lines


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(format="[%(levelname)s] %(message)s", level=level)


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Filtra pistas como el script base y luego renombra metadatos.\n"
            "- Filtrado: conserva ES/EN, respeta el audio default original y evita salidas sin audio.\n"
            "- Renombrado: normaliza language y nombra pistas con la marca."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("ruta", nargs="?", default=".", help="Archivo o carpeta a procesar")
    parser.add_argument("-b", "--brand", default=DEFAULT_BRAND, help="Texto de marca a añadir, p.ej. 'GDriveLatinoHD'")
    parser.add_argument("-v", "--verbose", action="store_true", help="Salida detallada")
    parser.add_argument(
        "--file-in-use-action",
        choices=["skip", "close-qbittorrent", "remove-torrent", "remove-torrent-and-data"],
        default=DEFAULT_LOCK_ACTION,
        help=(
            "Acción cuando el archivo está bloqueado (WinError 32). "
            "Por defecto cierra qBittorrent."
        ),
    )
    parser.add_argument(
        "--lock-retry-seconds",
        type=int,
        default=DEFAULT_LOCK_RETRY_SECONDS,
        help="Segundos de espera tras desbloqueo antes de reintentar mover el archivo.",
    )
    parser.add_argument(
        "--qb-url",
        default=DEFAULT_QB_URL,
        help="URL WebUI de qBittorrent para acciones remove-torrent.",
    )
    parser.add_argument("--qb-user", default=None, help="Usuario WebUI de qBittorrent.")
    parser.add_argument("--qb-pass", default=None, help="Password WebUI de qBittorrent.")
    parser.add_argument(
        "--delete-originals",
        action="store_true",
        help="Elimina los archivos originales movidos a la carpeta ORIGINAL cuando el filtrado termina bien.",
    )
    parser.add_argument(
        "--keep-audio-ids",
        default=None,
        help="Lista de track IDs de audio a conservar, separada por comas. Si se omite, se usa la selección automática.",
    )
    parser.add_argument(
        "--audio-default-id",
        default=None,
        help="Track ID de audio que debe quedar como predeterminado dentro de la selección conservada.",
    )
    parser.add_argument(
        "--keep-audio-signatures",
        default=None,
        help="Payload base64/json con firmas estables de audio para resolver lotes con layouts mixtos.",
    )
    parser.add_argument(
        "--audio-default-signature",
        default=None,
        help="Payload base64/json con la firma del audio que debe quedar como predeterminado.",
    )
    parser.add_argument(
        "--keep-subtitle-ids",
        default=None,
        help="Lista de track IDs de subtítulos a conservar, separada por comas. Usa __none__ para quitar todos explícitamente.",
    )
    parser.add_argument(
        "--subtitle-default-id",
        default=None,
        help="Track ID de subtítulo que debe quedar como predeterminado dentro de la selección conservada.",
    )
    parser.add_argument(
        "--keep-subtitle-signatures",
        default=None,
        help="Payload base64/json con firmas estables de subtítulos para resolver lotes con layouts mixtos.",
    )
    parser.add_argument(
        "--subtitle-default-signature",
        default=None,
        help="Payload base64/json con la firma del subtítulo que debe quedar como predeterminado.",
    )
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
    keep_audio_ids = parse_keep_track_ids(args.keep_audio_ids)
    keep_subtitle_ids = parse_keep_track_ids(args.keep_subtitle_ids)
    audio_default_id = (args.audio_default_id or "").strip() or None
    subtitle_default_id = (args.subtitle_default_id or "").strip() or None
    keep_audio_signatures = parse_signature_payload(args.keep_audio_signatures)
    keep_subtitle_signatures = parse_signature_payload(args.keep_subtitle_signatures)
    parsed_audio_default_signatures = parse_signature_payload(args.audio_default_signature)
    parsed_subtitle_default_signatures = parse_signature_payload(args.subtitle_default_signature)
    audio_default_signature = parsed_audio_default_signatures[0] if parsed_audio_default_signatures else None
    subtitle_default_signature = parsed_subtitle_default_signatures[0] if parsed_subtitle_default_signatures else None
    manual_subtitle_selection = args.keep_subtitle_ids is not None

    ok_total = 0
    for idx, f in enumerate(files, start=1):
        name = f.name
        print(f"[{idx}/{total_files}] Iniciando filtrado: {name}", flush=True)
        reported_milestones = set()

        def _mux_progress(pct: int):
            pct = max(0, min(100, int(pct)))
            for milestone in PROGRESS_MILESTONES:
                if pct >= milestone and milestone not in reported_milestones:
                    reported_milestones.add(milestone)
                    print(f"[{idx}/{total_files}] Mux {milestone:>3}%: {name}", flush=True)

        dst, status, warning_lines = process_video(
            f,
            workdir=workdir,
            output_in_root=output_in_root,
            file_in_use_action=args.file_in_use_action,
            keep_audio_ids=keep_audio_ids,
            audio_default_id=audio_default_id,
            keep_audio_signatures=keep_audio_signatures,
            audio_default_signature=audio_default_signature,
            keep_subtitle_ids=keep_subtitle_ids,
            subtitle_default_id=subtitle_default_id,
            keep_subtitle_signatures=keep_subtitle_signatures,
            subtitle_default_signature=subtitle_default_signature,
            manual_subtitle_selection=manual_subtitle_selection,
            delete_originals=args.delete_originals,
            lock_retry_seconds=max(1, int(args.lock_retry_seconds)),
            qb_url=args.qb_url,
            qb_user=args.qb_user,
            qb_pass=args.qb_pass,
            progress_cb=_mux_progress,
        )
        if status != "filtrado" or not dst:
            print(f"[{idx}/{total_files}] Error filtrando: {name}", flush=True)
            if warning_lines:
                _format_warning_panel("Advertencia de subtítulos", warning_lines)
            print(
                f"[Global] {idx}/{total_files} procesados ({int((idx / total_files) * 100)}%) | OK {ok_total}",
                flush=True,
            )
            continue

        if warning_lines:
            _format_warning_panel("Advertencia de subtítulos", warning_lines)

        # Renombrar metadatos (solo MKV/WEBM)
        rename_ok = True
        if dst.suffix.lower() in {".mkv", ".webm"}:
            print(f"[{idx}/{total_files}] Renombrando metadatos: {dst.name}", flush=True)
            rename_ok = mkvpropedit_rename(dst, brand=args.brand, set_title=True, verbose=False)
        else:
            logging.info("%s: contenedor no Matroska; se omitió renombrado de metadatos.", dst.name)
        ok_total += 1 if rename_ok else 0

        print(
            (
                f"[{idx}/{total_files}] OK: {dst.name}"
                if rename_ok
                else f"[{idx}/{total_files}] ERROR metadatos: {dst.name}"
            ),
            flush=True,
        )
        print(
            f"[Global] {idx}/{total_files} procesados ({int((idx / total_files) * 100)}%) | OK {ok_total}",
            flush=True,
        )

    print(f"Completado: {ok_total}/{total_files} archivos OK.")


if __name__ == "__main__":
    main()


