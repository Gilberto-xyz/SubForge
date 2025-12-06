#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Extraer_subtitulos_multi.py (PGS-aware)
Versión 2.1 - 13-sep-2025

- Extrae todos los subtítulos de archivos .mkv en el directorio actual.
- Respeta el formato original (ASS → .ass | SubRip → .srt).
- PGS (hdmv_pgs_subtitle) se extrae a .sup y se convierte a .srt con Subtitle Edit (OCR tesseract).
"""

import subprocess
import os
import json
from collections import defaultdict

VIDEO_EXTS = {'.mkv','.mp4'}
FFPROBE_FIELDS = 'stream=index,codec_type,codec_name:stream_tags=language'


def _run(cmd):
    """Ejecuta un comando y devuelve (returncode, stdout, stderr)."""
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.returncode, result.stdout.decode(errors='ignore'), result.stderr.decode(errors='ignore')


def _have_subtitle_edit():
    """Devuelve True si 'SubtitleEdit' está disponible en PATH."""
    try:
        result = subprocess.run(["SubtitleEdit", "/help"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return result.returncode in (0, 1)
    except FileNotFoundError:
        return False


def _ocr_sup_to_srt(sup_path, srt_path):
    """Convierte un .sup a .srt usando Subtitle Edit (OCR tesseract)."""
    return _run([
        'SubtitleEdit', '/convert', sup_path, 'SubRip',
        f'/outputfilename:{srt_path}', '/overwrite', '/ocrengine:tesseract'
    ])


def scan_directory(path):
    """
    Escanea el directorio y devuelve:
      - idiomas_disponibles: lista ordenada de códigos ISO únicos
      - streams_info: [(archivo, index, iso, codec_name), ...]
    """
    idiomas, streams = set(), []

    for f in os.listdir(path):
        if os.path.splitext(f)[1].lower() not in VIDEO_EXTS:
            continue

        code, out, err = _run([
            'ffprobe', '-v', 'error',
            '-select_streams', 's',
            '-show_entries', FFPROBE_FIELDS,
            '-of', 'json', os.path.join(path, f)
        ])
        if code != 0:
            print(f"!!  ffprobe falló en '{f}': {err.strip()}")
            continue

        try:
            data = json.loads(out)
            for s in data.get('streams', []):
                if s.get('codec_type') != 'subtitle':
                    continue
                idx = s['index']
                codec = s.get('codec_name', 'unknown')
                lang = s.get('tags', {}).get('language', 'und')
                idiomas.add(lang)
                streams.append((f, idx, lang, codec))
        except json.JSONDecodeError:
            print(f"!!  No se pudo leer la salida de ffprobe para '{f}'.")

    return sorted(idiomas), streams


def extract_subs(path, streams, idiomas_sel):
    out_dir = os.path.join(path, 'subtitulos_extraidos')
    os.makedirs(out_dir, exist_ok=True)

    ok, fail = 0, 0
    errores = defaultdict(list)

    for file_name, idx, lang, codec in streams:
        if lang not in idiomas_sel:
            continue

        base = os.path.splitext(file_name)[0]

        # Texto directo (ASS/SRT): copiar sin cambios
        if codec == 'ass':
            ext, codec_opt = '.ass', 'copy'
            out_file = f"{base}_{lang}_sub{idx}{ext}"
            out_path = os.path.join(out_dir, out_file)
            code, _, err = _run([
                'ffmpeg', '-y', '-i', os.path.join(path, file_name),
                '-map', f'0:{idx}', '-c:s', codec_opt, out_path
            ])
            if code == 0 and os.path.isfile(out_path):
                ok += 1
                print(f"✔ Extraído {lang} | {codec.upper():<7} → {out_file}")
            else:
                fail += 1
                errores[file_name].append((idx, lang, codec))
                last = (err.strip().splitlines() or [''])[-1]
                print(f"✖ Error al extraer {lang} | {codec.upper()} de '{file_name}'.\n   ffmpeg: {last}")
            continue

        if codec in {'srt', 'subrip'}:
            ext, codec_opt = '.srt', 'copy'
            out_file = f"{base}_{lang}_sub{idx}{ext}"
            out_path = os.path.join(out_dir, out_file)
            code, _, err = _run([
                'ffmpeg', '-y', '-i', os.path.join(path, file_name),
                '-map', f'0:{idx}', '-c:s', codec_opt, out_path
            ])
            if code == 0 and os.path.isfile(out_path):
                ok += 1
                print(f"✔ Extraído {lang} | {codec.upper():<7} → {out_file}")
            else:
                fail += 1
                errores[file_name].append((idx, lang, codec))
                last = (err.strip().splitlines() or [''])[-1]
                print(f"✖ Error al extraer {lang} | {codec.upper()} de '{file_name}'.\n   ffmpeg: {last}")
            continue

        # PGS (imagen): extraer .sup y OCR a .srt
        if codec in {'hdmv_pgs_subtitle', 'pgssub'}:
            se_ok = _have_subtitle_edit()
            sup_file = f"{base}_{lang}_sub{idx}.sup"
            sup_path = os.path.join(out_dir, sup_file)
            srt_file = f"{base}_{lang}_sub{idx}.srt"
            srt_path = os.path.join(out_dir, srt_file)

            # 1) Extraer PGS → SUP
            code1, _, err1 = _run([
                'ffmpeg', '-y', '-analyzeduration', '200M', '-probesize', '200M',
                '-i', os.path.join(path, file_name),
                '-map', f'0:{idx}', '-c:s', 'copy', sup_path
            ])
            if code1 != 0 or not os.path.isfile(sup_path):
                fail += 1
                errores[file_name].append((idx, lang, codec))
                last = (err1.strip().splitlines() or [''])[-1]
                print(f"✖ Error al extraer (SUP) {lang} | {codec.upper()} de '{file_name}'.\n   ffmpeg: {last}")
                continue

            # 2) OCR SUP → SRT con Subtitle Edit
            if se_ok:
                code2, out2, err2 = _ocr_sup_to_srt(sup_path, srt_path)
                if code2 == 0 and os.path.isfile(srt_path):
                    ok += 1
                    print(f"✔ Extraído {lang} | PGS → SUP → SRT → {srt_file}")
                else:
                    fail += 1
                    errores[file_name].append((idx, lang, codec))
                    last = (err2.strip().splitlines() or out2.strip().splitlines() or [''])[-1]
                    print(f"⚠ Extraído .SUP pero falló OCR a SRT para '{file_name}' (stream {idx}).\n   SubtitleEdit: {last}\n   Archivo disponible: {sup_file}")
            else:
                ok += 1  # Se extrajo el .sup como mínimo
                print(f"⚠ Extraído .SUP {lang} (PGS) → {sup_file}. Instala/añade 'SubtitleEdit' al PATH para OCR a SRT.")
            continue

        # Otros formatos: intentar conversión a SRT con ffmpeg
        out_file = f"{base}_{lang}_sub{idx}.srt"
        out_path = os.path.join(out_dir, out_file)
        code, _, err = _run([
            'ffmpeg', '-y', '-i', os.path.join(path, file_name),
            '-map', f'0:{idx}', '-c:s', 'srt', out_path
        ])
        if code == 0 and os.path.isfile(out_path):
            ok += 1
            print(f"✔ Extraído {lang} | {codec.upper():<7} → {out_file}")
        else:
            fail += 1
            errores[file_name].append((idx, lang, codec))
            last = (err.strip().splitlines() or [''])[-1]
            print(f"✖ Error al extraer {lang} | {codec.upper()} de '{file_name}'.\n   ffmpeg: {last}")

    # Resumen
    print("\n-------- Resumen --------")
    print(f"Subtítulos extraídos correctamente: {ok}")
    if fail:
        print(f"Subtítulos con error: {fail}")
        for f, lst in errores.items():
            detalles = ', '.join(f"{i}:{l}" for i, l, _ in lst)
            print(f"  - {f}: {detalles}")


def seleccionar_idiomas(idiomas):
    print("Idiomas encontrados:")
    for i, lang in enumerate(idiomas, 1):
        print(f" {i}. {lang}")
    sel = input("Elige los numeros de los idiomas a extraer (ej. 1,3): ")
    nums = [int(x.strip()) for x in sel.split(',') if x.strip().isdigit()]
    return [idiomas[i - 1] for i in nums if 0 < i <= len(idiomas)]


if __name__ == '__main__':
    DIR = os.path.abspath(os.path.dirname(__file__))
    idiomas, streams = scan_directory(DIR)

    if not idiomas:
        print("No se encontraron subtítulos en los archivos .mkv del directorio.")
        raise SystemExit(0)

    idiomas_seleccionados = seleccionar_idiomas(idiomas)
    if not idiomas_seleccionados:
        print("No se seleccionó ningún idioma. Saliendo.")
        raise SystemExit(0)

    print(f"\n>> Iniciando extraccion para: {', '.join(idiomas_seleccionados)}\n")
    extract_subs(DIR, streams, idiomas_seleccionados)

