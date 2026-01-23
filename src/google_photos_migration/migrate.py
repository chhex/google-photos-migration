#!/usr/bin/env python3
"""
Google Takeout to Apple Photos Migration Script - v2
=====================================================

Änderungen v2:
- Duplikate werden still übersprungen (kein Fehler)
- Fehler bei einem Foto stoppen nicht den ganzen Batch
- Erfolgreich importierte Fotos werden aus dem Output-Ordner gelöscht
- Detaillierter Report was importiert/übersprungen/fehlgeschlagen ist

Workflow:
1. Entpackt alle Takeout-ZIPs
2. Findet JSON-Metadaten und schreibt sie in die Bilder (via exiftool)
3. Importiert in Apple Photos mit Albumstruktur (via osxphotos)
4. Löscht erfolgreich importierte Dateien

Voraussetzungen:
    brew install exiftool
    pip install osxphotos

Authors: Che, Claude
"""

import sys
import json
import zipfile
import subprocess
import shutil
import csv
import tomllib
import re
import psutil
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional



# =============================================================================
# KONFIGURATION - Hier anpassen
# =============================================================================
def load_config(config_path: Path = Path("config.toml")) -> dict:
    with open(config_path, "rb") as f:
        return tomllib.load(f)

config = load_config()
# Quellordner mit den Takeout-ZIPs
TAKEOUT_ZIP_DIR = Path(config["paths"]["takeout_zip_dir"])

# Arbeitsverzeichnis für entpackte Dateien
WORK_DIR = Path(config["paths"]["work_dir"])

# Fertig verarbeitete Fotos (bereit für Import)
OUTPUT_DIR = Path(config["paths"]["output_dir"])

# Anzahl paralleler Verarbeitungen
MAX_WORKERS = 4

# Trockenlauf - nur anzeigen, nichts ändern
DRY_RUN = config["import"]["dry_run"]

# Erfolgreich importierte Dateien löschen
DELETE_AFTER_IMPORT = config["import"]["delete_after_import"]

# Nur bestimmte Alben verarbeiten (leer = alle)
# z.B. ["Urlaub 2023", "Familie"]
ALBUM_FILTER = config["import"]["album_filter"]

# Unterstützte Dateitypen
IMAGE_EXTENSIONS = set(config["extensions"]["image"])
VIDEO_EXTENSIONS = set(config["extensions"]["video"])
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS


# =============================================================================
# DATENKLASSEN FÜR TRACKING
# =============================================================================

@dataclass
class ImportResult:
    """Ergebnis eines einzelnen Imports."""
    filepath: Path
    album: str
    status: str  # 'imported', 'duplicate', 'error', 'skipped'
    error_message: Optional[str] = None


@dataclass 
class ImportStats:
    """Gesamtstatistiken für den Import."""
    total: int = 0
    imported: int = 0
    duplicates: int = 0
    errors: int = 0
    skipped: int = 0
    deleted: int = 0
    results: list = field(default_factory=list)
    
    def add_result(self, result: ImportResult):
        self.results.append(result)
        self.total += 1
        if result.status == 'imported':
            self.imported += 1
        elif result.status == 'duplicate':
            self.duplicates += 1
        elif result.status == 'error':
            self.errors += 1
        elif result.status == 'skipped':
            self.skipped += 1
    
    def summary(self) -> str:
        return (f"Total: {self.total} | "
                f"Importiert: {self.imported} | "
                f"Duplikate: {self.duplicates} | "
                f"Fehler: {self.errors} | "
                f"Übersprungen: {self.skipped} | "
                f"Gelöscht: {self.deleted}")


# =============================================================================
# HEALTH CHECK FOR APPLE PHOTOS SANITY
# =============================================================================


def get_photos_errors(since_minutes: int = 5) -> int:
    """
    Zählt photolibraryd-Fehler in den System Logs.
    """
    since = (datetime.now() - timedelta(minutes=since_minutes)).strftime("%Y-%m-%d %H:%M:%S")
    
    cmd = [
        "log", "show",
        "--predicate", 'process == "photolibraryd"',
        "--start", since,
        "--style", "compact"
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    
    error_lines = [l for l in result.stdout.split('\n') 
                   if ' E  photolibraryd' in l]
    
    return len(error_lines)


def check_photos_health(max_memory_gb: float = 3.0, max_errors: int = 5) -> tuple[bool, str]:
    """
    Prüft ob Apple Photos noch gesund ist.
    """
    for proc in psutil.process_iter(['name', 'memory_info']):
        if proc.info['name'] == 'Photos':
            memory_gb = proc.info['memory_info'].rss / (1024 ** 3)
            
            if memory_gb > max_memory_gb:
                return False, f"Memory zu hoch: {memory_gb:.1f} GB"
            
            error_count = get_photos_errors(since_minutes=5)
            if error_count > max_errors:
                return False, f"Zu viele Fehler: {error_count}"
            
            return True, f"OK (Mem: {memory_gb:.1f} GB, Errors: {error_count})"
    
    return False, "Photos läuft nicht"


def restart_photos():
    """Beendet und startet Photos neu."""
    import time
    
    log("Photos wird neugestartet...", "WARN")
    subprocess.run(["osascript", "-e", 'quit app "Photos"'])
    time.sleep(5)
    subprocess.run(["open", "-a", "Photos"])
    time.sleep(10)
    log("Photos neugestartet", "OK")

def handle_health_failure(reason: str, action: str) -> bool:
    """
    Reagiert auf Health-Probleme.
    
    Returns: True wenn weiterfahren, False wenn abbrechen
    """
    if action == "restart":
        log(f"Health Check fehlgeschlagen: {reason}", "WARN")
        restart_photos()
        return True
        
    elif action == "terminate":
        log(f"Health Check fehlgeschlagen: {reason}", "ERR")
        log("Abbruch - bitte Photos manuell neustarten und Skript erneut ausführen", "ERR")
        return False
        
    elif action == "manual":
        log(f"Health Check fehlgeschlagen: {reason}", "WARN")
        response = input("Weiterfahren? (j=ja, r=restart Photos, n=abbrechen): ").strip().lower()
        
        if response == 'j':
            return True
        elif response == 'r':
            restart_photos()
            return True
        else:
            log("Abbruch durch Benutzer", "INFO")
            return False
    
    return False

# =============================================================================
# HILFSFUNKTIONEN
# =============================================================================

def log(message: str, level: str = "INFO"):
    """Einfaches Logging mit Timestamp."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    prefix = {"INFO": "ℹ️ ", "OK": "✅", "WARN": "⚠️ ", "ERR": "❌", "SKIP": "⏭️ ", "DEL": "🗑️ "}
    print(f"[{timestamp}] {prefix.get(level, '')} {message}")


def run_command(cmd: list, check: bool = True) -> subprocess.CompletedProcess:
    """Führt einen Shell-Befehl aus."""
    if DRY_RUN:
        log(f"DRY RUN: {' '.join(cmd)}", "SKIP")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        log(f"Command failed: {result.stderr}", "ERR")
    return result


def check_dependencies():
    """Prüft ob exiftool und osxphotos installiert sind."""
    missing = []
    
    # exiftool prüfen
    result = subprocess.run(["which", "exiftool"], capture_output=True)
    if result.returncode != 0:
        missing.append("exiftool (brew install exiftool)")
    
    # osxphotos prüfen
    try:
        import osxphotos
    except ImportError:
        missing.append("osxphotos (pip install osxphotos)")
    
    if missing:
        log("Fehlende Abhängigkeiten:", "ERR")
        for dep in missing:
            print(f"    - {dep}")
        sys.exit(1)
    
    log("Alle Abhängigkeiten vorhanden", "OK")


def save_report(stats: ImportStats, report_path: Path):
    """Speichert einen detaillierten CSV-Report."""
    with open(report_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['Datei', 'Album', 'Status', 'Fehler'])
        for result in stats.results:
            writer.writerow([
                str(result.filepath),
                result.album,
                result.status,
                result.error_message or ''
            ])
    log(f"Report gespeichert: {report_path}", "OK")


def sanitize_album_name(name: str) -> str:
    """
    Bereinigt Albumnamen von problematischen Sonderzeichen.
    """
    # Zeichen die Probleme machen können
    replacements = {
        ",": " -",
        "/": "-",
        "\\": "-",
        ":": " -",
        '"': "'",
        "<": "",
        ">": "",
        "|": "-",
        "?": "",
        "*": "",
    }
    
    for char, replacement in replacements.items():
        name = name.replace(char, replacement)
    
    # Mehrfache Leerzeichen/Bindestriche aufräumen
    name = re.sub(r'\s+', ' ', name)        # Mehrfache Spaces → ein Space
    name = re.sub(r'-+', '-', name)          # Mehrfache Bindestriche → einer
    name = re.sub(r'\s*-\s*', ' - ', name)   # " - " normalisieren
    
    return name.strip()


# =============================================================================
# PHASE 1: ENTPACKEN
# =============================================================================

def extract_all_zips():
    """Entpackt alle Takeout-ZIPs in das Arbeitsverzeichnis."""
    log(f"Phase 1: Entpacke ZIPs aus {TAKEOUT_ZIP_DIR}")
    
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    
    zip_files = sorted(TAKEOUT_ZIP_DIR.glob("*.zip"))
    log(f"Gefunden: {len(zip_files)} ZIP-Dateien")
    
    for i, zip_path in enumerate(zip_files, 1):
        log(f"[{i}/{len(zip_files)}] Entpacke {zip_path.name}...")
        
        if DRY_RUN:
            continue
            
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                zf.extractall(WORK_DIR)
        except zipfile.BadZipFile:
            log(f"Fehlerhafte ZIP-Datei: {zip_path.name}", "ERR")
    
    log("Entpacken abgeschlossen", "OK")


# =============================================================================
# PHASE 2: METADATEN EINBETTEN
# =============================================================================

def find_json_for_media(media_path: Path) -> Path | None:
    """
    Findet die zugehörige JSON-Datei für eine Mediendatei.
    
    Google Takeout verwendet verschiedene Namenskonventionen:
    - foto.jpg -> foto.jpg.json
    - foto.jpg -> foto.json
    - foto(1).jpg -> foto(1).jpg.json
    """
    # Versuch 1: Exakter Name + .json
    json_path = media_path.with_suffix(media_path.suffix + '.json')
    if json_path.exists():
        return json_path
    
    # Versuch 2: Name ohne Extension + .json
    json_path = media_path.with_suffix('.json')
    if json_path.exists():
        return json_path
    
    # Versuch 3: Bei abgeschnittenen Dateinamen (Google kürzt manchmal)
    stem = media_path.stem
    parent = media_path.parent
    
    for json_file in parent.glob("*.json"):
        json_stem = json_file.stem.replace('.jpg', '').replace('.jpeg', '').replace('.heic', '')
        if stem.startswith(json_stem) or json_stem.startswith(stem):
            return json_file
    
    return None


def parse_google_json(json_path: Path) -> dict:
    """
    Parst eine Google Takeout JSON-Datei und extrahiert relevante Metadaten.
    """
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    result = {}
    
    # Aufnahmedatum
    if 'photoTakenTime' in data:
        ts = int(data['photoTakenTime'].get('timestamp', 0))
        if ts > 0:
            result['timestamp'] = ts
            result['date_taken'] = datetime.fromtimestamp(ts).strftime('%Y:%m:%d %H:%M:%S')
    
    # GPS-Koordinaten
    if 'geoData' in data:
        geo = data['geoData']
        lat = geo.get('latitude', 0)
        lon = geo.get('longitude', 0)
        if lat != 0 and lon != 0:
            result['latitude'] = lat
            result['longitude'] = lon
    
    if 'geoDataExif' in data and 'latitude' not in result:
        geo = data['geoDataExif']
        lat = geo.get('latitude', 0)
        lon = geo.get('longitude', 0)
        if lat != 0 and lon != 0:
            result['latitude'] = lat
            result['longitude'] = lon
    
    # Beschreibung
    if 'description' in data and data['description']:
        result['description'] = data['description']
    
    if 'title' in data and data['title']:
        result['title'] = data['title']
    
    return result


def apply_metadata_with_exiftool(media_path: Path, metadata: dict) -> bool:
    """Schreibt Metadaten mit exiftool in die Mediendatei."""
    if not metadata:
        return False
    
    cmd = ['exiftool', '-overwrite_original']
    
    if 'date_taken' in metadata:
        date = metadata['date_taken']
        cmd.extend([
            f'-DateTimeOriginal={date}',
            f'-CreateDate={date}',
            f'-ModifyDate={date}',
        ])
    
    if 'latitude' in metadata and 'longitude' in metadata:
        lat = metadata['latitude']
        lon = metadata['longitude']
        lat_ref = 'N' if lat >= 0 else 'S'
        lon_ref = 'E' if lon >= 0 else 'W'
        cmd.extend([
            f'-GPSLatitude={abs(lat)}',
            f'-GPSLatitudeRef={lat_ref}',
            f'-GPSLongitude={abs(lon)}',
            f'-GPSLongitudeRef={lon_ref}',
        ])
    
    if 'description' in metadata:
        desc = metadata['description']
        cmd.extend([
            f'-ImageDescription={desc}',
            f'-Caption-Abstract={desc}',
        ])
    
    cmd.append(str(media_path))
    
    result = run_command(cmd, check=False)
    return result.returncode == 0


def process_single_album(album_dir: Path) -> dict:
    """Verarbeitet ein einzelnes Album (Ordner)."""
    stats = {'total': 0, 'processed': 0, 'skipped': 0, 'errors': 0}
    album_name = sanitize_album_name(album_dir.name)
    
    if ALBUM_FILTER and album_name not in ALBUM_FILTER:
        return stats
    
    target_album_dir = OUTPUT_DIR / album_name
    target_album_dir.mkdir(parents=True, exist_ok=True)
    
    media_files = [f for f in album_dir.iterdir() 
                   if f.is_file() and f.suffix.lower() in MEDIA_EXTENSIONS]
    
    for media_path in media_files:
        stats['total'] += 1
        
        json_path = find_json_for_media(media_path)
        target_path = target_album_dir / media_path.name
        
        if DRY_RUN:
            log(f"  DRY: {media_path.name} -> {target_path}", "SKIP")
            stats['processed'] += 1
            continue
        
        try:
            shutil.copy2(media_path, target_path)
            
            if json_path:
                metadata = parse_google_json(json_path)
                if metadata:
                    apply_metadata_with_exiftool(target_path, metadata)
            
            stats['processed'] += 1
            
        except Exception as e:
            log(f"  Fehler bei {media_path.name}: {e}", "ERR")
            stats['errors'] += 1
    
    return stats


def process_all_albums():
    """Phase 2: Verarbeitet alle Alben und bettet Metadaten ein."""
    log(f"Phase 2: Verarbeite Alben und bette Metadaten ein")
    
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    google_photos_dir = None
    for candidate in [
        WORK_DIR / "Takeout" / "Google Photos",
        WORK_DIR / "Takeout" / "Google Fotos",
        WORK_DIR / "Google Photos",
    ]:
        if candidate.exists():
            google_photos_dir = candidate
            break
    
    if not google_photos_dir:
        log("Google Photos Ordner nicht gefunden!", "ERR")
        return
    
    log(f"Google Photos Ordner: {google_photos_dir}")
    
    album_dirs = [d for d in google_photos_dir.iterdir() if d.is_dir()]
    log(f"Gefunden: {len(album_dirs)} Alben")
    
    total_stats = {'total': 0, 'processed': 0, 'skipped': 0, 'errors': 0}
    
    for i, album_dir in enumerate(sorted(album_dirs), 1):
        album_name = album_dir.name
        log(f"[{i}/{len(album_dirs)}] Album: {album_name}")
        
        stats = process_single_album(album_dir)
        
        for key in total_stats:
            total_stats[key] += stats[key]
        
        if stats['total'] > 0:
            log(f"  -> {stats['processed']}/{stats['total']} Dateien verarbeitet", "OK")
    
    log(f"Phase 2 abgeschlossen: {total_stats['processed']}/{total_stats['total']} Dateien", "OK")


# =============================================================================
# PHASE 3: IMPORT IN APPLE PHOTOS (MIT VERBESSERTER FEHLERBEHANDLUNG)
# =============================================================================

def import_album_to_photos(album_dir: Path, stats: ImportStats) -> bool:
    album_name = sanitize_album_name(album_dir.name)
    
    media_files = [f for f in album_dir.iterdir() 
                   if f.is_file() and f.suffix.lower() in MEDIA_EXTENSIONS]
    
    if not media_files:
        return True  # Nichts zu importieren
    
    log(f"  Importiere {len(media_files)} Dateien in Album '{album_name}'...")

    health_check_interval = config["monitoring"]["health_check_interval"]
    max_memory = config["monitoring"]["max_photos_memory_gb"]
    max_errors = config["monitoring"]["max_errors_per_interval"]

    for i, media_file in enumerate(media_files):
        # Health Check alle X Dateien
        if i > 0 and i % health_check_interval == 0:
            is_healthy, reason = check_photos_health(max_memory, max_errors)
            log(f"  Health Check: {reason}", "INFO")

            if not is_healthy:
                action = config["monitoring"]["on_health_failure"]
                if not handle_health_failure(reason, action):
                    return False  # Import abbrechen
        
        result = import_single_file(media_file, album_name)
        stats.add_result(result)
        
        # ... rest wie vorher (delete after import etc.)
        
        # Bei Erfolg und DELETE_AFTER_IMPORT: Datei löschen
        if result.status == 'imported' and DELETE_AFTER_IMPORT and not DRY_RUN:
            try:
                media_file.unlink()
                stats.deleted += 1
                log(f"    {media_file.name} gelöscht", "DEL")
            except Exception as e:
                log(f"    Konnte {media_file.name} nicht löschen: {e}", "WARN")
        
        # Bei Duplikat: auch löschen (ist ja schon in Photos)
        elif result.status == 'duplicate' and DELETE_AFTER_IMPORT and not DRY_RUN:
            try:
                media_file.unlink()
                stats.deleted += 1
                log(f"    {media_file.name} (Duplikat) gelöscht", "DEL")
            except Exception as e:
                log(f"    Konnte {media_file.name} nicht löschen: {e}", "WARN")
    return True

def import_single_file(filepath: Path, album_name: str) -> ImportResult:
    """
    Importiert eine einzelne Datei in Apple Photos.
    
    Returns ImportResult mit Status.
    """
    if DRY_RUN:
        return ImportResult(filepath=filepath, album=album_name, status='skipped')
    
    cmd = [
        'osxphotos', 'import',
        str(filepath),
        '--album', album_name,
        '--skip-dups',      # Duplikate still überspringen
        '--verbose',
    ]
    
    try:
        result = subprocess.run(
            cmd, 
            capture_output=True, 
            text=True, 
            timeout=120  # 2 Minuten Timeout pro Datei
        )
        
        output = result.stdout + result.stderr
        
        # Prüfen ob es ein Duplikat war
        if 'duplicate' in output.lower() or 'skipping' in output.lower():
            return ImportResult(
                filepath=filepath, 
                album=album_name, 
                status='duplicate'
            )
        
        # Prüfen ob Import erfolgreich
        if result.returncode == 0:
            return ImportResult(
                filepath=filepath, 
                album=album_name, 
                status='imported'
            )
        else:
            return ImportResult(
                filepath=filepath, 
                album=album_name, 
                status='error',
                error_message=output[:200]  # Erste 200 Zeichen der Fehlermeldung
            )
            
    except subprocess.TimeoutExpired:
        return ImportResult(
            filepath=filepath, 
            album=album_name, 
            status='error',
            error_message='Timeout nach 120 Sekunden'
        )
    except Exception as e:
        return ImportResult(
            filepath=filepath, 
            album=album_name, 
            status='error',
            error_message=str(e)
        )


def import_to_apple_photos():
    """
    Phase 3: Importiert die verarbeiteten Fotos in Apple Photos.
    
    Verbesserte Version mit:
    - Einzeldatei-Import für bessere Fehlerbehandlung
    - Skip Duplicates
    - Löschen nach erfolgreichem Import
    """
    log("Phase 3: Import in Apple Photos")
    
    if not OUTPUT_DIR.exists():
        log(f"Output-Verzeichnis nicht gefunden: {OUTPUT_DIR}", "ERR")
        return
    
    album_dirs = [d for d in OUTPUT_DIR.iterdir() if d.is_dir()]
    log(f"Zu importieren: {len(album_dirs)} Alben")
    
    stats = ImportStats()
    
    for i, album_dir in enumerate(sorted(album_dirs), 1):
        album_name = album_dir.name
        log(f"[{i}/{len(album_dirs)}] Album: {album_name}")
        
        if not import_album_to_photos(album_dir, stats):
            log("Import abgebrochen aufgrund von Health-Check Fehlern", "ERR")
            break


        # Zwischenstand alle 10 Alben
        if i % 10 == 0:
            log(f"  Zwischenstand: {stats.summary()}", "INFO")
    
    # Finaler Report
    log("="*60, "INFO")
    log(f"Import abgeschlossen: {stats.summary()}", "OK")
    
    # CSV Report speichern
    report_path = OUTPUT_DIR / f'import_report_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
    save_report(stats, report_path)
    
    # Fehler auflisten
    errors = [r for r in stats.results if r.status == 'error']
    if errors:
        log(f"\n{len(errors)} Fehler aufgetreten:", "WARN")
        for err in errors[:20]:  # Erste 20 Fehler anzeigen
            log(f"  {err.filepath.name}: {err.error_message}", "ERR")
        if len(errors) > 20:
            log(f"  ... und {len(errors) - 20} weitere (siehe Report)", "WARN")


# =============================================================================
# ALTERNATIVE: BATCH-IMPORT (schneller, aber weniger Kontrolle)
# =============================================================================

def import_to_apple_photos_batch():
    """
    Alternative: Batch-Import eines ganzen Albums auf einmal.
    
    Schneller als Einzeldatei-Import, aber weniger Kontrolle über Fehler.
    Verwendet osxphotos mit --skip-dups und --report.
    """
    log("Phase 3 (Batch): Import in Apple Photos")
    
    if not OUTPUT_DIR.exists():
        log(f"Output-Verzeichnis nicht gefunden: {OUTPUT_DIR}", "ERR")
        return
    
    report_path = OUTPUT_DIR / f'import_report_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
    
    cmd = [
        'osxphotos', 'import',
        str(OUTPUT_DIR),
        '--album', '{filepath.parent.name}',
        '--walk',
        '--skip-dups', # Duplikate still überspringen!
        '--verbose',
        '--report', str(report_path),
    ]
    
    log(f"Starte osxphotos batch import...")
    log(f"Befehl: {' '.join(cmd)}")
    
    if DRY_RUN:
        log("DRY RUN: Überspringe tatsächlichen Import", "SKIP")
        return
    
    try:
        # Batch-Import ausführen
        result = subprocess.run(cmd, capture_output=False)
        
        if result.returncode == 0:
            log("Batch-Import abgeschlossen!", "OK")
            
            # Report auslesen und erfolgreich importierte löschen
            if DELETE_AFTER_IMPORT and report_path.exists():
                delete_imported_files_from_report(report_path)
        else:
            log(f"Batch-Import mit Fehlern beendet (returncode: {result.returncode})", "WARN")
            
    except Exception as e:
        log(f"Import fehlgeschlagen: {e}", "ERR")


def delete_imported_files_from_report(report_path: Path):
    """
    Liest den osxphotos Import-Report und löscht erfolgreich importierte Dateien.
    """
    log("Lösche erfolgreich importierte Dateien...", "INFO")
    
    deleted_count = 0
    error_count = 0
    
    try:
        with open(report_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            
            for row in reader:
                # osxphotos Report-Format prüfen
                filepath = row.get('filename') or row.get('filepath') or row.get('file')
                status = row.get('imported') or row.get('status')
                
                if not filepath:
                    continue
                
                filepath = Path(filepath)
                
                # Nur löschen wenn importiert oder Duplikat
                if status in ['True', 'true', '1', 'imported', 'duplicate', 'skipped_duplicate']:
                    if filepath.exists():
                        try:
                            filepath.unlink()
                            deleted_count += 1
                        except Exception as e:
                            log(f"  Konnte nicht löschen: {filepath.name} - {e}", "WARN")
                            error_count += 1
    
    except Exception as e:
        log(f"Fehler beim Lesen des Reports: {e}", "ERR")
        return
    
    log(f"Gelöscht: {deleted_count} Dateien, Fehler: {error_count}", "OK")


# =============================================================================
# HAUPTPROGRAMM
# =============================================================================

def print_config():
    """Zeigt die aktuelle Konfiguration an."""
    print("\n" + "="*60)
    print("GOOGLE TAKEOUT → APPLE PHOTOS MIGRATION v2")
    print("="*60)
    print(f"  Takeout ZIPs:       {TAKEOUT_ZIP_DIR}")
    print(f"  Arbeitsverz.:       {WORK_DIR}")
    print(f"  Output:             {OUTPUT_DIR}")
    print(f"  Trockenlauf:        {'JA' if DRY_RUN else 'NEIN'}")
    print(f"  Löschen nach Import: {'JA' if DELETE_AFTER_IMPORT else 'NEIN'}")
    print(f"  Album-Filter:       {ALBUM_FILTER if ALBUM_FILTER else 'Alle'}")
    print("="*60 + "\n")


def main():
    """Hauptfunktion - führt alle Phasen aus."""
    print_config()
    
    check_dependencies()
    
    print("\nWelche Phasen ausführen?")
    print("  1 = Nur entpacken")
    print("  2 = Nur Metadaten verarbeiten")
    print("  3 = Import in Apple Photos (Einzeldatei-Modus, sicherer)")
    print("  3B = Import in Apple Photos (Batch-Modus, schneller)")
    print("  A = Alle Phasen (mit Einzeldatei-Import)")
    print("  T = Test-Modus (DRY_RUN)")
    print()
    
    choice = input("Auswahl [A]: ").strip().upper() or 'A'
    
    global DRY_RUN
    if choice == 'T':
        DRY_RUN = True
        choice = 'A'
        log("TEST-MODUS aktiviert", "WARN")
    
    if choice in ['1', 'A']:
        extract_all_zips()
    
    if choice in ['2', 'A']:
        process_all_albums()
    
    if choice == '3':
        import_to_apple_photos()
    elif choice == '3B':
        import_to_apple_photos_batch()
    elif choice == 'A':
        import_to_apple_photos()
    
    print("\n" + "="*60)
    log("Migration abgeschlossen!", "OK")
    print("="*60)


if __name__ == '__main__':
    main()
