# -*- coding: utf-8 -*-
"""Descargador de Música — versión 1.0.0 beta.

Aplicación independiente (no complemento de NVDA), accesible con NVDA.
Busca canciones en YouTube, acepta URL (canción o playlist), reproduce
la canción completa dentro de la propia aplicación (con control de
volumen) y descarga en audio o vídeo con opciones de formato, calidad
y carpeta. Incorpora yt-dlp y ffmpeg dentro del mismo .exe.
"""

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

import wx

APP_NAME = "Descargador de Música"
VERSION = "1.0.0 beta"
SEARCH_LIMIT = 10

FORMATOS = [
    ("m4a", "M4A"),
    ("opus", "OPUS"),
    ("mp3", "MP3"),
    ("wav", "WAV"),
    ("vorbis", "OGG"),
    ("webm", "WEBM"),
]
FORMAT_CODES = [c for c, _ in FORMATOS]

CALIDADES = [
    ("128K", "128 kbps"),
    ("192K", "192 kbps"),
    ("256K", "256 kbps"),
    ("320K", "320 kbps"),
]

DEFAULT_FORMAT = "m4a"
DEFAULT_CALIDAD = "192K"
DEFAULT_VOLUMEN = 80


# ── Rutas y configuración ──────────────────────────────────────
def app_dir():
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def exe_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def candidate_dirs():
    dirs = [app_dir()]  # versión integrada primero (siempre sana)
    if getattr(sys, "frozen", False):
        dirs.append(exe_dir())  # copia externa junto al exe (solo para actualizar)
    extras = []
    for d in dirs:
        extras.append(os.path.join(d, "recursos"))
    return dirs + extras


def find_ytdlp():
    for d in candidate_dirs():
        local = os.path.join(d, "yt-dlp.exe")
        if os.path.exists(local):
            return local
    return shutil.which("yt-dlp")


def find_ffmpeg():
    for d in candidate_dirs():
        local = os.path.join(d, "ffmpeg.exe")
        if os.path.exists(local):
            return local
    return shutil.which("ffmpeg")


def ensure_external_ytdlp():
    if not getattr(sys, "frozen", False):
        return find_ytdlp()
    ext = os.path.join(exe_dir(), "yt-dlp.exe")
    if os.path.exists(ext):
        return ext
    bundled = os.path.join(app_dir(), "yt-dlp.exe")
    if os.path.exists(bundled):
        try:
            shutil.copyfile(bundled, ext)
            return ext
        except Exception:
            return bundled
    return find_ytdlp()


def config_dir():
    base = os.path.join(os.environ.get("APPDATA", ""), "DescargaMusica")
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        pass
    return base


def config_path():
    return os.path.join(config_dir(), "config.json")


def load_config():
    cfg = {
        "carpeta": os.path.join(os.path.expanduser("~"), "Downloads", "Descargador de Música"),
        "formato": DEFAULT_FORMAT,
        "calidad": DEFAULT_CALIDAD,
        "volumen": DEFAULT_VOLUMEN,
        "espanol": True,
    }
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k in cfg:
                if k == "volumen":
                    if isinstance(data.get(k), (int, float)):
                        cfg[k] = int(data[k])
                elif k == "espanol":
                    if isinstance(data.get(k), bool):
                        cfg[k] = data[k]
                elif isinstance(data.get(k), str) and data[k].strip():
                    cfg[k] = data[k].strip()
    except Exception:
        pass
    if cfg["formato"] not in FORMAT_CODES:
        cfg["formato"] = DEFAULT_FORMAT
    if cfg["calidad"] not in [c for c, _ in CALIDADES]:
        cfg["calidad"] = DEFAULT_CALIDAD
    cfg["volumen"] = max(0, min(100, cfg.get("volumen", DEFAULT_VOLUMEN)))
    cfg["espanol"] = bool(cfg.get("espanol", True))
    if os.path.basename(cfg["carpeta"]) == "DescargasMúsica":
        cfg["carpeta"] = os.path.join(os.path.expanduser("~"), "Downloads", "Descargador de Música")
    return cfg


def save_config(cfg):
    try:
        with open(config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ── Herramientas de yt-dlp ─────────────────────────────────────
def cookies_path():
    """Busca cookies.txt junto al programa o en la carpeta de configuración."""
    for d in (exe_dir(), config_dir()):
        c = os.path.join(d, "cookies.txt")
        if os.path.exists(c):
            return c
    return None


def actualizar_externo():
    """Actualiza la copia externa de yt-dlp de forma segura: baja la nueva
    versión a un archivo temporal, la comprueba con -V y solo entonces la
    coloca junto al programa con reemplazo atómico. Nunca toca la copia
    integrada (que es la que se usa para descargar), de modo que un fallo
    en la comprobación no deja nunca el descargador en mal estado.
    Devuelve (rc, texto)."""
    externo = ensure_external_ytdlp()
    integrado = os.path.join(app_dir(), "yt-dlp.exe")
    if not os.path.exists(integrado):
        integrado = None
    origen = externo if (externo and os.path.exists(externo)) else integrado
    if not origen:
        return -1, "No se encontró yt-dlp."
    temporal = os.path.join(tempfile.gettempdir(),
                            "ytdlp_tmp_%d_%d.exe" % (os.getpid(), int(time.time() * 1000)))
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        shutil.copyfile(origen, temporal)
        proc = subprocess.run([temporal, "-U"],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", creationflags=creationflags, timeout=180,
                              stdin=subprocess.DEVNULL)
        out = (proc.stdout + "\n" + proc.stderr).strip()
        rc = proc.returncode
        if rc == 0:
            ver = subprocess.run([temporal, "-V"],
                                 capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", creationflags=creationflags, timeout=60,
                                 stdin=subprocess.DEVNULL)
            if ver.returncode != 0:
                out += "\nNo se pudo verificar la actualización."
                rc = -1
            else:
                destino = externo if externo else os.path.join(exe_dir(), "yt-dlp.exe")
                try:
                    os.makedirs(os.path.dirname(destino) or ".", exist_ok=True)
                    os.replace(temporal, destino)
                    temporal = None
                except Exception as e:
                    out += "\nNo se pudo escribir la copia junto al programa: %s" % e
                    rc = -1
        return rc, out
    except Exception as e:
        return -1, str(e)
    finally:
        if temporal and os.path.exists(temporal):
            try:
                os.remove(temporal)
            except Exception:
                pass


def _run_ydl(args):
    exe = find_ytdlp()
    if not exe:
        raise RuntimeError("No se encontró yt-dlp.")
    flags = ["--no-warnings", "--newline", "--age-limit", "100"]
    c = cookies_path()
    if c:
        flags += ["--cookies", c]
    ffmpeg = find_ffmpeg()
    if ffmpeg:
        flags.append("--ffmpeg-location")
        flags.append(os.path.dirname(ffmpeg))
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(
        [exe] + flags + args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
        timeout=None,
        stdin=subprocess.DEVNULL,
    )
    return proc.returncode, proc.stdout, proc.stderr


def parse_duration(sec):
    try:
        sec = int(sec) or 0
        return "%d:%02d" % (sec // 60, sec % 60)
    except Exception:
        return "¿:??"


def _small_error(err):
    m = re.search(r"ERROR:\s*(.+)", err or "")
    return m.group(1).strip() if m else (err or "").strip()


# Mensajes de yt-dlp más comunes traducidos al español.
TRADUCCIONES = [
    ("Current version:", "Versión actual:"),
    ("Latest version:", "Última versión:"),
    ("Checking for update", "Comprobando si hay actualización"),
    ("Updating to version", "Actualizando a la versión"),
    ("Updated yt-dlp to version", "yt-dlp actualizado a la versión"),
    ("yt-dlp is up to date", "yt-dlp ya está actualizado (última versión instalada)"),
    ("This video is unavailable", "Este vídeo no está disponible"),
    ("Video unavailable", "Vídeo no disponible"),
    ("Sign in to confirm you're not a bot",
     "YouTube pide iniciar sesión para confirmar que no eres un robot"),
    ("Sign in to confirm your age",
     "YouTube pide iniciar sesión para confirmar la edad"),
    ("Unable to extract", "No se pudo extraer"),
    ("HTTP Error 429", "Error del servidor 429: YouTube limita las peticiones, inténtalo más tarde"),
    ("Requested format is not available", "El formato pedido no está disponible"),
    ("ffmpeg is not installed", "ffmpeg no está disponible"),
    ("Private video", "Vídeo privado"),
    ("Members only video", "Vídeo solo para miembros"),
    ("The uploader has not made this video available",
     "El creador no ha hecho disponible este vídeo"),
    ("No video formats found", "No se encontraron formatos de vídeo"),
    ("is not a valid URL", "no es una URL válida"),
    ("Unsupported URL", "URL no compatible"),
    ("The playlist does not exist", "La lista de reproducción no existe"),
    ("No such file or directory", "No existe la carpeta indicada"),
    ("[DRM] The requested site is known to use DRM protection. It will NOT be supported.",
     "Este sitio usa protección DRM (contenido protegido) y no se puede descargar. Prueba con el enlace de la misma canción en YouTube."),
    ("Log in to your account", "Necesitas iniciar sesión en una cuenta para ver este contenido"),
    ("Sign in to your account", "Necesitas iniciar sesión en una cuenta para ver este contenido"),
    ("requires login", "requiere iniciar sesión"),
    ("This video is age-restricted",
     "Este vídeo tiene restricción de edad y YouTube pide una sesión iniciada"),
    ("Sign in to confirm you're not a robot",
     "YouTube pide iniciar sesión para confirmar que no eres un robot"),
    ("Enter a valid URL", "Escribe una URL válida"),
    ("Connection to", "No se pudo conectar con"),
]


def _sin_prompts(texto):
    """Quita las líneas interactivas de yt-dlp (pide pulsar Enter, etc.):
    solo las lee el lector de pantalla, no se muestran en pantalla."""
    if not texto:
        return texto
    out = []
    for ln in texto.splitlines():
        t = ln.strip().lower()
        if any(k in t for k in ("press enter", "pulse enter", "presiona enter",
                                "enter to continue", "continue? [y/n]",
                                "type 'y' or 'n'", "confirmation")):
            continue
        out.append(ln)
    return "\n".join(out).strip()


def traducir_ytdlp(texto, activo=True):
    """Traduce al español los fragmentos conocidos que habla yt-dlp."""
    if not activo or not texto:
        return texto
    texto = _sin_prompts(texto)
    t = texto
    for ing, esp in TRADUCCIONES:
        if ing in t:
            t = t.replace(ing, esp)
    return t


def _pack(entries):
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        entry = e.get("_flat") if isinstance(e.get("_flat"), dict) else e
        title = (entry.get("title") or "").strip()
        if not title:
            continue
        url = entry.get("url") or entry.get("webpage_url") or entry.get("original_url") or ""
        if url.startswith("/"):
            url = "https://www.youtube.com" + url
        out.append({
            "title": title,
            "duration": parse_duration(entry.get("duration") or 0),
            "channel": (entry.get("channel") or entry.get("uploader") or "").strip(),
            "url": url,
        })
    return out


def resolver_spotify(url):
    """Convierte un enlace de Spotify a una búsqueda de YouTube.

    Spotify no permite descargar sus canciones (protección DRM, igual que
    Netflix), así que se leen los datos de la canción desde la página de
    Spotify y se busca la misma canción en YouTube.
    """
    if "open.spotify.com" not in url.lower():
        return None
    m = re.search(r"/track/([A-Za-z0-9]+)", url)
    if not m:
        tipo = "álbum" if "album/" in url else ("lista" if "playlist/" in url else "Spotify")
        raise RuntimeError(
            "Spotify usa protección DRM y no deja descargar sus canciones. "
            "Este enlace es de un %s; pega el enlace de una canción individual "
            "(open.spotify.com/track/...) o busca el nombre de la canción aquí: "
            "se buscará la misma canción en YouTube." % tipo)
    embed = "https://open.spotify.com/embed/track/" + m.group(1)
    req = urllib.request.Request(embed, headers={"User-Agent": "Mozilla/5.0"})
    try:
        html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
    except Exception:
        raise RuntimeError("Spotify no respondió. Revisa tu conexión y vuelve a intentarlo.")
    tm = re.search(r"<title>(.*?)</title>", html, re.S)
    title = re.sub(r"\s+", " ", tm.group(1)).strip() if tm else ""
    title = re.sub(r"\s*\|\s*(?:Spotify|Embed)\s*$", "", title, flags=re.I).strip()
    title = re.sub(r"\s*[-–—]\s*|\s+by\s+", " ", title, flags=re.I).strip()
    title = re.sub(r"\s+", " ", title).strip()
    if not title:
        raise RuntimeError("No se pudo leer la canción de Spotify. Prueba buscando el título aquí.")
    return title


def spotify_credenciales():
    """Lee Client ID y Client Secret de Spotify del archivo spotify.txt
    (primera línea el ID, segunda el SECRETO) junto al programa o en la
    carpeta de configuración."""
    for d in (exe_dir(), config_dir()):
        f = os.path.join(d, "spotify.txt")
        if os.path.exists(f):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    lines = [ln.strip() for ln in fh if ln.strip()]
                if len(lines) >= 2 and lines[0] and lines[1]:
                    return lines[0], lines[1]
            except Exception:
                pass
    return None


def spotify_tipo(url):
    if "open.spotify.com" not in url.lower():
        return None
    if "/track/" in url:
        return "track"
    if "/album/" in url:
        return "album"
    if "/playlist/" in url:
        return "playlist"
    return "otro"


def spotify_nombre(url):
    """Lee el nombre de un álbum o lista de Spotify desde su página."""
    m = re.search(r"/(album|playlist)/([A-Za-z0-9]+)", url)
    if not m:
        return None
    embed = "https://open.spotify.com/embed/%s/%s" % (m.group(1), m.group(2))
    req = urllib.request.Request(embed, headers={"User-Agent": "Mozilla/5.0"})
    try:
        html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
    except Exception:
        return None
    tm = re.search(r"<title>(.*?)</title>", html, re.S)
    nombre = re.sub(r"\s*\|\s*Spotify\s*$", "", (re.sub(r"\s+", " ", tm.group(1)).strip()
                                                 if tm else "")).strip()
    return nombre or None


def run_spotdl(url, carpeta, on_line, on_done, stop_event):
    """Descarga un álbum o lista de Spotify usando la librería spotDL.
    spotDL no extrae el audio de Spotify (es DRM): localiza cada canción
    en YouTube con la librería y la baja a la carpeta elegida."""
    def _work():
        ff = find_ffmpeg()
        path_prev = os.environ.get("PATH", "")
        try:
            if ff:
                os.environ["PATH"] = os.path.dirname(ff) + os.pathsep + path_prev
            try:
                from spotdl import Spotdl
            except Exception as e:
                on_done("La librería de Spotify no está disponible: %s" % e)
                return
            creds = spotify_credenciales()
            try:
                if creds:
                    sp = Spotdl(client_id=creds[0], client_secret=creds[1],
                                output_format="mp3", output=carpeta)
                else:
                    sp = Spotdl(output_format="mp3", output=carpeta)
            except Exception as e:
                on_done("Spotify no se pudo iniciar (comprueba las credenciales "
                        "del archivo spotify.txt): %s" % e)
                return
            if ff:
                opts = getattr(sp, "ydl_opts", None)
                if isinstance(opts, dict):
                    opts["ffmpeg_location"] = os.path.dirname(ff)
            try:
                try:
                    os.makedirs(carpeta, exist_ok=True)
                except Exception:
                    pass
                canciones = sp.search(url) or []
                if not canciones:
                    on_done("No se encontraron canciones en ese enlace de Spotify.")
                    return
                on_line("Voy a descargar %d canciones..." % len(canciones))
                for c in canciones:
                    if stop_event.is_set():
                        on_done("Descarga cancelada.")
                        return
                    artistas = ", ".join(getattr(c, "artists", []) or [])
                    nombre = str(getattr(c, "name", c) or c)
                    on_line("Descargando: %s" % (("%s - %s" % (artistas, nombre))
                                                 if artistas else nombre))
                    try:
                        res = sp.download(c)
                        if res is not None and hasattr(res, "wait"):
                            res.wait()
                    except Exception as e:
                        on_line("No se pudo descargar una canción: %s" % e)
                on_done(None)
            except Exception as e:
                on_done("Falló con Spotify: %s" % e)
        finally:
            os.environ["PATH"] = path_prev

    threading.Thread(target=_work, daemon=True).start()


def search(query, url, playlist=False):
    if url.strip():
        if not re.match(r"https?://", url.strip()):
            raise ValueError("La URL debe empezar con http:// o https://")
        args = ["--dump-single-json"]
        if playlist:
            args.append("--flat-playlist")
        args.append(url.strip())
        code, out, err = _run_ydl(args)
        if code != 0:
            raise RuntimeError(_small_error(err) or url.strip())
        data = json.loads(out)
        if playlist:
            entries = data.get("entries") or ([data] if data.get("id") else [])
            return _pack(entries)
        return _pack([data])
    q = query.strip()
    if not q:
        raise ValueError("Escribe el nombre de una canción o pega una URL.")
    code, out, err = _run_ydl(
        ["--flat-playlist", "--dump-single-json", "ytsearch%d:%s" % (SEARCH_LIMIT, q)]
    )
    if code != 0:
        raise RuntimeError(_small_error(err) or q)
    data = json.loads(out)
    entries = data.get("entries") or []
    return _pack(entries)


def _sanitize(name):
    return re.sub(r'[\\/:*?"<>|]', "", name).strip()


def build_download_cmd(item, cfg, is_video, name, playlist, target_url):
    carpeta = cfg["carpeta"]
    try:
        os.makedirs(carpeta, exist_ok=True)
    except Exception:
        pass
    url = target_url or item["url"]
    base = "%(title)s" if playlist else (_sanitize(name) if name else "%(title)s")
    out = os.path.join(carpeta, base + ".%(ext)s")
    args = ["-o", out]
    if playlist:
        args.append("--yes-playlist")
    else:
        args.append("--no-playlist")
    if is_video:
        if find_ffmpeg():
            args += ["-f", "bestvideo*+bestaudio/best", "--merge-output-format", "mp4"]
        else:
            args += ["-f", "best"]
    else:
        formato = cfg["formato"]
        if formato in ("m4a", "opus", "webm"):
            prefijo = "bestaudio[ext=opus]/bestaudio[ext=webm]/bestaudio/best" \
                if formato == "opus" else \
                "bestaudio[ext=%s]/bestaudio/best" % formato
            args += ["-f", prefijo]
        else:
            ffmpeg = find_ffmpeg()
            if not ffmpeg:
                raise RuntimeError(
                    "El formato %s necesita ffmpeg, que está integrado. "
                    "Vuelve a abrir la aplicación desde el .exe." % formato.upper()
                )
            args += ["-f", "bestaudio/best"]
            args += ["-x", "--audio-format", formato, "--audio-quality", cfg["calidad"]]
    args.append(url)
    return args


def run_download(item, cfg, is_video, name, playlist, target_url,
                 on_percent, on_done, stop_event):
    def _work():
        exe = find_ytdlp()
        if not exe:
            on_done("No se encontró yt-dlp.")
            return
        try:
            args = build_download_cmd(item, cfg, is_video, name, playlist, target_url)
        except Exception as e:
            on_done(str(e))
            return
        flags = ["--no-warnings", "--newline", "--age-limit", "100"]
        c = cookies_path()
        if c:
            flags += ["--cookies", c]
        ffmpeg = find_ffmpeg()
        if ffmpeg:
            flags += ["--ffmpeg-location", os.path.dirname(ffmpeg)]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        carpeta = cfg.get("carpeta") or ""
        try:
            anteriores = set(os.listdir(carpeta)) if carpeta and os.path.isdir(carpeta) else None
        except Exception:
            anteriores = None
        try:
            proc = subprocess.Popen(
                [exe] + flags + args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
                bufsize=1,
                stdin=subprocess.DEVNULL,
            )
        except Exception as e:
            on_done("No se pudo iniciar la descarga: %s" % e)
            return

        def _espera_cancelacion():
            stop_event.wait()
            try:
                proc.kill()
            except Exception:
                pass

        threading.Thread(target=_espera_cancelacion, daemon=True).start()

        def _limpia_parciales():
            if not anteriores or not carpeta:
                return
            try:
                for n in os.listdir(carpeta):
                    if n.startswith("."):
                        continue
                    p = os.path.join(carpeta, n)
                    if n not in anteriores and os.path.isfile(p):
                        try:
                            os.remove(p)
                        except Exception:
                            pass
            except Exception:
                pass

        error = None
        err_lines = []
        last_lines = []
        for line in proc.stdout:
            if stop_event.is_set():
                if not error:
                    error = "Descarga cancelada."
                break
            s = line.strip()
            if s:
                last_lines.append(s)
                if len(last_lines) > 6:
                    last_lines.pop(0)
            if "ERROR:" in line:
                err_lines.append(s)
                if len(err_lines) > 5:
                    err_lines.pop(0)
            m = re.search(r"(\d+(?:\.\d+)?)%", line)
            if m:
                try:
                    on_percent(float(m.group(1)))
                except Exception:
                    on_percent(m.group(1))
        proc.wait()
        if stop_event.is_set():
            error = error or "Descarga cancelada."
            _limpia_parciales()
        elif error is None and proc.returncode != 0:
            error = "La descarga terminó con error."
            detalle = " | ".join(err_lines[-3:] or last_lines[-3:])
            if detalle:
                error += " Detalle: %s" % detalle
        on_done(error)

    threading.Thread(target=_work, daemon=True).start()


# ── Reproducción dentro de la aplicación (MCI / winmm) ─────────
_mci = ctypes.windll.winmm.mciSendStringW
_mci.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p]
_mci.restype = ctypes.c_ulong


def mci_cmd(cmd):
    buf = ctypes.create_unicode_buffer(512)
    code = _mci(cmd, buf, 512, None)
    return code == 0, buf.value


def preview_folder():
    folder = os.path.join(tempfile.gettempdir(), "DescargaMusica")
    try:
        os.makedirs(folder, exist_ok=True)
    except Exception:
        pass
    return folder


def mci_open_play(path, alias, volumen=None, offset_ms=0):
    ok, _ = mci_cmd('open "%s" type mpegvideo alias %s' % (path, alias))
    if not ok:
        ok, _ = mci_cmd('open "%s" alias %s' % (path, alias))
    if not ok:
        return False
    target = None
    if volumen is not None:
        target = max(0, min(1000, int(volumen) * 10))
        mci_cmd('setaudio %s volume to %d' % (alias, target))
    ok, _ = mci_cmd('play %s from %d' % (alias, int(offset_ms)))
    if not ok:
        mci_cmd('close %s' % alias)
        return False
    if target is not None:
        mci_cmd('setaudio %s volume to %d' % (alias, target))
    return True


def mci_position(alias):
    ok, txt = mci_cmd('status %s position' % alias)
    if ok:
        try:
            return int(txt.strip())
        except Exception:
            pass
    return None


def mci_duration(alias):
    ok, txt = mci_cmd('status %s length' % alias)
    if ok:
        try:
            return int(txt.strip()) // 1000
        except Exception:
            pass
    return 0


def mci_stop_and_close(alias):
    if not alias:
        return
    mci_cmd('stop %s' % alias)
    mci_cmd('close %s' % alias)


def remove_preview(path):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass


def _descargar_preview(args):
    return _run_ydl(args)


def _find_preview_file(folder, base):
    for f in os.listdir(folder):
        if f.startswith(os.path.basename(base)) and f != os.path.basename(base):
            p = os.path.join(folder, f)
            if os.path.isfile(p):
                return p
    return None


def _probar_mci(path):
    ok, _ = mci_cmd('open "%s" type mpegvideo alias tryplay' % path)
    mci_cmd('close tryplay')
    if ok:
        return path
    wav = path.rsplit(".", 1)[0] + ".wav"
    ff = find_ffmpeg()
    if ff:
        cc = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        pr = subprocess.run([ff, "-y", "-v", "error", "-i", path, "-vn", wav],
                            capture_output=True, creationflags=cc)
        if pr.returncode == 0 and os.path.exists(wav):
            ok2, _ = mci_cmd('open "%s" alias tryplay2' % wav)
            mci_cmd('close tryplay2')
            if ok2:
                remove_preview(path)
                return wav
    return None


CHUNK_SECONDS = 15


def _video_duration_sec(item):
    m = re.match(r"(\d+):(\d+)", str(item.get("duration") or ""))
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    return 0


def prepare_full_preview(item, on_chunk, on_seg, on_skip, on_error, stop_event=None):
    """Baja el audio por trozos seguidos: el primer trozo suena casi al
    instante y el resto se va encadenando sin cortes, de modo que la
    canción suena completa mientras se descarga. Si un trozo no llega,
    se reintenta y, si sigue sin llegar, se salta para no detener la
    canción en mitad."""
    def descargar_trozo(base, a, b, url):
        try:
            code, _, err = _descargar_preview([
                "-f", "bestaudio[ext=m4a]/bestaudio/best",
                "--download-sections", "*%d-%d" % (a, b),
                "-o", base + ".%(ext)s", url,
            ])
        except Exception as e:
            return -1, str(e)
        path = _find_preview_file(folder, base) if code == 0 else None
        return code, (path if path else err)

    def work():
        folder = preview_folder()
        url = item.get("url") or ""
        total = _video_duration_sec(item)
        conocido = total > CHUNK_SECONDS
        index = 0
        reintentos = 3
        while True:
            if stop_event is not None and stop_event.is_set():
                return
            a = index * CHUNK_SECONDS
            b = (index + 1) * CHUNK_SECONDS
            if conocido and a >= total:
                wx.CallAfter(on_seg, None, index, total)
                return
            if conocido and b > total:
                b = total
            base = os.path.join(folder, "seg_%d_%d_%d"
                                % (os.getpid(), int(time.time() * 1000), index))
            path = None
            err = ""
            for intento in range(reintentos):
                if stop_event is not None and stop_event.is_set():
                    return
                code, err = descargar_trozo(base, a, b, url)
                if code == 0:
                    path = err
                    break
                if intento + 1 < reintentos:
                    time.sleep(1)
            if path is None:
                if index == 0:
                    wx.CallAfter(on_error, _small_error(err)
                                 or "No se pudo descargar el audio. Revisa tu conexión.")
                    return
                if conocido:
                    wx.CallAfter(on_skip, index)
                    index += 1
                    continue
                wx.CallAfter(on_seg, None, index, total)
                return
            if index == 0:
                wx.CallAfter(on_chunk, path, index, total)
            else:
                wx.CallAfter(on_seg, path, index, total)
            index += 1

    threading.Thread(target=work, daemon=True).start()


# ── Ayuda detallada (se abre con F1) ───────────────────────────
HELP_TEXT = """Descargador de Música, versión 1.0.0 beta.

Qué hace este programa:
Descarga música y vídeo como aplicación independiente, pensada para
leerse cómodamente con NVDA u otro lector de pantalla. Lleva dentro
del mismo archivo .exe todo lo que necesita (yt-dlp y ffmpeg): no hay
que instalar nada.

Pantalla de búsqueda:
- Primer cuadro: escribe el nombre de una canción y pulsa Intro, o
  pulsa el botón "Buscar". Mientras busca, el lector dice "Buscando".
- Botón "Buscar por URL": abre la pantalla para pegar un enlace de
  un vídeo, de una playlist, de Facebook, de Spotify, etc.

Pantalla de URL:
- Cuadro para pegar el enlace. Al pulsar Intro se busca.
- "Tipo de descarga": elige si quieres la canción sola o la playlist
  completa.
- "Audio o Vídeo": elige cómo quieres obtenerla. Si no eliges, se
  toma Audio.
- Botón "Buscar": muestra la lista de resultados.
- Botón "Volver a búsqueda normal": regresa sin hacer nada.

Spotify:
- Si pegas el enlace de una canción individual, se leen sus datos y
  se busca la misma canción en YouTube para escucharla y descargarla
  (Spotify no entrega su audio: es contenido protegido).
- Si pegas un álbum o una lista, se ofrece descargarlas completas con
  la librería de Spotify integrada. Para ello, Spotify requiere dos
  claves gratuitas: crea una aplicación en developers.spotify.com,
  copia el Client ID y el Client Secret, y guárdalos en el archivo
  spotify.txt junto a este programa (primera línea el ID, segunda
  línea el SECRETO). Después repite la búsqueda del enlace.

Otros sitios (Facebook, etc.) y contenido para mayores de 18:
- Pegando la URL de estos sitios, la descarga funciona directamente.
- Si un sitio o YouTube pide iniciar sesión o confirmar edad, coloca
  un archivo cookies.txt junto a este programa (puedes exportarlo
  desde tu navegador o usar una extensión que lo genere).

Pantalla de resultados:
- La lista: cada elemento muestra número, título, duración y canal.
- Botón "Reproducir": en pocos segundos suena el principio y la
  canción completa se termina de bajar mientras suena, encadenándose
  por trozos para que no se corte. El botón se convierte en
  "Detener" para cortar cuando quieras. Si un trozo no llega, se
  salta para que la canción no se detenga en mitad.
- Botón "Descargar": abre una ventana y, con las flechas, eliges si
  se descargará en Audio o en Vídeo. Si eliges Audio, aparecen el
  formato y la calidad en kbps solo para esa descarga. Si eliges
  Vídeo, se descarga en MP4. Después pulsas "Descargar". Mientras
  baja, se ve "Descargando" con el nombre de la canción y la música
  sigue sonando; puedes cancelarla cuando quieras. Al terminar dice
  "La canción fue descargada con éxito" y la carpeta.
- Botón "Nueva búsqueda": vuelve a buscar otra canción.

Pantalla principal:
- Botón "Más opciones": cambia el volumen de reproducción, la carpeta
  de descargas y comprueba e instala actualizaciones de yt-dlp. La
  comprobación automática al abrir el programa es silenciosa: solo
  avisa cuando se ha instalado una versión nueva.
- Botón "Salir": cierra la aplicación.
- F1 abre un menú donde eliges ver la Ayuda o las Novedades.

Volumen:
El deslizador de volumen está en "Más opciones". Súbelo o bájalo
para que la música no suene más fuerte que el lector de pantalla.
Queda guardado para la próxima vez.

Formatos de audio:
M4A y OPUS: funcionan directamente (excelente calidad; M4A es el más
recomendable para escuchar en cualquier aparato).
WEBM: también directo.
MP3, WAV y OGG: se convierten con ffmpeg, que ya está integrado.
MP3 es el más compatible; WAV es sin compresión (pesa mucho);
OGG es de buena calidad.
Para vídeo se descarga en MP4 (imagen y sonido juntos).

Calidad en kbps:
Velocidad de bits del audio convertido: a mayor número, mejor sonido
y mayor archivo. 192 es un buen equilibrio; 128 es más liviano y
320 la máxima. Solo afecta a los formatos que necesitan conversión.

Carpeta de descargas:
Por defecto Downloads/Descargador de Música. Puedes cambiarla en
"Más opciones" con "Elegir carpeta". La elección queda guardada.

Actualizar yt-dlp:
Al abrir la aplicación se comprueba automáticamente si hay una nueva
versión de yt-dlp: si la hay, se instala y se avisa con un mensaje;
si ya está al día, no molesta. Además, en "Más opciones" está el
botón que hace esa comprobación manualmente cuando quieras. Es
recomendable mantenerlo actualizado para que siga descargando
sin errores. La actualización se hace de forma segura: nunca deja
al descargador estropeado.

Accesibilidad:
Todo se maneja con tabulador, flechas, Intro, Espacio y F1. Los
avisos breves que no necesitan acción, como "Buscando" o
"Cargando", los dice el lector de pantalla sin mostrar ningún
cuadro en pantalla.
"""

NOVEDADES_TEXT = """Novedades de esta versión.

1. La canción completa suena sin cortes:
   Ya no se detiene a los pocos segundos. Reproduce el principio casi
   al instante y va uniendo trozos mientras baja, hasta que pulses
   Detener o termine la canción. Si un trozo llega tarde, se salta
   para que no se quede detenida en mitad.

2. Spotify integrado:
   - Enlace de una canción individual: se busca la misma canción en
     YouTube para escucharla y descargarla.
   - Enlace de un álbum o lista: se descargan todas sus canciones con
     la librería de Spotify integrada (necesita las claves gratuitas
     del archivo spotify.txt, explicado en la Ayuda con F1).

3. Facebook y otros sitios:
   Al pegar la URL, se descarga directamente. También los sitios para
   mayores de 18; y si piden iniciar sesión, con un cookies.txt junto
   al programa se desbloquean.

4. Mensajes de descarga:
   Al descargar se ve "Descargando" con el nombre de la canción y la
   música sigue sonando. Al terminar dice "La canción fue descargada
   con éxito" y muestra la carpeta.

5. Sin cuadros que pidan Enter:
   Los avisos de yt-dlp que pedían pulsar una tecla ya no se muestran
   en pantalla. Los breves como "Buscando" o "Cargando" solo los dice
   el lector, sin cuadros visibles.

6. Actualización de yt-dlp más segura:
   Se comprueba en silencio al abrir el programa y solo avisa cuando
   se instala una versión nueva. Ya no deja el descargador en mal
   estado si el proceso falla.

7. Menú con F1:
   Pulsar F1 abre un menú para elegir entre ver la Ayuda o las
   Novedades.
"""


class MenuDialog(wx.Dialog):
    def __init__(self, parent):
        super().__init__(parent, title="F1: menú", size=(400, 190))
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(panel, label="Elige lo que quieres ver:"), 0, wx.ALL, 8)
        btnAyuda = wx.Button(panel, label="&Ayuda")
        btnAyuda.Bind(wx.EVT_BUTTON, lambda evt: self.EndModal(1))
        btnNews = wx.Button(panel, label="&Novedades")
        btnNews.Bind(wx.EVT_BUTTON, lambda evt: self.EndModal(2))
        btnCerrar = wx.Button(panel, wx.ID_CANCEL)
        btnCerrar.SetLabel("&Cancelar")
        btnCerrar.Bind(wx.EVT_BUTTON, lambda evt: self.EndModal(wx.ID_CANCEL))
        sizer.Add(btnAyuda, 0, wx.ALL | wx.EXPAND, 4)
        sizer.Add(btnNews, 0, wx.ALL | wx.EXPAND, 4)
        sizer.Add(btnCerrar, 0, wx.ALL | wx.EXPAND, 4)
        panel.SetSizer(sizer)
        btnAyuda.SetFocus()


class HelpDialog(wx.Dialog):
    def __init__(self, parent):
        super().__init__(parent, title="Ayuda del Descargador de Música", size=(720, 580))
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        txt = wx.TextCtrl(panel, value=HELP_TEXT, style=wx.TE_MULTILINE | wx.TE_READONLY)
        sizer.Add(txt, 1, wx.ALL | wx.EXPAND, 6)
        closeBtn = wx.Button(panel, wx.ID_OK)
        closeBtn.SetLabel("Cerrar")
        sizer.Add(closeBtn, 0, wx.ALIGN_CENTER | wx.ALL, 6)
        panel.SetSizer(sizer)
        self.Bind(wx.EVT_BUTTON, lambda evt: self.EndModal(wx.ID_OK), id=wx.ID_OK)
        txt.SetFocus()


class NoveltiesDialog(wx.Dialog):
    def __init__(self, parent):
        super().__init__(parent, title="Novedades del Descargador de Música", size=(720, 580))
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        txt = wx.TextCtrl(panel, value=NOVEDADES_TEXT, style=wx.TE_MULTILINE | wx.TE_READONLY)
        sizer.Add(txt, 1, wx.ALL | wx.EXPAND, 6)
        closeBtn = wx.Button(panel, wx.ID_OK)
        closeBtn.SetLabel("Cerrar")
        sizer.Add(closeBtn, 0, wx.ALIGN_CENTER | wx.ALL, 6)
        panel.SetSizer(sizer)
        self.Bind(wx.EVT_BUTTON, lambda evt: self.EndModal(wx.ID_OK), id=wx.ID_OK)
        txt.SetFocus()


# ── Diálogo "Más opciones" ─────────────────────────────────────
class MoreOptionsDialog(wx.Dialog):
    def __init__(self, parent, cfg):
        super().__init__(parent, title="Más opciones", size=(560, 460))
        self.cfg = cfg
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        sizer.Add(wx.StaticText(panel, label="Volumen de reproducción:"), 0, wx.ALL, 4)
        self.slider = wx.Slider(panel, minValue=0, maxValue=100, value=int(cfg.get("volumen", 80)))
        self.slider.Bind(wx.EVT_SLIDER, self._onVolume)
        sizer.Add(self.slider, 0, wx.EXPAND | wx.ALL, 4)

        sizer.Add(wx.StaticText(panel, label="Carpeta de descargas:"), 0, wx.ALL, 4)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.folderText = wx.TextCtrl(panel, value=cfg["carpeta"])
        row.Add(self.folderText, 1, wx.EXPAND | wx.ALL, 4)
        pickBtn = wx.Button(panel, label="&Elegir carpeta...")
        pickBtn.Bind(wx.EVT_BUTTON, self._onPickFolder)
        row.Add(pickBtn, 0, wx.ALL, 4)
        sizer.Add(row, 0, wx.EXPAND)

        updBtn = wx.Button(panel, label="Comprobar y &actualizar yt-dlp")
        updBtn.Bind(wx.EVT_BUTTON, self._onUpdate)
        sizer.Add(updBtn, 0, wx.ALL, 4)
        self.updBtn = updBtn

        btns = wx.BoxSizer(wx.HORIZONTAL)
        okBtn = wx.Button(panel, wx.ID_OK)
        cancelBtn = wx.Button(panel, wx.ID_CANCEL)
        btns.Add(okBtn, 0, wx.RIGHT, 8)
        btns.Add(cancelBtn, 0, 0, 0)
        sizer.Add(btns, 0, wx.ALIGN_CENTER | wx.ALL, 8)

        panel.SetSizer(sizer)
        self.Bind(wx.EVT_BUTTON, lambda evt: self.EndModal(wx.ID_OK), id=wx.ID_OK)
        self.Bind(wx.EVT_BUTTON, lambda evt: self.EndModal(wx.ID_CANCEL), id=wx.ID_CANCEL)

    def _onVolume(self, evt):
        v = self.slider.GetValue()
        mci_cmd('setaudio repro volume to %d' % (v * 10))

    def _onPickFolder(self, evt):
        dlg = wx.DirDialog(self, "Carpeta para las descargas",
                           defaultPath=self.folderText.GetValue() or os.path.expanduser("~"))
        if dlg.ShowModal() == wx.ID_OK:
            self.folderText.SetValue(dlg.GetPath())
        dlg.Destroy()

    def _onUpdate(self, evt):
        self.updBtn.Disable()
        threading.Thread(target=self._update_worker, daemon=True).start()

    def _update_worker(self):
        rc, out = actualizar_externo()
        wx.CallAfter(self._on_update_done, out, rc == 0)

    def _on_update_done(self, detail, ok):
        self.updBtn.Enable()
        espanol = bool(self.cfg.get("espanol", True))
        detail = traducir_ytdlp(detail, espanol)
        if ok:
            wx.MessageBox("Resultado de la actualización:\n\n%s" % detail,
                          "Actualizar yt-dlp", wx.OK | wx.ICON_INFORMATION,
                          parent=self)
        else:
            wx.MessageBox("La actualización no se completó:\n\n%s" % detail,
                          "Actualizar yt-dlp", wx.OK | wx.ICON_ERROR, parent=self)

    def get_values(self):
        self.cfg["volumen"] = self.slider.GetValue()
        folder = self.folderText.GetValue().strip()
        if folder:
            try:
                os.makedirs(folder, exist_ok=True)
                self.cfg["carpeta"] = folder
            except Exception:
                wx.MessageBox("La carpeta elegida no se puede usar.", "Aviso",
                              wx.OK | wx.ICON_WARNING, parent=self)
        return self.cfg


# ── Diálogo "Descargar" (elige Audio/Vídeo con flechas) ────────
class DownloadDialog(wx.Dialog):
    def __init__(self, parent, cfg, tipo_default=0):
        super().__init__(parent, title="Descargar", size=(460, 340))
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        sizer.Add(wx.StaticText(panel, label="¿En qué se descargará?"), 0, wx.ALL, 4)
        self.tipoCho = wx.Choice(panel, choices=["&Audio", "&Vídeo"])
        self.tipoCho.SetSelection(1 if tipo_default else 0)
        self.tipoCho.Bind(wx.EVT_CHOICE, self._onTipo)
        sizer.Add(self.tipoCho, 0, wx.ALL, 4)

        self.videoNote = wx.StaticText(panel, label="El vídeo se descargará en formato MP4.")
        sizer.Add(self.videoNote, 0, wx.ALL, 4)

        self.lblFmt = wx.StaticText(panel, label="Formato:")
        self.formatCho = wx.Choice(panel, choices=[label for _, label in FORMATOS])
        self.formatCho.SetSelection(FORMAT_CODES.index(cfg["formato"]))
        self.lblCal = wx.StaticText(panel, label="Calidad:")
        self.qualityCho = wx.Choice(panel, choices=[label for _, label in CALIDADES])
        self.qualityCho.SetSelection([c for c, _ in CALIDADES].index(cfg["calidad"]))
        sizer.Add(self.lblFmt, 0, wx.ALL, 4)
        sizer.Add(self.formatCho, 0, wx.ALL, 4)
        sizer.Add(self.lblCal, 0, wx.ALL, 4)
        sizer.Add(self.qualityCho, 0, wx.ALL, 4)

        btns = wx.BoxSizer(wx.HORIZONTAL)
        okBtn = wx.Button(panel, wx.ID_OK)
        okBtn.SetLabel("&Descargar")
        cancelBtn = wx.Button(panel, wx.ID_CANCEL)
        btns.Add(okBtn, 0, wx.RIGHT, 8)
        btns.Add(cancelBtn, 0, 0, 0)
        sizer.Add(btns, 0, wx.ALIGN_CENTER | wx.ALL, 8)

        panel.SetSizer(sizer)
        self.Bind(wx.EVT_BUTTON, lambda evt: self.EndModal(wx.ID_OK), id=wx.ID_OK)
        self.Bind(wx.EVT_BUTTON, lambda evt: self.EndModal(wx.ID_CANCEL), id=wx.ID_CANCEL)
        self._onTipo(None)
        self.tipoCho.SetFocus()

    def _onTipo(self, evt):
        audio = self.tipoCho.GetSelection() == 0
        mostrar = (self.lblFmt, self.formatCho, self.lblCal, self.qualityCho)
        for w in mostrar:
            w.Show(audio)
        self.videoNote.Show(not audio)
        self.Layout()

    def get_values(self):
        tipo = 1 if self.tipoCho.GetSelection() == 1 else 0
        return tipo, FORMAT_CODES[self.formatCho.GetSelection()], \
            [c for c, _ in CALIDADES][self.qualityCho.GetSelection()]


# ── Ventana principal ──────────────────────────────────────────
class MainFrame(wx.Frame):
    P_SEARCH, P_URL, P_RESULTS = 0, 1, 2

    def __init__(self):
        super().__init__(None, title="%s — versión %s" % (APP_NAME, VERSION),
                         size=(840, 660))
        self.cfg = load_config()
        try:
            os.makedirs(self.cfg["carpeta"], exist_ok=True)
        except Exception:
            pass
        self.results = []
        self._busy = False
        self._playing = False
        self._prep = False
        self._preview_alias = None
        self._preview_path = None
        self._preview_temp = []
        self._dl_cancel = threading.Event()
        self._dl_progress = None
        self._dl_ultimo_anuncio = 0.0
        self._dl_ultimo_step = -1
        self._dl_fin_guard = False
        self._active_url = ""
        self._active_playlist = False
        self._last_tipo = 0
        self._ann_frames = []
        self._preview_tok = 0
        self._expected_tok = -1
        self._preview_queue = []
        self._preview_fin_ms = 0
        self._preview_total_ms = 0
        self._preview_actual_index = 0
        self._preview_segs_fin = False
        self._abort_preview = threading.Event()
        self._seg_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_seg_tick, self._seg_timer)
        self._prev_pos = -1
        self._sin_cambios = 0
        preview_folder()

        self.book = wx.Simplebook(self)
        self._build_search_page()
        self._build_url_page()
        self._build_results_page()

        root = wx.BoxSizer(wx.VERTICAL)
        root.Add(self.book, 1, wx.EXPAND | wx.ALL, 4)

        self.SetSizer(root)
        self.book.SetSelection(self.P_SEARCH)
        self.txtSearch.SetFocus()

        self.Bind(wx.EVT_CHAR_HOOK, self._onKey)
        self.Bind(wx.EVT_CLOSE, self._onClose)
        self._auto_check_done = False
        wx.CallAfter(self._start_auto_update)

    def _add_footer(self, s, p):
        """Botones comunes "Más opciones" y "Salir" al final de cada pantalla."""
        s.AddStretchSpacer(1)
        line = wx.BoxSizer(wx.HORIZONTAL)
        moreBtn = wx.Button(p, label="Más &opciones...")
        moreBtn.Bind(wx.EVT_BUTTON, self._onMore)
        salirBtn = wx.Button(p, label="&Salir")
        salirBtn.Bind(wx.EVT_BUTTON, lambda evt: self.Close())
        line.Add(moreBtn, 0, wx.RIGHT, 8)
        line.Add(salirBtn, 0, 0, 0)
        s.Add(line, 0, wx.ALIGN_CENTER | wx.ALL, 6)
        hint = wx.StaticText(p, label="F1: ayuda")
        s.Add(hint, 0, wx.ALIGN_CENTER | wx.BOTTOM, 2)

    def _build_search_page(self):
        p = wx.Panel(self.book)
        s = wx.BoxSizer(wx.VERTICAL)

        s.Add(wx.StaticText(p, label="Buscar canción:"), 0, wx.ALL, 4)
        self.txtSearch = wx.TextCtrl(p, style=wx.TE_PROCESS_ENTER)
        self.txtSearch.SetHint("Escribe el nombre de la canción")
        self.txtSearch.Bind(wx.EVT_TEXT_ENTER, self._onSearch)
        s.Add(self.txtSearch, 0, wx.EXPAND | wx.ALL, 4)

        self.btnSearch = wx.Button(p, label="&Buscar")
        self.btnSearch.Bind(wx.EVT_BUTTON, self._onSearch)
        s.Add(self.btnSearch, 0, wx.LEFT | wx.ALL, 4)

        self.btnOpenUrl = wx.Button(p, label="&Buscar por URL")
        self.btnOpenUrl.Bind(wx.EVT_BUTTON, self._open_url_page)
        s.Add(self.btnOpenUrl, 0, wx.LEFT | wx.ALL, 4)

        self._add_footer(s, p)
        p.SetSizer(s)
        self.book.AddPage(p, "Búsqueda")

    def _build_url_page(self):
        p = wx.Panel(self.book)
        s = wx.BoxSizer(wx.VERTICAL)

        s.Add(wx.StaticText(p, label="URL:"), 0, wx.ALL, 4)
        self.txtUrl = wx.TextCtrl(p, style=wx.TE_PROCESS_ENTER)
        self.txtUrl.SetHint("Pega aquí el enlace de un vídeo o playlist")
        self.txtUrl.Bind(wx.EVT_TEXT_ENTER, self._onSearchUrl)
        s.Add(self.txtUrl, 0, wx.EXPAND | wx.ALL, 4)

        s.Add(wx.StaticText(p, label="Tipo de descarga:"), 0, wx.ALL, 4)
        self.playlistCho = wx.Choice(p, choices=["Canción sola", "Playlist completa"])
        self.playlistCho.SetSelection(0)
        s.Add(self.playlistCho, 0, wx.ALL, 4)

        s.Add(wx.StaticText(p, label="Qué quieres obtener:"), 0, wx.ALL, 4)
        rowTipo = wx.BoxSizer(wx.HORIZONTAL)
        self.radioAudioUrl = wx.CheckBox(p, label="&Audio")
        self.radioVideoUrl = wx.CheckBox(p, label="&Vídeo")
        rowTipo.Add(self.radioAudioUrl, 0, wx.RIGHT, 16)
        rowTipo.Add(self.radioVideoUrl, 0, 0, 0)
        self.radioAudioUrl.SetValue(False)
        self.radioVideoUrl.SetValue(False)
        self.radioAudioUrl.Bind(wx.EVT_CHECKBOX, self._onTipoUrlAudio)
        self.radioVideoUrl.Bind(wx.EVT_CHECKBOX, self._onTipoUrlVideo)
        s.Add(rowTipo, 0, wx.ALL, 4)

        btns = wx.BoxSizer(wx.HORIZONTAL)
        self.btnBuscarUrl = wx.Button(p, label="&Buscar")
        self.btnBuscarUrl.Bind(wx.EVT_BUTTON, self._onSearchUrl)
        self.btnVolver = wx.Button(p, label="Volver a búsqueda &normal")
        self.btnVolver.Bind(wx.EVT_BUTTON, self._back_to_search)
        btns.Add(self.btnBuscarUrl, 0, wx.RIGHT, 8)
        btns.Add(self.btnVolver, 0, 0, 0)
        s.Add(btns, 0, wx.LEFT | wx.ALL, 4)

        s.AddStretchSpacer(1)
        p.SetSizer(s)
        self.book.AddPage(p, "URL")

    def _build_results_page(self):
        p = wx.Panel(self.book)
        s = wx.BoxSizer(wx.VERTICAL)

        s.Add(wx.StaticText(p, label="Resultados:"), 0, wx.LEFT | wx.TOP, 8)
        self.lstResults = wx.ListBox(p)
        self.lstResults.Bind(wx.EVT_LISTBOX_DCLICK, self._onDownload)
        s.Add(self.lstResults, 1, wx.ALL | wx.EXPAND, 4)

        btns = wx.BoxSizer(wx.HORIZONTAL)
        self.btnPlay = wx.Button(p, label="&Reproducir")
        self.btnPlay.Bind(wx.EVT_BUTTON, self._onPlay)
        self.btnPlay.Disable()
        self.btnDownload = wx.Button(p, label="&Descargar")
        self.btnDownload.Bind(wx.EVT_BUTTON, self._onDownload)
        self.btnDownload.Disable()
        newBtn = wx.Button(p, label="&Nueva búsqueda")
        newBtn.Bind(wx.EVT_BUTTON, self._onNewSearch)
        btns.Add(self.btnPlay, 0, wx.RIGHT, 8)
        btns.Add(self.btnDownload, 0, wx.RIGHT, 8)
        btns.Add(newBtn, 0, 0, 0)
        s.Add(btns, 0, wx.ALIGN_CENTER | wx.ALL, 6)

        s.AddStretchSpacer(1)
        p.SetSizer(s)
        self.book.AddPage(p, "Resultados")

    # ── evento global F1 ──
    def _onKey(self, evt):
        if evt.GetKeyCode() == wx.WXK_F1:
            self._onHelp()
            return
        evt.Skip()

    def _onHelp(self, evt=None):
        menu = MenuDialog(self)
        opc = menu.ShowModal()
        menu.Destroy()
        if opc == 1:
            dlg = HelpDialog(self)
        elif opc == 2:
            dlg = NoveltiesDialog(self)
        else:
            return
        dlg.ShowModal()
        dlg.Destroy()
        self.Raise()

    def _onClose(self, evt):
        self._abort_preview.set()
        self._seg_timer.Stop()
        if self._preview_alias:
            mci_stop_and_close(self._preview_alias)
            self._preview_alias = None
        self._cleanup_preview()
        self._clear_announce()
        if self._dl_progress:
            try:
                self._dl_progress.Destroy()
            except Exception:
                pass
            self._dl_progress = None
        save_config(self.cfg)
        evt.Skip()

    def _cleanup_preview(self):
        for p in list(self._preview_temp):
            remove_preview(p)
        self._preview_temp = []
        for sig, _ in list(self._preview_queue):
            remove_preview(sig)
        self._preview_queue = []
        if self._preview_path:
            remove_preview(self._preview_path)
            self._preview_path = None
        self._preview_segs_fin = False

    # ── Búsquedas ──
    def _open_url_page(self, evt=None):
        self.book.SetSelection(self.P_URL)
        wx.CallAfter(self.txtUrl.SetFocus)

    def _back_to_search(self, evt=None):
        self.book.SetSelection(self.P_SEARCH)
        wx.CallAfter(self.txtSearch.SetFocus)

    def _onSearch(self, evt=None):
        if self._busy:
            return
        self._start_search(self.txtSearch.GetValue(), "", False)

    def _onSearchUrl(self, evt=None):
        if self._busy:
            return
        self._last_tipo = 1 if self.radioVideoUrl.GetValue() else 0
        self._start_search("", self.txtUrl.GetValue(),
                           self.playlistCho.GetSelection() == 1)

    def _onTipoUrlAudio(self, evt):
        if self.radioAudioUrl.GetValue():
            self.radioVideoUrl.SetValue(False)

    def _onTipoUrlVideo(self, evt):
        if self.radioVideoUrl.GetValue():
            self.radioAudioUrl.SetValue(False)

    def _announce(self, texto):
        """Anuncia un mensaje breve para los lectores de pantalla, sin que
        se vea ningún cuadro ni panel en pantalla."""
        self._clear_announce()
        fr = wx.Frame(self, style=wx.FRAME_TOOL_WINDOW | wx.FRAME_NO_TASKBAR)
        fr.SetPosition(wx.Point(-32000, -32000))
        ed = wx.TextCtrl(fr, value=texto, style=wx.TE_READONLY, size=(1, 1))
        fr.SetSize((1, 1))
        fr.Show()
        self._ann_frames.append(fr)
        ed.SetFocus()

    def _clear_announce(self):
        for fr in self._ann_frames:
            try:
                fr.Destroy()
            except Exception:
                pass
        self._ann_frames = []

    def _annunciar_progreso(self, texto):
        ahora = time.time()
        if ahora - self._dl_ultimo_anuncio < 3:
            return
        self._dl_ultimo_anuncio = ahora
        self._announce(texto)

    def _start_search(self, query, url, playlist):
        self._busy = True
        self._announce("Buscando...")
        self.btnSearch.Disable()
        self.btnBuscarUrl.Disable()
        threading.Thread(target=self._search_worker,
                         args=(query, url, playlist), daemon=True).start()

    def _search_worker(self, query, url, playlist):
        try:
            if url.strip() and "open.spotify.com" in url.lower():
                tipo = spotify_tipo(url.strip())
                if tipo == "track":
                    q = resolver_spotify(url.strip())
                    wx.CallAfter(self._announce, "Buscando la canción en YouTube...")
                    results = search(q, "", playlist)
                    wx.CallAfter(self._show_results, results, "", playlist)
                    return
                if tipo in ("album", "playlist"):
                    if spotify_credenciales():
                        nombre = spotify_nombre(url.strip()) or "Lista de Spotify"
                        results = [{
                            "title": nombre,
                            "duration": "",
                            "channel": "Descargar todas las canciones con Spotify",
                            "url": url.strip(),
                            "_bulk": True,
                        }]
                        wx.CallAfter(self._show_results, results, url.strip(), False)
                    else:
                        raise RuntimeError(
                            "Spotify necesita dos claves gratuitas para leer la lista. "
                            "Crea una aplicación en developers.spotify.com, copia el "
                            "'Client ID' y el 'Client Secret', y guárdalos en el archivo "
                            "spotify.txt junto al programa (primera línea el ID, segunda "
                            "el SECRETO). Mientras tanto, pega el enlace de una CANCIÓN "
                            "individual para encontrarla en YouTube o busca el título aquí.")
                    return
                raise RuntimeError("Ese enlace de Spotify no se reconoce. Prueba con un "
                                   "enlace de canción, álbum o lista.")
            results = search(query, url, playlist)
            wx.CallAfter(self._show_results, results, url, playlist)
        except Exception as e:
            wx.CallAfter(self._show_search_error, str(e))

    def _show_results(self, results, url, playlist):
        self._clear_announce()
        self._busy = False
        self.btnSearch.Enable()
        self.btnBuscarUrl.Enable()
        self.results = results
        self._active_url = url
        self._active_playlist = playlist
        self.lstResults.Clear()
        for i, r in enumerate(results, 1):
            canal = r["channel"] and (" — " + r["channel"]) or ""
            self.lstResults.Append("%d. %s (%s)%s" % (i, r["title"], r["duration"], canal))
        if results:
            self.book.SetSelection(self.P_RESULTS)
            self.lstResults.SetSelection(0)
            self.btnPlay.Enable()
            self.btnDownload.Enable()
            wx.CallAfter(self.lstResults.SetFocus)
        else:
            self._show_search_error("No se encontraron resultados.")

    def _show_search_error(self, err):
        self._clear_announce()
        self._busy = False
        self.btnSearch.Enable()
        self.btnBuscarUrl.Enable()
        wx.MessageBox("Error: %s" % traducir_ytdlp(err, self.cfg.get("espanol", True)),
                      APP_NAME, wx.OK | wx.ICON_ERROR)
        self.Raise()

    def _onNewSearch(self, evt=None):
        self._abort_preview.set()
        self._seg_timer.Stop()
        self._preview_tok += 1
        self._busy = False
        self.results = []
        self.lstResults.Clear()
        self.btnPlay.Disable()
        self.btnDownload.Disable()
        if self._preview_alias:
            mci_stop_and_close(self._preview_alias)
            self._preview_alias = None
        self._cleanup_preview()
        self._playing = False
        self.btnPlay.SetLabel("&Reproducir")
        self.book.SetSelection(self.P_SEARCH)
        wx.CallAfter(self.txtSearch.SetFocus)

    # ── Reproducir (canción completa en la propia aplicación) ──
    def _onPlay(self, evt=None):
        if self._prep:
            return
        if self._playing:
            self._abort_preview.set()
            self._seg_timer.Stop()
            mci_stop_and_close(self._preview_alias)
            self._preview_alias = None
            self._cleanup_preview()
            self._playing = False
            self.btnPlay.SetLabel("&Reproducir")
            self.btnPlay.Enable()
            return
        if self._busy or not self.results:
            return
        sel = self.lstResults.GetSelection()
        if sel == wx.NOT_FOUND:
            wx.MessageBox("Primero elige un resultado de la lista.", APP_NAME,
                          wx.OK | wx.ICON_INFORMATION)
            return
        item = self.results[sel]
        if item.get("_bulk"):
            wx.MessageBox("Esta opción descarga toda la lista de Spotify a tu carpeta "
                          "con el botón Descargar. Pulsa Descargar para empezar.",
                          APP_NAME, wx.OK | wx.ICON_INFORMATION)
            return
        self._cleanup_preview()
        self._preview_tok += 1
        self._expected_tok = self._preview_tok
        self._abort_preview = threading.Event()
        self._prep = True
        self._busy = True
        self._announce("Cargando...")
        self.btnPlay.Disable()
        self.btnDownload.Disable()
        self.btnPlay.SetLabel("&Preparando...")
        prepare_full_preview(item, self._on_preview_chunk, self._on_preview_seg,
                             self._on_preview_skip, self._on_preview_error,
                             self._abort_preview)

    def _dur_de_indice(self, index):
        if self._preview_total_ms:
            a = index * CHUNK_SECONDS * 1000
            b = min((index + 1) * CHUNK_SECONDS * 1000, self._preview_total_ms)
            return max(0, b - a)
        return CHUNK_SECONDS * 1000

    def _on_preview_chunk(self, path, index, total):
        if self._preview_tok != self._expected_tok:
            remove_preview(path)
            return
        self._clear_announce()
        self._prep = False
        self._busy = False
        self.btnPlay.Enable()
        self.btnDownload.Enable()
        mci_stop_and_close(self._preview_alias)
        self._preview_alias = None
        self._preview_total_ms = total * 1000 if total and total > CHUNK_SECONDS else 0
        playable = _probar_mci(path)
        if not playable:
            self._playing = False
            remove_preview(path)
            self.btnPlay.SetLabel("&Reproducir")
            wx.MessageBox("No se pudo reproducir este audio.", APP_NAME,
                          wx.OK | wx.ICON_ERROR)
            self.Raise()
            return
        if mci_open_play(playable, "repro", self.cfg.get("volumen", DEFAULT_VOLUMEN), 0):
            self._preview_temp.append(path)
            if playable != path:
                self._preview_temp.append(playable)
            self._preview_alias = "repro"
            self._preview_path = playable
            self._preview_actual_index = 0
            self._preview_fin_ms = self._dur_de_indice(0)
            self._preview_segs_fin = bool(self._preview_total_ms
                                          and CHUNK_SECONDS * 1000 >= self._preview_total_ms)
            self._playing = True
            self.btnPlay.SetLabel("&Detener")
            self._prev_pos = -1
            self._sin_cambios = 0
            self._seg_timer.Start(300)
        else:
            self._playing = False
            remove_preview(path)
            remove_preview(playable)
            self.btnPlay.SetLabel("&Reproducir")
            wx.MessageBox("No se pudo reproducir este audio.", APP_NAME,
                          wx.OK | wx.ICON_ERROR)
            self.Raise()

    def _on_preview_skip(self, index):
        if self._preview_tok != self._expected_tok:
            return
        self._preview_queue.append((None, index))

    def _on_preview_seg(self, path, index, total):
        if self._preview_tok != self._expected_tok:
            if path:
                remove_preview(path)
            return
        if total and total > CHUNK_SECONDS:
            self._preview_total_ms = total * 1000
        if path:
            self._preview_temp.append(path)
            self._preview_queue.append((path, index))
        else:
            self._preview_segs_fin = True

    def _on_seg_tick(self, evt):
        if not self._playing or self._preview_tok != self._expected_tok:
            self._seg_timer.Stop()
            return
        pos = mci_position("repro")
        if pos is None or pos <= 0:
            return
        if pos == self._prev_pos:
            self._sin_cambios += 1
        else:
            self._prev_pos = pos
            self._sin_cambios = 0
        fin = self._preview_fin_ms
        if pos >= fin - 250 or (self._sin_cambios >= 12 and pos >= fin - 3000):
            self._sin_cambios = 0
            sig = None
            sig_index = -1
            while self._preview_queue:
                s0, i0 = self._preview_queue.pop(0)
                if s0 is not None:
                    sig, sig_index = s0, i0
                    break
            if not sig:
                if self._preview_segs_fin:
                    self._detener_repro_fin()
                return
            mci_stop_and_close(self._preview_alias)
            self._preview_alias = None
            playable = _probar_mci(sig)
            if not playable:
                remove_preview(sig)
                return
            if mci_open_play(playable, "repro",
                             self.cfg.get("volumen", DEFAULT_VOLUMEN), 0):
                if playable != sig:
                    self._preview_temp.append(playable)
                self._preview_alias = "repro"
                self._preview_path = playable
                self._preview_actual_index = sig_index
                self._preview_fin_ms = self._dur_de_indice(sig_index)
                self._preview_segs_fin = bool(
                    self._preview_total_ms
                    and (sig_index + 1) * CHUNK_SECONDS * 1000
                    >= self._preview_total_ms)
            else:
                remove_preview(sig)
                remove_preview(playable)

    def _detener_repro_fin(self):
        mci_stop_and_close(self._preview_alias)
        self._preview_alias = None
        self._fin_repro_ui()

    def _fin_repro_ui(self):
        self._seg_timer.Stop()
        self._playing = False
        self.btnPlay.SetLabel("&Reproducir")
        self._cleanup_preview()

    def _on_preview_error(self, msg):
        self._abort_preview.set()
        self._seg_timer.Stop()
        if self._preview_tok != self._expected_tok:
            self._cleanup_preview()
            return
        self._clear_announce()
        self._prep = False
        self._busy = False
        self.btnPlay.Enable()
        self.btnDownload.Enable()
        self.btnPlay.SetLabel("&Reproducir")
        self._cleanup_preview()
        wx.MessageBox("No se pudo reproducir: %s"
                      % traducir_ytdlp(msg, self.cfg.get("espanol", True)),
                      APP_NAME, wx.OK | wx.ICON_ERROR)
        self.Raise()

    # ── Descargar ──
    def _onDownload(self, evt=None):
        if self._busy or not self.results:
            return
        sel = self.lstResults.GetSelection()
        if sel == wx.NOT_FOUND:
            wx.MessageBox("Primero elige un resultado de la lista.", APP_NAME,
                          wx.OK | wx.ICON_INFORMATION)
            return
        item = self.results[sel]
        if item.get("_bulk"):
            self._descargar_spotify_bulk(item["url"])
            return
        dlg = DownloadDialog(self, self.cfg, self._last_tipo)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        tipo, formato, calidad = dlg.get_values()
        dlg.Destroy()
        is_video = tipo == 1
        self._last_tipo = tipo

        self.cfg["formato"] = formato
        self.cfg["calidad"] = calidad
        save_config(self.cfg)

        cfg = dict(self.cfg)
        titulo = item["title"]

        self._busy = True
        self._dl_titulo = titulo
        self._dl_cancel = threading.Event()
        self._dl_fin_guard = False
        self._dl_ultimo_anuncio = 0.0
        self._dl_ultimo_step = -1

        target_url = ""
        playlist = self._active_playlist
        if playlist and self._active_url:
            target_url = self._active_url

        self._dl_progress = wx.ProgressDialog(
            "Descarga en curso", "Descargando %s... La música sigue sonando." % titulo,
            maximum=100, style=wx.PD_CAN_ABORT | wx.PD_AUTO_HIDE)
        self._dl_progress.Show()

        def on_percent(pct):
            wx.CallAfter(self._on_dl_percent, pct)

        def on_done(error):
            wx.CallAfter(self._on_download_done, error)

        run_download(item, cfg, is_video, "", playlist, target_url,
                     on_percent, on_done, self._dl_cancel)

    def _descargar_spotify_bulk(self, url):
        cfg = dict(self.cfg)
        self._busy = True
        self.btnPlay.Disable()
        self.btnDownload.Disable()
        self._dl_cancel = threading.Event()
        self._dl_fin_guard = False
        self._dl_ultimo_anuncio = 0.0
        self._dl_ultimo_step = -1
        self._dl_progress = wx.ProgressDialog(
            "Descarga de Spotify", "Preparando...",
            maximum=100, style=wx.PD_CAN_ABORT | wx.PD_AUTO_HIDE)
        self._dl_progress.Show()
        if not spotify_credenciales():
            self._on_spotdl_done(
                "Spotify necesita las credenciales gratuitas: crea una aplicación en "
                "developers.spotify.com y guarda el Client ID y el Client Secret en el "
                "archivo spotify.txt junto al programa (primera línea el ID, segunda "
                "el SECRETO).")
            return

        def on_line(msg):
            wx.CallAfter(self._on_spotdl_line, msg)

        def on_done(error):
            wx.CallAfter(self._on_spotdl_done, error)

        run_spotdl(url, cfg["carpeta"], on_line, on_done, self._dl_cancel)

    def _on_spotdl_line(self, msg):
        if not self._dl_progress or self._dl_cancel.is_set():
            return
        cont, _ = self._dl_progress.Update(0, str(msg))
        if not cont:
            self._dl_cancel.set()
        self._annunciar_progreso(str(msg))

    def _on_spotdl_done(self, error):
        if self._dl_fin_guard:
            return
        self._dl_fin_guard = True
        if self._dl_progress:
            try:
                self._dl_progress.Destroy()
            except Exception:
                pass
            self._dl_progress = None
        self._busy = False
        self.btnPlay.Enable()
        self.btnDownload.Enable()
        if error:
            wx.MessageBox("No se pudo descargar de Spotify:\n\n%s" % error,
                          APP_NAME, wx.OK | wx.ICON_ERROR)
        else:
            wx.MessageBox("Las canciones se descargaron con éxito.\nCarpeta: %s"
                          % self.cfg["carpeta"], APP_NAME, wx.OK | wx.ICON_INFORMATION)
        self.Raise()

    def _on_dl_percent(self, pct):
        if not self._dl_progress or self._dl_cancel.is_set():
            return
        cont, _ = self._dl_progress.Update(int(pct),
                                           "Descargando %s... %d%%" % (self._dl_titulo, int(pct)))
        if not cont:
            self._dl_cancel.set()
        step = int(int(pct) / 10)
        if step != self._dl_ultimo_step:
            self._dl_ultimo_step = step
            self._annunciar_progreso("Descargando %s... %d por ciento"
                                     % (self._dl_titulo, int(pct)))

    def _on_download_done(self, error):
        if self._dl_fin_guard:
            return
        self._dl_fin_guard = True
        self._dl_ultimo_step = -1
        if self._dl_progress:
            try:
                self._dl_progress.Destroy()
            except Exception:
                pass
            self._dl_progress = None
        self._busy = False
        self.btnPlay.Enable()
        self.btnDownload.Enable()
        if error:
            wx.MessageBox("No se pudo descargar: %s"
                          % traducir_ytdlp(error, self.cfg.get("espanol", True)),
                          APP_NAME, wx.OK | wx.ICON_ERROR)
        else:
            wx.MessageBox("La canción fue descargada con éxito.\nAhora está en:\n%s"
                          % self.cfg["carpeta"], APP_NAME, wx.OK | wx.ICON_INFORMATION)
        self.Raise()

    # ── Más opciones ──
    def _onMore(self, evt=None):
        dlg = MoreOptionsDialog(self, self.cfg)
        if dlg.ShowModal() == wx.ID_OK:
            self.cfg = dlg.get_values()
            save_config(self.cfg)
        dlg.Destroy()

    # ── Comprobación automática de yt-dlp al abrir ──
    def _start_auto_update(self):
        def work():
            try:
                if self.IsBeingDeleted():
                    return
            except Exception:
                return
            time.sleep(6)
            if not self:
                return
            rc, out = actualizar_externo()
            wx.CallAfter(self._show_auto_update, out, rc)

        threading.Thread(target=work, daemon=True).start()

    def _show_auto_update(self, out, rc):
        if self._auto_check_done:
            return
        self._auto_check_done = True
        try:
            if self.IsBeingDeleted() or not self.IsShown():
                return
        except Exception:
            return
        es = traducir_ytdlp(out, True)
        # Silencioso salvo que se haya instalado una actualización real.
        if rc == 0 and "actualizado a la versión" in es.lower():
            wx.MessageBox("yt-dlp se ha actualizado:\n\n%s" % es,
                          APP_NAME, wx.OK | wx.ICON_INFORMATION)
            self.Raise()


def main():
    app = App(False)
    frame = MainFrame()
    frame.Show()
    app.MainLoop()


class App(wx.App):
    def OnExceptionInMainLoop(self, evt):
        info = self.GetCurrentExceptionInfo()
        try:
            wx.MessageBox(
                "Ocurrió un error inesperado:\n\n%s\n\nLa aplicación seguirá funcionando."
                % (info[2].strip().splitlines()[-1] if info and info[2] else info),
                APP_NAME, wx.OK | wx.ICON_ERROR)
        except Exception:
            pass
        return True


if __name__ == "__main__":
    main()