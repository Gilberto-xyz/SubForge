# SubForge

Colección de scripts para preparar videos (MKV/MP4), limpiar pistas y multiplexar subtítulos.

## Estructura actual

Scripts canónicos (nuevos):

- `limpiar_tracks.py`: flujo principal para filtrar pistas y normalizar metadatos.
- `convertir_mp4_a_mkv.py`: convierte MP4 a MKV aplicando reglas similares de selección/renombrado.
- `unir_subs.py`: agrega subtítulos externos (`.srt`/`.ass`) a videos existentes.
- `extraer_subtitulos.py`: extrae subtítulos internos y soporta PGS (`.sup`) con OCR opcional.

## Requisitos

- Python 3.10+ (recomendado).
- MKVToolNix (`mkvmerge`, `mkvpropedit`) para `limpiar_tracks.py`, `convertir_mp4_a_mkv.py`, `unir_subs.py`.
- FFmpeg (`ffmpeg`, `ffprobe`) para `extraer_subtitulos.py`.
- Opcional: `SubtitleEdit` + Tesseract (en PATH) para OCR de subtítulos PGS a SRT.
- Opcional: `tqdm`, `colorama` (mejor salida visual).

## Qué hace cada script

### 1) `limpiar_tracks.py` (principal)

Uso recomendado cuando ya tienes archivos de video y quieres dejarlos limpios y con metadatos consistentes.

- Procesa: `.mkv`, `.mp4`, `.webm`, `.avi`.
- Mueve el original a carpeta `ORIGINAL`.
- Genera versión filtrada (nombre con ` (filtered)`), en raíz o carpeta `filtrados`.
- Renombra metadatos de audio/subs y título de segmento con tu marca (`--brand`).
- Prioriza audio/subtítulos en español según su heurística interna.

Comandos:

```bash
python .\limpiar_tracks.py
python .\limpiar_tracks.py "D:\Videos\Anime" -b "GDriveLatinoHD"
python .\limpiar_tracks.py "archivo.mkv" -v
```

### 2) `convertir_mp4_a_mkv.py`

Para convertir MP4 a MKV y aplicar reglas de pistas/metadatos durante la conversión.

- Procesa solo `.mp4`.
- Crea salida `.mkv` (sufijo `WEB-DL`) en raíz o carpeta `mux_mp4_mkv`.
- Si termina bien, elimina el MP4 original.
- También aplica renombrado de metadatos.

Comandos:

```bash
python .\convertir_mp4_a_mkv.py
python .\convertir_mp4_a_mkv.py "D:\MP4s" -b "GDriveLatinoHD"
python .\convertir_mp4_a_mkv.py "video.mp4" -v
```

### 3) `unir_subs.py`

Para pegar subtítulos externos (`.srt`/`.ass`) a videos.

- Busca videos y subtítulos en la carpeta objetivo.
- Empareja por nombre/episodio con heurística.
- Muxea a carpeta `muxeados`.
- Por defecto pide confirmación por video (`-y` para automático).

Comandos:

```bash
python .\unir_subs.py
python .\unir_subs.py "D:\Temporada" --lang spa -y
python .\unir_subs.py "D:\Temporada" --debug-match --verbose
```

### 4) `extraer_subtitulos.py`

Para extraer subtítulos internos de videos.

- Extrae subtítulos por idioma seleccionado.
- Copia ASS/SRT; convierte formatos no-texto cuando puede.
- Para PGS (`hdmv_pgs_subtitle`): extrae `.sup` y, si existe `SubtitleEdit`, intenta OCR a `.srt`.
- Guarda en `subtitulos_extraidos`.

Comando:

```bash
python .\extraer_subtitulos.py
```

Nota: este script escanea la carpeta donde está el script.

## Flujo recomendado

Si solo quieres un flujo estable y simple:

1. Usa `limpiar_tracks.py` como herramienta principal.
2. Usa `unir_subs.py` solo cuando tengas subtítulos externos aparte.
3. Usa `extraer_subtitulos.py` solo si necesitas sacar subtítulos internos.
4. Usa `convertir_mp4_a_mkv.py` cuando tu fuente sea MP4 y quieras estandarizar a MKV.

## Consejos de seguridad

- Haz una prueba primero con 1 archivo.
- `convertir_mp4_a_mkv.py` borra el MP4 original cuando el proceso termina OK.
- `limpiar_tracks.py` mueve originales a `ORIGINAL`; revisa espacio en disco.
