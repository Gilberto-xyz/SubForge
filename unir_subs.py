#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Uso:
    python Merge_Subs_v3.py [carpeta] [--overwrite] [--verbose] [--no-color] [--lang spa] [--debug-match] [--yes]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import subprocess
import sys
import re
from dataclasses import dataclass
from typing import Iterable, Optional, Tuple, List

MKVMERGE = r"C:\Program Files\MKVToolNix\mkvmerge.exe"

EXT_VIDEOS = {".mkv", ".mp4", ".avi", ".ts"}
EXT_SUBS = {".srt", ".ass"}
LANG_CODE = "spa"
OUTPUT_FOLDER = "muxeados"


@dataclass
class SubtitleTrack:
    path: pathlib.Path
    lang: str
    display: str
    default: bool = False


class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    CYAN = "\033[36m"


def _enable_colors_win():
    try:
        import colorama  # type: ignore
        colorama.just_fix_windows_console()
    except Exception:
        pass


def c(fmt: str, color: str, use_color: bool) -> str:
    return f"{color}{fmt}{Colors.RESET}" if use_color else fmt


def resolve_mkvmerge() -> Optional[str]:
    path = pathlib.Path(MKVMERGE)
    if path.is_file():
        return str(path)
    return shutil.which("mkvmerge")


def mkvmerge_disponible() -> bool:
    return resolve_mkvmerge() is not None


SPANISH_NAME_HINTS = (
    "spanish",
    "español",
    "espanol",
    "castellano",
    "latam",
    "latino",
    "mex",
    "méx",
    "mexico",
)


LANG_TOKEN_MAP = {
    "es": "spa",
    "es-419": "es-419",
    "spa": "spa",
    "latam": "spa",
    "latino": "spa",
    "castellano": "spa",
    "mx": "spa",
    "mex": "spa",
    "en": "eng",
    "eng": "eng",
    "english": "eng",
    "ing": "eng",
    "en-us": "eng",
    "en-gb": "eng",
    "en-uk": "eng",
    "pt": "por",
    "ptbr": "por",
    "pt-br": "por",
    "por": "por",
    "portugues": "por",
    "br": "por",
    "fr": "fra",
    "fre": "fra",
    "fra": "fra",
    "french": "fra",
    "it": "ita",
    "ita": "ita",
    "italian": "ita",
    "de": "deu",
    "ger": "deu",
    "deu": "deu",
    "german": "deu",
    "jp": "jpn",
    "ja": "jpn",
    "jpn": "jpn",
    "japanese": "jpn",
    "ru": "rus",
    "rus": "rus",
    "russian": "rus",
}

LANG_DISPLAY_MAP = {
    "spa": "Espanol",
    "es-419": "Espanol Latam",
    "eng": "English",
    "por": "Portugues",
    "fra": "Francais",
    "ita": "Italiano",
    "deu": "Deutsch",
    "jpn": "Japanese",
    "rus": "Russian",
    "und": "Sin idioma",
}

LANG_EXCLUDE_TOKENS = {
    "sub",
    "subs",
    "subtitulo",
    "subtitulos",
    "subtitle",
    "subtitles",
    "subt",
    "forced",
    "cc",
    "sdh",
    "sign",
    "song",
    "multi",
    "dual",
}


def _display_for_lang(lang: str) -> str:
    return LANG_DISPLAY_MAP.get(lang, lang.upper())


def _normalize_lang_token(token: str) -> Optional[str]:
    token = token.strip().lower()
    if not token or token in LANG_EXCLUDE_TOKENS:
        return None
    token = token.strip("-_.")
    if not token or token in LANG_EXCLUDE_TOKENS:
        return None
    if token == "es419":
        token = "es-419"
    return token


def _guess_language_from_filename(path: pathlib.Path, fallback: str) -> tuple[str, str]:
    name = path.stem.lower()
    tokens = [t for t in re.split(r"[^a-z0-9\-]+", name) if t]
    for raw in reversed(tokens):
        norm = _normalize_lang_token(raw)
        if not norm:
            continue
        if norm in LANG_TOKEN_MAP:
            lang = LANG_TOKEN_MAP[norm]
            return lang, _display_for_lang(lang)
        if norm.startswith("es") and norm.replace("-", "") == "es419":
            lang = "es-419"
            return lang, _display_for_lang(lang)
    fallback_norm = _normalize_lang_token(fallback) or fallback or "und"
    if fallback_norm in LANG_TOKEN_MAP:
        lang = LANG_TOKEN_MAP[fallback_norm]
    else:
        lang = fallback_norm
    return lang, _display_for_lang(lang)


def _is_spanish(lang: str | None, name: str | None) -> bool:
    l = (lang or "und").lower().strip()
    n = (name or "").lower()
    if l.startswith("es") or l == "spa":
        return True
    return any(h in n for h in SPANISH_NAME_HINTS)


def _extrae_tracks(video: pathlib.Path) -> tuple[list[dict], list[dict]]:
    mkv = resolve_mkvmerge()
    assert mkv is not None
    try:
        out = subprocess.check_output([mkv, "-J", str(video)], text=True, encoding="utf-8")
        data = json.loads(out)
    except Exception:
        return ([], [])

    audios: list[dict] = []
    subs: list[dict] = []
    for t in data.get("tracks", []):
        props = t.get("properties", {})
        info = {
            "id": t.get("id"),
            "type": t.get("type"),
            "lang": props.get("language", "und"),
            "name": props.get("track_name", ""),
            "default": bool(props.get("default_track", 0)),
        }
        if info["type"] == "audio":
            audios.append(info)
        elif info["type"] in {"subtitles", "subtitle"}:
            subs.append(info)
    return audios, subs


def archivos_en_directorio(carpeta: pathlib.Path):
    for ruta in sorted(carpeta.iterdir()):
        if ruta.is_file() and ruta.suffix.lower() in EXT_VIDEOS:
            yield ruta


# ======= Análisis de título, año y episodio =======

_EP_PATTERNS = [
    r"\b(?:e|ep|epi|episode|episodio|capitulo|cap)\s*0*(\d{1,3})\b",
    r"\bS\s*0*(\d{1,2})\s*E\s*0*(\d{1,3})\b",  # SxxExx (el grupo 2 es el episodio)
    r"\bE\s*0*(\d{1,3})\b",
]

def _extract_episode_number(name: str) -> Optional[int]:
    s = name.lower().replace("_", " ")
    s = re.sub(r"[.\-]+", " ", s)
    for pat in _EP_PATTERNS:
        m = re.search(pat, s, flags=re.IGNORECASE)
        if m:
            try:
                ep = int(m.groups()[-1])
                return ep
            except Exception:
                continue
    # Casos como 'episodio 10.ts' en español con espacio
    m2 = re.search(r"\bepisodio\s+(\d{1,3})\b", s, flags=re.IGNORECASE)
    if m2:
        try:
            return int(m2.group(1))
        except Exception:
            pass
    return None


def _extract_year(text: str) -> Optional[int]:
    # Año explícito 19xx / 20xx
    m = re.search(r"\b(19|20)\d{2}\b", text)
    if m:
        return int(m.group(0))
    # Fecha 2019-10-04 / 20191004 / 2019.10.04
    m2 = re.search(r"\b((19|20)\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])\b", text)
    if m2:
        return int(m2.group(1))
    # YYMMDD -> inferir siglo
    m3 = re.search(r"\b(\d{2})(\d{2})(\d{2})\b", text)
    if m3:
        yy = int(m3.group(1))
        if 0 <= yy <= 23:
            return 2000 + yy
        if 90 <= yy <= 99:
            return 1900 + yy
    return None


def _normalize_tokens(name: str, keep_year: Optional[int] = None) -> list[str]:
    s = name.lower()

    # Quitar extensión
    s = re.sub(r"\.(srt|ass|mkv|mp4|avi|ts)$", "", s)

    # Quitar contenido entre [] y {} y la mayoría de () salvo el año
    s = re.sub(r"\[[^\]]*\]", " ", s)
    s = re.sub(r"\{[^}]*\}", " ", s)
    s = re.sub(r"\((?!19\d{2}|20\d{2})[^)]*\)", " ", s)

    # Normalizar separadores
    s = re.sub(r"[\._\-\+]+", " ", s)
    s = re.sub(r"[^0-9a-záéíóúüñ ]+", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip()

    STOP = {
        # codecs/res
        "x264","x265","h264","h265","hevc","avc","xvid","divx",
        "hdr","hdr10","hdr10plus","dv","dolby","vision",
        "2160p","1080p","720p","576p","480p","4k","8k","10bit","8bit",
        # fuentes
        "bluray","bdrip","brrip","webrip","web","webdl","web-dl","hdrip","dvdrip","remux","hdtv",
        # audio
        "dts","dtshd","truehd","aac","ac3","eac3","ddp","dd","5.1","7.1","atmos",
        # etiquetas
        "proper","repack","internal","limited","extended","remastered","imax",
        "amzn","nf","netflix","hmax","hbo","hulu","yts","rarbg","evo","utr","qman","next","odk",
        # idiomas/subs
        "eng","english","es","spa","es-419","latam","latino","castellano",
        "spanish","español","espanol","sub","subs","sub2","forced","sdh",
        # otros
        "sample","trailer","uhd","br","bd","end"
    }

    tokens = []
    year_str = str(keep_year) if keep_year is not None else None
    for tok in s.split(" "):
        t = tok.strip()
        if not t or t in STOP:
            continue

        # conservar 'e02' como token
        if re.match(r"^e\d{1,3}$", t):
            tokens.append(t)
            continue

        # eliminar números sueltos salvo el año
        if t.isdigit():
            if year_str and t == year_str:
                tokens.append(t)
            continue

        # saltar 1080p, 2160p, etc.
        if re.match(r"^\d{3,4}p$", t):
            continue

        tokens.append(t)

    return tokens


def _similarity(a_tokens: list[str], b_tokens: list[str]) -> float:
    from difflib import SequenceMatcher
    a = list(dict.fromkeys(a_tokens))
    b = list(dict.fromkeys(b_tokens))
    if not a or not b:
        return 0.0
    set_a, set_b = set(a), set(b)
    inter = len(set_a & set_b)
    union = len(set_a | set_b)
    jacc = inter / union if union else 0.0
    coverage = inter / max(1, min(len(set_a), len(set_b)))
    ratio = SequenceMatcher(None, " ".join(a), " ".join(b)).ratio()
    score = 0.6 * jacc + 0.4 * ratio + (0.1 if coverage >= 0.8 else 0.0)
    return score


def _episode_token(ep: Optional[int]) -> Optional[str]:
    return f"e{ep:02d}" if isinstance(ep, int) else None


def _subtitle_sort_key(path: pathlib.Path) -> tuple[int, str]:
    return (0 if path.suffix.lower() == ".ass" else 1, path.name.lower())


def busca_subtitulos(video: pathlib.Path, *, debug: bool=False) -> List[pathlib.Path]:
    stem = video.stem
    carpeta = video.parent
    subs_en_carpeta = [s for s in carpeta.iterdir() if s.is_file() and s.suffix.lower() in EXT_SUBS]

    # 1) Prefijo exacto
    prefijo = sorted([s for s in subs_en_carpeta if s.stem.startswith(stem)], key=_subtitle_sort_key)
    if prefijo:
        if debug:
            for candidato in prefijo:
                print(f"[debug] Prefijo exacto -> {video.name} => {candidato.name}")
        return prefijo

    # 2) Intentar por número de episodio
    ep_v = _extract_episode_number(stem)
    year_v = _extract_year(stem)
    v_tokens = _normalize_tokens(stem, keep_year=year_v)
    ep_tok = _episode_token(ep_v)
    if ep_tok and ep_tok not in v_tokens:
        v_tokens.append(ep_tok)

    seleccionados: list[tuple[float, pathlib.Path]] = []
    mejor: Optional[pathlib.Path] = None
    mejor_score = -1.0
    mejor_ep_match = False

    candidatos = subs_en_carpeta
    if ep_v is not None:
        filtrados = []
        for s in subs_en_carpeta:
            ep_s = _extract_episode_number(s.stem)
            if ep_s == ep_v:
                filtrados.append(s)
        if filtrados:
            candidatos = filtrados

    for s in candidatos:
        year_s = _extract_year(s.stem)
        s_tokens = _normalize_tokens(s.stem, keep_year=year_v or year_s)
        ep_s = _extract_episode_number(s.stem)
        ep_tok_s = _episode_token(ep_s)
        if ep_tok_s and ep_tok_s not in s_tokens:
            s_tokens.append(ep_tok_s)

        score = _similarity(v_tokens, s_tokens)

        # Bonus fuerte por episodio igual
        if ep_v is not None and ep_s == ep_v:
            score += 0.35
        # Bonus leve por año igual
        if year_v and year_s and year_v == year_s:
            score += 0.15
        # Penalización por año distinto
        elif year_v and year_s and year_v != year_s:
            score -= 0.2

        if debug:
            print(f"[debug] {video.stem} vs {s.stem} -> score={score:.3f}  ep_v={ep_v} ep_s={ep_s} year_v={year_v} year_s={year_s}")

        if score > mejor_score or (abs(score - mejor_score) < 1e-6 and mejor is not None and s.suffix.lower() == ".ass"):
            mejor = s
            mejor_score = score
            mejor_ep_match = ep_v is not None and ep_s == ep_v

        ep_match = ep_v is not None and ep_s == ep_v
        umbral = 0.50 if ep_match else 0.60
        if score >= umbral:
            seleccionados.append((score, s))

    if seleccionados:
        seleccionados.sort(key=lambda item: (-item[0], _subtitle_sort_key(item[1])))
        if debug:
            nombres = ", ".join(p.name for _, p in seleccionados)
            print(f"[debug] Seleccionados ({len(seleccionados)}): {nombres}")
        return [item[1] for item in seleccionados]

    if mejor is not None:
        fallback_umbral = 0.45 if mejor_ep_match else 0.55
        if debug:
            print(f"[debug] Mejor candidato: {mejor.name} con score={mejor_score:.3f} (umbral fallback={fallback_umbral:.2f})")
        if mejor_score >= fallback_umbral:
            return [mejor]

    if debug:
        print(f"[debug] Sin coincidencias para {video.name}")
    return []


def _lang_suffix_for_filename(langs: Iterable[str]) -> str:
    vistos: set[str] = set()
    orden: list[str] = []
    for lang in langs:
        tag = (lang or "").strip().lower()
        if not tag:
            continue
        tag = tag.replace(":", "-")
        if tag not in vistos:
            vistos.add(tag)
            orden.append(tag)
    return "-".join(orden) if orden else "multi"


def genera_nombre_salida(video: pathlib.Path, carpeta_salida: pathlib.Path, langs: Iterable[str]) -> pathlib.Path:
    sufijo = _lang_suffix_for_filename(langs)
    nombre_base_salida = f"{video.stem} sub {sufijo}.mkv"
    return carpeta_salida / nombre_base_salida


def multiplexa(
    video: pathlib.Path,
    subs: List[SubtitleTrack],
    carpeta_salida: pathlib.Path,
    *,
    lang_code: str,
    verbose: bool,
    colors: bool,
    overwrite: bool,
    display: bool = True,
) -> tuple[bool, bool, Optional[pathlib.Path]]:
    if not subs:
        if display:
            print(c("[aviso] No hay subtitulos seleccionados", Colors.YELLOW, colors))
        return (True, False, None)

    salida = genera_nombre_salida(video, carpeta_salida, (track.lang or lang_code for track in subs))
    if salida.exists() and not overwrite:
        if display:
            print(c(f"[omitido] {salida.name} ya existe", Colors.DIM, colors))
        return (True, False, None)

    mkv_path = resolve_mkvmerge()
    assert mkv_path is not None

    a_tracks, s_tracks = _extrae_tracks(video)

    audio_es = [t for t in a_tracks if _is_spanish(t.get("lang"), t.get("name"))]
    audio_default_id = None
    if audio_es:
        latam = [t for t in audio_es if any(h in (t.get("name") or "").lower() for h in SPANISH_NAME_HINTS)]
        chosen = next((t for t in (latam or audio_es) if t.get("default")), (latam or audio_es)[0])
        audio_default_id = chosen.get("id")

    sub_ids_in_video = [t.get("id") for t in s_tracks]

    cmd: list[str] = [
        mkv_path, "--ui-language", "es", "-o", str(salida),
    ]

    if a_tracks or s_tracks:
        if audio_default_id is not None:
            for t in a_tracks:
                tid = str(t.get("id"))
                yes = "yes" if (t.get("id") == audio_default_id) else "no"
                cmd += ["--default-track-flag", f"{tid}:{yes}"]
        for sid in sub_ids_in_video:
            cmd += ["--default-track-flag", f"{sid}:no"]

    cmd.append(str(video))

    for track in subs:
        track_lang = track.lang or lang_code or "und"
        track_display = track.display or _display_for_lang(track_lang)
        default_flag = "yes" if track.default else "no"
        cmd += [
            "--language", f"0:{track_lang}",
            "--track-name", f"0:{track_display}",
            "--default-track-flag", f"0:{default_flag}",
            str(track.path),
        ]

    if display:
        detalles = ", ".join(
            f"{t.path.name} [{t.lang}{'*' if t.default else ''}]"
            for t in subs
        )
        print(c("-> Mux:", Colors.CYAN, colors), video.name, "+", detalles)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert proc.stdout is not None

    output_chunks: list[str] = []
    buffer = ""
    progress_re = re.compile(r"(\d{1,3})(?:\.\d+)?%")
    showed_progress = False

    def _handle_line(line: str) -> None:
        nonlocal showed_progress
        output_chunks.append(line)
        m = progress_re.search(line)
        if m:
            try:
                pct = float(m.group(1))
            except ValueError:
                pct = None
            if pct is not None:
                pct = min(100.0, max(0.0, pct))
                showed_progress = True
                print(f"\r  Progreso mux: {pct:5.1f}%".ljust(80), end="", flush=True)
            return
        if display and verbose:
            print(line.rstrip("\r\n"))
        elif verbose:
            print(line.rstrip("\r\n"))

    while True:
        chunk = proc.stdout.read(1)
        if chunk == "" and proc.poll() is not None:
            if buffer:
                _handle_line(buffer)
                buffer = ""
            break
        if not chunk:
            continue
        buffer += chunk
        if chunk in {"\n", "\r"}:
            _handle_line(buffer)
            buffer = ""

    proc.wait()
    if buffer:
        _handle_line(buffer)

    if showed_progress:
        print("\r" + " " * 120, end="\r", flush=True)

    output = "".join(output_chunks)
    had_warnings = proc.returncode == 1 or ("Advertencia:" in output or "Warning:" in output)

    log_path: Optional[pathlib.Path] = None
    if verbose and display:
        print(output.rstrip())
    else:
        if proc.returncode != 0:
            carpeta_salida.mkdir(exist_ok=True)
            log_path = carpeta_salida / f"{salida.stem}.log"
            try:
                log_path.write_text(output, encoding="utf-8", errors="replace")
            except Exception:
                log_path = None

    if proc.returncode == 0:
        if display:
            print(c("  ✓ Completado:", Colors.GREEN, colors), salida.name)
        return (True, False, None)
    elif proc.returncode == 1:
        warn_count = output.count("Advertencia:") + output.count("Warning:")
        msg = f"  ⚠ Completado con advertencias ({warn_count})."
        if log_path is None and not verbose:
            log_path = carpeta_salida / f"{salida.stem}.log"
            try:
                log_path.write_text(output, encoding="utf-8", errors="replace")
            except Exception:
                log_path = None
        if log_path:
            msg += f" Ver log: {log_path.name}"
        if display:
            print(c(msg, Colors.YELLOW, colors))
        return (True, True, log_path)
    else:
        msg = f"  ✗ Error: mkvmerge devolvió código {proc.returncode}"
        if log_path:
            msg += f" · Log: {log_path.name}"
        if display:
            print(c(msg, Colors.RED, colors))
        return (False, False, log_path)


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Multiplexa automáticamente subtítulos .srt/.ass con mkvmerge (v3 corregida)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("carpeta", nargs="?", default=".", help="Carpeta donde buscar videos y subtítulos")
    p.add_argument("--lang", default=LANG_CODE, help="Código de idioma para la pista de subtítulo (p. ej., spa)")
    p.add_argument("--overwrite", action="store_true", help="Sobrescribe archivos de salida si ya existen")
    p.add_argument("--verbose", action="store_true", help="Muestra la salida completa de mkvmerge")
    p.add_argument("--no-color", action="store_true", help="Desactiva colores en la salida")
    p.add_argument("--debug-match", action="store_true", help="Muestra cómo se empareja cada video con su subtítulo")
    p.add_argument("-y", "--yes", action="store_true", help="Acepta el mux sin solicitar confirmación")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv or sys.argv[1:])
    _enable_colors_win()
    use_colors = not args.no_color

    if not mkvmerge_disponible():
        sys.exit("No se encontró mkvmerge.exe. Añádelo al PATH o ajusta la variable MKVMERGE.")

    carpeta = pathlib.Path(args.carpeta).resolve()
    carpeta_salida = carpeta / OUTPUT_FOLDER
    carpeta_salida.mkdir(exist_ok=True)

    videos = list(archivos_en_directorio(carpeta))
    total = len(videos)

    ok = warn = errs = 0
    sin_subs = 0
    skip_user = 0
    sin_subs_list: list[str] = []
    skip_user_list: list[str] = []

    def _render_bar(done: int, total_items: int, *, width: int = 28) -> str:
        if total_items <= 0:
            return ""
        filled = int(width * done / total_items)
        bar = "█" * filled + "░" * (width - filled)
        pct = int(100 * done / total_items)
        return f"[{bar}] {done}/{total_items} ({pct}%)"

    def _clear_line() -> None:
        print("\r" + " " * 120, end="\r", flush=True)

    # Barra inicial
    print(_render_bar(0, total), end="", flush=True)

    for idx, video in enumerate(videos, start=1):
        subs_paths = busca_subtitulos(video, debug=args.debug_match)
        if subs_paths:
            tracks: List[SubtitleTrack] = []
            for sub_path in subs_paths:
                lang_code, display_name = _guess_language_from_filename(sub_path, args.lang or LANG_CODE)
                tracks.append(SubtitleTrack(path=sub_path, lang=lang_code, display=display_name))

            fallback_lang = (args.lang or LANG_CODE).lower()
            fallback_root = fallback_lang.split("-")[0] if fallback_lang else ""
            default_index = 0
            for pos, track in enumerate(tracks):
                lang_lower = (track.lang or "").lower()
                lang_root = lang_lower.split("-")[0] if lang_lower else ""
                if lang_lower == fallback_lang or (fallback_root and lang_root == fallback_root):
                    default_index = pos
                    break
            tracks[default_index].default = True

            _clear_line()
            print(c("Video:", Colors.BLUE, use_colors), video.name)
            print(c("  Subtitulos detectados:", Colors.DIM, use_colors))
            for track in tracks:
                marker = "*" if track.default else "-"
                print(f"    {marker} {track.path.name} [{track.lang}] {track.display}")

            proceed = True
            if not args.yes:
                respuesta = input("Deseas muxear estos subtitulos? [S/n]: ").strip().lower()
                if respuesta and respuesta not in {"s", "si", "y", "yes"}:
                    proceed = False

            if proceed:
                success, had_warnings, _ = multiplexa(
                    video, tracks, carpeta_salida,
                    lang_code=args.lang, verbose=args.verbose, colors=use_colors,
                    overwrite=args.overwrite, display=args.verbose
                )
                if success and had_warnings:
                    ok += 1
                    warn += 1
                elif success:
                    ok += 1
                else:
                    errs += 1
            else:
                skip_user += 1
                skip_user_list.append(video.name)
                print(c("  Operacion cancelada por el usuario.", Colors.DIM, use_colors))
        else:
            if args.verbose or args.debug_match:
                _clear_line()
                print(c(f"[aviso] No encontre subtitulos (.srt/.ass) para: {video.name}", Colors.YELLOW, use_colors))
            sin_subs += 1
            sin_subs_list.append(video.name)

        progreso = _render_bar(idx, total)
        print("\r" + progreso + f"  ok:{ok} warn:{warn} err:{errs} sin:{sin_subs} skip:{skip_user}", end="", flush=True)

    print()  # cerrar barra
    print()
    print(c("Resumen:", Colors.BOLD, use_colors))
    print("  Videos encontrados:", total)
    print(c("  Exitos:", Colors.GREEN, use_colors), ok)
    if warn:
        print(c("  Con advertencias:", Colors.YELLOW, use_colors), warn)
    if errs:
        print(c("  Errores:", Colors.RED, use_colors), errs)
    if sin_subs:
        print(c("  Sin subtitulos:", Colors.DIM, use_colors), sin_subs)
        for name in sin_subs_list:
            print(c(f"    - {name}", Colors.DIM, use_colors))
    if skip_user:
        print(c("  Omitidos por usuario:", Colors.DIM, use_colors), skip_user)
        for name in skip_user_list:
            print(c(f"    - {name}", Colors.DIM, use_colors))


if __name__ == "__main__":
    main()
