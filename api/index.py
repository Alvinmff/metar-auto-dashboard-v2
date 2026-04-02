from flask import Flask, render_template, request, send_file, jsonify, make_response  # pyre-ignore
import json
import requests  # pyre-ignore
import pandas as pd  # pyre-ignore
import os
import re
import io   
from datetime import datetime
from datetime import timedelta
from io import BytesIO
import time
import threading
from collections import deque
import sys
import traceback
from typing import Optional, List, Dict, Any, Union
try:
    from .sheets_handler import sheets_handler  # type: ignore
except (ImportError, ValueError):
    from sheets_handler import sheets_handler  # type: ignore
 
def format_indonesian_date(dt):
    """Format datetime ke format Indonesia: Kamis, 02 April 2026"""
    days = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
    months = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", 
              "Juli", "Agustus", "September", "Oktober", "November", "Desember"]
    
    day_name = days[dt.weekday()]
    month_name = months[dt.month - 1]
    
    return f"{day_name}, {dt.day:02d} {month_name} {dt.year}"

# Resolve absolute paths for Vercel
# Vercel structured as /var/task/api/index.py
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
template_dir = os.path.join(project_root, "templates")
static_dir = os.path.join(project_root, "static")

app = Flask(__name__, 
            template_folder=template_dir, 
            static_folder=static_dir)
application = app # Alias for compatibility

# ============ KONFIGURASI UNTUK VERCEL ============
# Vercel Environment Detection
IS_VERCEL = os.environ.get("VERCEL") == "true" or os.environ.get("VERCEL_ENV") is not None or os.path.exists("/var/task")

# Gunakan /tmp untuk writeable storage di Vercel
# Pada Vercel, hanya /tmp yang bisa ditulisi (writable)
ROOT_CSV = os.path.join(project_root, "metar_history.csv")

if IS_VERCEL:
    CSV_FILE = "/tmp/metar_history.csv"
    print("[INIT] Running on VERCEL detected - Using /tmp/ storage", file=sys.stderr)
    
    # 🔥 HYBRID HISTORY STRATEGY:
    # Jika /tmp/metar_history.csv belum ada, copykan dari root folder (Git)
    # ATAU sync dari Google Sheets untuk data terbaru
    if not os.path.exists(CSV_FILE):
        sync_success = False
        if IS_VERCEL:
            print("[INIT] Attempting sync from Google Sheets...", file=sys.stderr)
            sync_success = sheets_handler.sync_to_local(CSV_FILE)
        
        if not sync_success and os.path.exists(ROOT_CSV):
            try:
                import shutil
                shutil.copy2(ROOT_CSV, CSV_FILE)
                print("[INIT] Base history copied from project root to /tmp/", file=sys.stderr)
            except Exception as e:
                print(f"[INIT] Failed to copy base history: {e}", file=sys.stderr)
else:
    CSV_FILE = ROOT_CSV
    print("[INIT] Running locally - Using local storage", file=sys.stderr)

# =========================
# FAVICON HANDLER
# =========================
@app.route('/favicon.ico')
def favicon_ico():
    """Handle favicon.ico requests"""
    return '', 204  # No content

@app.route('/favicon.png')
def favicon_png():
    """Handle favicon.png requests"""
    return '', 204  # No content

# =========================
# ERROR HANDLERS
# =========================
@app.errorhandler(404)
def not_found_error(error):
    """Handle 404 errors gracefully"""
    # Log to stderr but don't crash
    print(f"[404] Not Found: {request.path}", file=sys.stderr)
    
    # If request looks like favicon, return 204 without body
    if 'favicon' in request.path.lower():
        return '', 204
    
    # For API requests, return JSON
    if request.path.startswith('/api/'):
        return jsonify({"error": "Not found", "path": request.path}), 404
    
    # For web requests, return simple message
    return "Page not found", 404

@app.errorhandler(Exception)
def handle_exception(e):
    """Global error handler to catch all exceptions"""
    error_msg = f"ERROR: {str(e)}"
    print(error_msg, file=sys.stderr)
    
    # Detailed log for server/stderr
    import traceback
    traceback.print_exc()

    # Generic error for client
    return jsonify({
        "error": str(e)
    }), 500

# System Control State
auto_fetch = True
last_metar_update = None

# Cache for latest METAR data (used by polling endpoint)
latest_metar_data = {}

# ============ HELPER FUNCTIONS ============

def extract_temp(metar):
    """Extract temperature from METAR (XX/XX)"""
    if not metar: return None
    match = re.search(r'(\d{2})/(\d{2})', str(metar))
    return int(match.group(1)) if match else None

def extract_pressure(metar):
    """Extract QNH pressure from METAR (QXXXX)"""
    if not metar: return None
    match = re.search(r'Q(\d{4})', str(metar))
    return int(match.group(1)) if match else None

# ==========================================

# ==========================================

# Wind history storage for Wind Rose
wind_history = deque(maxlen=500)

# Store wind data for Wind Rose
def store_wind(parsed, station="WARR"):
    # Skip if wind direction is "VRB" (variable) or missing
    wind_dir = parsed.get("wind_dir")
    if not wind_dir or wind_dir == "VRB" or not parsed.get("wind_speed_kt"):
        return
    
    try:
        wind_history.append({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "station": station,
            "dir": int(wind_dir),
            "speed": float(parsed["wind_speed_kt"])
        })
        # print(f"[WIND] Stored: dir={wind_dir}, speed={parsed['wind_speed_kt']}kt")
    except (ValueError, TypeError) as e:
        pass

def load_wind_history():
    """Load historical wind data from CSV into memory for Wind Rose"""
    if not os.path.exists(CSV_FILE):
        return
    try:
        df = pd.read_csv(CSV_FILE)
        if df.empty:
            return
        
        # Take last 500 rows for the Wind Rose
        df = df.tail(500)
        
        count = 0
        for _, row in df.iterrows():
            metar = str(row["metar"]) if pd.notna(row["metar"]) else ""
            if not metar:
                continue
            
            # Use regex to quickly extract wind dir and speed
            wind_match = re.search(r'\b(\d{3})(\d{2,3})(G\d{2,3})?KT\b', metar)
            if wind_match:
                try:
                    wind_history.append({
                        "time": str(row["time"]),
                        "station": row["station"],
                        "dir": int(wind_match.group(1)),
                        "speed": float(wind_match.group(2))
                    })
                    count += 1
                except (ValueError, TypeError):
                    continue
        print(f"✅ Loaded {count} wind records from {CSV_FILE} for Wind Rose")
    except Exception as e:
        print(f"❌ Failed to load wind history: {e}")




import math

def calculate_crosswind(wind_dir, wind_speed, runway_heading):
    angle = abs(wind_dir - runway_heading)
    angle_rad = math.radians(angle)
    return round(wind_speed * math.sin(angle_rad), 1)


#Deteksi thunderstorm dari raw METAR
def detect_thunderstorm(raw_metar: str) -> bool:
    if not raw_metar: return False
    ts_codes = ["TS", "TSRA", "VCTS", "+TS", "TSGR", "-TS", "TSRA", "+TSRA", "-TSRA"]
    return any(code in raw_metar for code in ts_codes)


# =========================
# GET METAR FROM NOAA
# =========================
def get_metar(station_code):
    # Headers to mimic browser request
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
    }
    
    # Try primary NOAA source
    url = f"https://tgftp.nws.noaa.gov/data/observations/metar/stations/{station_code.upper()}.TXT"
    print(f"[DEBUG] Fetching METAR from: {url}")
    
    try:
        response = requests.get(url, timeout=15, headers=headers)
        print(f"[DEBUG] Response status: {response.status_code}")
        if response.status_code == 200:
            lines = response.text.strip().split("\n")
            print(f"[DEBUG] Raw response lines: {lines}")
            
            # Find the METAR line (usually the last line that starts with station code)
            for line in lines:
                line = line.strip()
                if line and line.startswith(station_code.upper()):
                    print(f"[DEBUG] METAR retrieved: {line}")
                    return line
            
            # Fallback: if no line starts with station, take last non-empty line
            for line in reversed(lines):
                line = line.strip()
                if line:
                    print(f"[DEBUG] METAR retrieved (fallback): {line}")
                    return line
                    
    except requests.exceptions.Timeout:
        print("[ERROR] Request timeout while fetching METAR from NOAA")
    except requests.exceptions.ConnectionError as e:
        print(f"[ERROR] Connection error while fetching METAR from NOAA: {e}")
    except Exception as e:
        print(f"[ERROR] Exception while fetching METAR from NOAA: {e}")
    
    # Try alternative source - AVWX (backup)
    print("[DEBUG] Trying alternative METAR source...")
    alt_url = f"https://avwx.rest/api/metar/{station_code.upper()}"
    try:
        response = requests.get(alt_url, timeout=15, headers=headers)
        if response.status_code == 200:
            data = response.json()
            if "raw" in data:
                metar = data["raw"]
                print(f"[DEBUG] METAR from alternative source: {metar}")
                return metar
    except Exception as e:
        print(f"[DEBUG] Alternative source also failed: {e}")
    
    print("[ERROR] All METAR sources failed!")
    return None

# =========================
# WEATHER CODES
# =========================
WEATHER_CODES = [
    "DZ", "-RA", "RA","SN","SG","IC","PL","GR","GS",
    "UP","BR","FG","FU","VA","DU","SA","HZ",
    "PO","SQ","FC","SS","DS","TS","SH", "TSRA",
    "+TSRA", "-TSRA", "-TS", "+TS", "VCTS"
]

# =========================
# DETECT METAR SPECIAL REPORT TYPE
# =========================
def detect_metar_report_type(metar: str) -> str:
    """
    Detect if METAR is a special report (COR, CCA, AMD, SPECI)
    Returns: 'COR', 'AMD', 'SPECI', or 'METAR'
    """
    if not metar:
        return "METAR"
    
    metar = metar.upper()

    if " SPECI " in metar or metar.startswith("SPECI "):
        return "SPECI"

    if " AMD " in metar or metar.startswith("METAR AMD"):
        return "AMD"

    if " COR " in metar or metar.startswith("METAR COR") or " CCA " in metar or "CCA" in metar:
        return "COR"

    return "METAR"

# =========================
# PARSE METAR
# =========================
def parse_metar(metar: str) -> dict:

    data: dict = {
        "station": None,
        "day": None,
        "hour": None,
        "minute": None,
        "wind_dir": None,
        "wind_speed_kt": None,
        "wind_gust_kt": None,
        "visibility_m": None,
        "weather": None,
        "cloud": None,
        "temperature_c": None,
        "dewpoint_c": None,
        "pressure_hpa": None,
        "trend": None,
        "tempo": None,  # Add tempo field
        "report_type": "METAR"   # 🔥 NEW: Tracks COR, AMD, SPECI, METAR
    }

    # Detect special report type
    data["report_type"] = detect_metar_report_type(metar)

    clean_metar = metar.replace("=", "")
    parts = clean_metar.split()

    # First, extract TEMPO clause before the main parsing
    # This removes TEMPO from METAR so weather isn't captured from TEMPO section
    tempo_match = re.search(r'TEMPO\s+(.+)', metar)
    if tempo_match:
        tempo_content = tempo_match.group(1).strip()
        # Store the full TEMPO content
        data["tempo"] = tempo_content
        # Remove TEMPO clause from METAR for parsing (to avoid capturing weather from TEMPO)
        main_metar = re.sub(r'\s+TEMPO\s+.+', '', metar)
    else:
        main_metar = metar
    
    # Parse the main METAR (without TEMPO) for weather and other fields
    parts: list[str] = main_metar.replace("=", "").split()

    for part in parts:

        if len(part) == 4 and part.isalpha() and data["station"] is None:
            data["station"] = part

        if part.endswith("Z") and len(part) == 7:
            data["day"] = part[0] + part[1]
            data["hour"] = part[2] + part[3]
            data["minute"] = part[4] + part[5]

        # WIND PARSER (robust aviation parser)
        if part.endswith("KT"):

            wind_match = re.match(r"^(\d{3}|VRB)(\d{2,3})(G(\d{2,3}))?KT$", part)

            if wind_match:
                data["wind_dir"] = wind_match.group(1)
                data["wind_speed_kt"] = wind_match.group(2)

                if wind_match.group(4):
                    data["wind_gust_kt"] = wind_match.group(4)
                else:
                    data["wind_gust_kt"] = None

        if part.isdigit() and len(part) == 4:
            data["visibility_m"] = int(part)

        if part in ["HZ","BR","FG","DZ","SN","SG","IC","PL","GR","GS","UP","RA","+RA","-RA","TSRA","+TSRA","TS","+TS","-TS","VCTS","SH","DS","SS","-TSRA"]:
            # Only set weather if not already set (get first weather occurrence)
            if data["weather"] is None:
                data["weather"] = part

        if part.startswith(("FEW","SCT","BKN","OVC")):
            data["cloud"] = part

        if "/" in part and len(part) == 5:
            t, d = part.split("/")
            data["temperature_c"] = t
            data["dewpoint_c"] = d

        if part.startswith("Q"):
            qnh_match = re.match(r"Q(\d{4})", part)
            if qnh_match:
                data["pressure_hpa"] = qnh_match.group(1)

        if part == "NOSIG":
            data["trend"] = part

    # If there's TEMPO data, set trend to include it
    if data["tempo"]:
        data["trend"] = "TEMPO " + data["tempo"]

    # =========================
    # STATUS COLOR LOGIC
    # =========================
    status = "normal"  # default green
    
    # Check for danger conditions
    if detect_thunderstorm(metar):
        status = "danger"  # red - thunderstorm
    elif data["visibility_m"] is not None and data["visibility_m"] < 3000:
        status = "danger"  # red - low visibility < 3000m
    elif data["weather"] and data["weather"] != "NIL":
        # Check for warning conditions
        warning_weather = ["RA", "FG", "HZ", "BR", "SH", "DS", "SS", "FC"]
        if any(code in data["weather"] for code in warning_weather):
            status = "warning"  # yellow/orange - moderate conditions
        elif "+" in data["weather"] or "TS" in data["weather"]:
            status = "danger"  # red - severe weather
    
    # Check visibility for warning (3-5km)
    if data["visibility_m"] is not None and status != "danger":
        vis_val = data["visibility_m"]
        if 3000 <= vis_val <= 5000:
            status = "warning"  # yellow - moderate visibility
    
    data["status"] = status

    return data

# =========================
# HELPER: Format visibility value
# =========================
def format_visibility(vis_m):
    """Convert visibility in meters to display format"""
    if vis_m is None:
        return "NIL"
    
    # Specific visibility values
    if vis_m >= 10000 or vis_m == 9999:
        return "10 KM"
    elif vis_m == 8000:
        return "8 KM"
    elif vis_m == 7000:
        return "7 KM"
    elif vis_m == 6000:
        return "6 KM"
    elif vis_m == 5000:
        return "5 KM"
    elif vis_m == 4000:
        return "4 KM"
    elif vis_m == 3000:
        return "3 KM"
    elif vis_m == 2000:
        return "2 KM"
    elif vis_m == 1500:
        return "1.5 KM"
    elif vis_m == 1000:
        return "1 KM"
    elif vis_m >= 1000:
        return f"{vis_m // 1000} KM"
    else:
        return f"{vis_m} M"

# =========================
# HELPER: Convert parsed data to display format
# =========================
def format_parsed_for_display(parsed):
    """Convert parsed METAR data to display format for QAM and narrative"""
    display = {}
    
    # Station
    display["station"] = parsed.get("station") or "-"
    
    # Wind - format: 000°/00KT or 000°/00G00KT (with gust)
    if parsed.get("wind_dir") and parsed.get("wind_speed_kt"):
        if parsed.get("wind_gust_kt"):
            display["wind"] = f"{parsed['wind_dir']}°/{parsed['wind_speed_kt']}G{parsed['wind_gust_kt']}KT"
        else:
            display["wind"] = f"{parsed['wind_dir']}°/{parsed['wind_speed_kt']}KT"
    else:
        display["wind"] = "NIL"
    
    # Visibility - format: 10 KM or 5000 M
    display["visibility"] = format_visibility(parsed.get("visibility_m"))
    
    # Weather
    display["weather"] = parsed.get("weather") or "NIL"
    
    # Cloud - format: FEW010FT, BKN025FT CB, etc.
    if parsed.get("cloud"):
        cloud = parsed["cloud"]
        try:
            # cloud format in new parse: "BKN025" or "FEW015CB"
            amount = cloud[:3]
            height = int(cloud[3:6]) * 100
            cloud_str = f"{amount} {height}FT"
            if "CB" in cloud:
                cloud_str += " CB"
            elif "TCU" in cloud:
                cloud_str += " TCU"
            display["cloud"] = cloud_str
        except:
            display["cloud"] = cloud
    else:
        display["cloud"] = "NIL"
    
    # Temperature/Dewpoint - format: 28/24
    if parsed.get("temperature_c") and parsed.get("dewpoint_c"):
        display["temp_td"] = f"{parsed['temperature_c']}/{parsed['dewpoint_c']}"
    else:
        display["temp_td"] = "NIL"
    
    # Pressure QNH/QFE
    display["qnh"] = parsed.get("pressure_hpa") or "NIL"
    display["qfe"] = parsed.get("pressure_hpa") or "NIL"
    
    # Trend
    display["trend"] = parsed.get("trend") or "NIL"
    
    # Time info
    display["day"] = parsed.get("day") or "-"
    display["hour"] = parsed.get("hour") or "-"
    display["minute"] = parsed.get("minute") or "-"
    
    return display

# =========================
# EXTRACT SUPPLEMENTARY INFORMATION
# =========================
def extract_supplementary_info(metar: str) -> str:
    """
    Extract supplementary information indicators (Recent Weather) from METAR.
    Returns: Joined string of codes (e.g., "RERA, RETS") or "NIL"
    """
    if not metar:
        return "NIL"
        
    # List of common supplementary "RE" indicators
    supp_codes = [
        "RERA", "RETS", "RETSRA", "RESN", "REGR", "REDZ", "RESH", "REVC", 
        "REPL", "REGS", "REUP", "REBR", "REFG", "RESA", "REDU", "REHZ", "REPY"
    ]
    
    found_indicators = []
    metar_upper = metar.upper()
    
    # Check for each indicator in the METAR string
    for code in supp_codes:
        # Match exact word to avoid partial matches
        pattern = r'(?:^|\s)' + re.escape(code) + r'(?:\s|$|=)'
        if re.search(pattern, metar_upper):
            found_indicators.append(code)
    
    # Return formatted string or NIL
    if found_indicators:
        return ", ".join(found_indicators)
    return "NIL"

# =========================
# GENERATE QAM FORMAT
# =========================
def generate_qam(station, parsed, raw_metar):
    # Convert parsed data to display format
    display = format_parsed_for_display(parsed)
    
    # Extract supplementary info
    supp_info = extract_supplementary_info(raw_metar)
    # Add trailing dot if not NIL to match example "RA."
    if supp_info != "NIL":
        supp_info += "."
    
    # Get time from raw METAR if not in parsed
    match = re.search(r'(\d{2})(\d{2})(\d{2})Z', raw_metar)
    if match:
        day, hour, minute = match.groups()
        now = datetime.utcnow()
        date_str = f"{day}/{now.strftime('%m/%Y')}"
        time_str = f"{hour}.{minute}"
    elif display["day"] != "-":
        date_str = f"{display['day']}/{datetime.utcnow().strftime('%m/%Y')}"
        time_str = f"{display['hour']}.{display['minute']}"
    else:
        date_str = "-"
        time_str = "-"

    qam = f"""MET REPORT (QAM)
BANDARA JUANDA ({station})
DATE     : {date_str}
TIME     : {time_str} UTC
========================
WIND     : {display['wind']}
VIS      : {display['visibility']}
WEATHER  : {display['weather']}
CLOUD    : {display['cloud']}
TT/TD    : {display['temp_td']}
QNH      : {display['qnh']} MB
QFE      : {display['qfe']} MB
TREND    : {display['trend']}
SUPPLMNT : {supp_info}
"""
    return qam

# =========================
# GENERATE NARRATIVE TEXT - FINAL IMPROVED VERSION
# =========================
def generate_metar_narrative(parsed, raw_metar=None):
    """Generate Indonesian narrative text from METAR data with natural language format"""
    if not parsed:
        return "Data METAR tidak valid."
    
    display = format_parsed_for_display(parsed)
    # Rename 'text' to 'narrative' to avoid potential shadowing or LiteralString inference issues
    narrative: list[str] = []
    
    # Get station info
    station = display.get('station', 'Unknown')
    if raw_metar and (not station or station == "-"):
        station_match = re.match(r'([A-Z]{4})', raw_metar)
        if station_match:
            station = station_match.group(1)
    if not station or station == "-":
        station = "Unknown"
    
    # Get observation time
    day, hour, minute = "??", "??", "??"
    month_indonesian = ""
    year = datetime.utcnow().year
    
    current_month_name = datetime.utcnow().strftime("%B")
    
    if raw_metar:
        time_match = re.search(r'(\d{2})(\d{2})(\d{2})Z', raw_metar)
        if time_match:
            day, hour, minute = time_match.groups()
    elif display.get('day') != "-":
        day = display.get('day', '??')
        hour = display.get('hour', '??')
        minute = display.get('minute', '??')
    
    # Convert month name to Indonesian
    month_map = {
        "January": "Januari", "February": "Februari", "March": "Maret", "April": "April",
        "May": "Mei", "June": "Juni", "July": "Juli", "August": "Agustus",
        "September": "September", "October": "Oktober", "November": "November", "December": "Desember"
    }
    month_indonesian = month_map.get(current_month_name, current_month_name)
    
    # Opening sentence
    narrative.append(f"Observasi cuaca di Bandara Juanda ({station}) pada tanggal {day} {month_indonesian} {year} pukul {hour}:{minute} UTC menunjukkan kondisi berikut:")
    
    # Wind information - FORMAT: "160° derajat 13 Gust 27 Knot"
    wind = display.get('wind', '')
    if wind and wind != 'NIL':
        # Parse wind format: 160°/13G27KT atau 160°/13KT
        wind_match = re.match(r'(\d{3})°/(\d{2,3})(G(\d{2,3}))?KT', str(wind))
        if wind_match:
            # Removed leading zeros (060 -> 60)
            wind_dir = str(int(wind_match.group(1)))
            wind_speed = wind_match.group(2)
            wind_gust = wind_match.group(4)
            
            if wind_gust:
                wind_text = f"Angin dari arah {wind_dir}° derajat dengan kecepatan angin {wind_speed} Gust {wind_gust} Knot."
            else:
                wind_text = f"Angin dari arah {wind_dir}° derajat dengan kecepatan angin {wind_speed} Knot."
            narrative.append(wind_text)
        else:
            narrative.append(f"Angin dari arah {wind}.")
    
    # Visibility information
    vis = display.get('visibility', '')
    if vis and vis != 'NIL':
        if vis == "10 KM":
            narrative.append("Jarak pandang sekitar 10 kilometer.")
        elif "KM" in str(vis):
            km_val = str(vis).replace("KM", "").strip()
            # Hilangkan .0 jika ada
            km_val_clean = km_val.replace(".0", "") if ".0" in km_val else km_val
            narrative.append(f"Jarak pandang sekitar {km_val_clean} kilometer.")
        elif "M" in str(vis):
            m_val = str(vis).replace("M", "").strip()
            narrative.append(f"Jarak pandang sekitar {m_val} meter.")
        else:
            narrative.append(f"Visibilitas {vis}.")
    
    # Define weather map at function level for use in both Main and TEMPO sections
    weather_map: Dict[str, str] = {
        "HZ": "kabut asap", "RA": "hujan", "+RA": "hujan lebat", "-RA": "hujan ringan",
        "TS": "badai petir", "-TS": "badai petir ringan", "+TS": "badai petir kuat",
        "TSRA": "badai petir disertai hujan", "-TSRA": "badai petir ringan disertai hujan", 
        "+TSRA": "badai petir kuat disertai hujan", "VCTS": "badai petir di sekitar",
        "SH": "hujan shower", "SHRA": "hujan shower", "DS": "debu pasir", "SS": "pasir badai",
        "FG": "kabut", "BR": "kabut tipis", "DZ": "gerimis", "SN": "salju", "GR": "hujan es",
        "SQ": "angin kencang", "FC": "puting beliung", "VCTS": "badai petir di sekitar"
    }

    # Weather information
    weather = display.get('weather', '')
    if weather and weather != 'NIL':
        weather_desc = weather_map.get(str(weather), str(weather))
        narrative.append(f"Terdapat fenomena cuaca berupa {weather_desc}.")
    
    # Cloud information - FORMAT: "awan banyak pada ketinggian 1800 kaki CB (Cumulonimbus)"
    cloud = display.get('cloud', '')
    if cloud and cloud != 'NIL':
        cloud_map = {
            "FEW": "awan sedikit", "SCT": "awan tersebar", "BKN": "awan banyak", "OVC": "awan menutup langit"
        }
        # Parse cloud: BKN 1800FT CB atau BKN018CB
        cloud_match = re.match(r'([A-Z]{3})\s*(\d+)(?:FT)?(CB|TCU)?', str(cloud))
        if cloud_match:
            c_type, c_height, c_extra = cloud_match.groups()
            c_desc = cloud_map.get(c_type, c_type)
            if c_extra == "CB":
                narrative.append(f"Terdapat {c_desc} pada ketinggian {c_height} kaki CB (Cumulonimbus).")
            elif c_extra == "TCU":
                narrative.append(f"Terdapat {c_desc} pada ketinggian {c_height} kaki TCU (Towering Cumulus).")
            else:
                narrative.append(f"Terdapat {c_desc} pada ketinggian {c_height} kaki.")
        else:
            narrative.append(f"Awan: {cloud}.")
    
    # Temperature and dewpoint
    temp_td = display.get('temp_td', '')
    if temp_td and temp_td != 'NIL':
        tt_match = re.match(r'(\d{2})/(\d{2})', str(temp_td))
        if tt_match:
            t_val, d_val = tt_match.groups()
            narrative.append(f"Suhu {t_val}°C dengan titik embun {d_val}°C.")
    
    # Pressure
    qnh = display.get('qnh', '')
    if qnh and qnh != 'NIL':
        narrative.append(f"Tekanan udara {qnh} hPa.")
    
    # TREND / TEMPO - FORMAT: "hingga pukul 08:30, dengan visibilitas 5 km, disertai hujan"
    trend_val = str(display.get('trend', ''))
    if trend_val and trend_val != 'NIL':
        if trend_val == 'NOSIG':
            narrative.append("Tidak ada perubahan signifikan dalam waktu dekat.")
        elif 'TEMPO' in trend_val.upper():
            tempo_items: list[str] = []
            # Extract time using groups to avoid variable slicing
            t_match = re.search(r'TL(\d{2})(\d{2})', trend_val)
            if t_match:
                hh, mm = t_match.groups()
                tempo_items.append(f"hingga pukul {hh}:{mm}")
            
            # Extract visibility (excluding digits inside time markers like TL0930)
            # We look for 4 digits NOT preceded by L (from TL), T (from AT), M (from FM) or Q
            v_match = re.search(r'(?<![LTMAQ\d])(\d{4})(?![\dZ])', trend_val)
            if v_match:
                raw_v = int(v_match.group(1))
                if raw_v >= 10000 or raw_v == 9999:
                    v_str = "10 km"
                elif raw_v >= 1000:
                    v_str = f"{raw_v // 1000} km" if raw_v % 1000 == 0 else f"{raw_v / 1000:.1f} km".replace(".0", "")
                else:
                    v_str = f"{raw_v} m"
                tempo_items.append(f"dengan visibilitas {v_str}")
            
            # Extract weather (Sync with main weather_map)
            # Find ALL matching phenomena in TEMPO segment
            tempo_weathers: list[str] = []
            codes = sorted(weather_map.keys(), key=len, reverse=True)
            
            # Divide trend_val into tokens to avoid partial matches (like 'RA' matching in 'TSRA')
            tempo_tokens = trend_val.split()
            for token in tempo_tokens:
                for w_code in codes:
                    if w_code == token:
                        w_desc = weather_map.get(w_code)
                        if w_desc:
                            tempo_weathers.append(w_desc)
                        break # Only one weather code per token
            
            if tempo_weathers:
                # Deduplicate while preserving order without using dict.fromkeys to satisfy linters
                unique_weathers: list[str] = []
                for w in tempo_weathers:
                    if w not in unique_weathers:
                        unique_weathers.append(w)
                
                tempo_items.append(f"disertai {' dan '.join(unique_weathers)}")
            
            if tempo_items:
                narrative.append(f"Dalam waktu dekat, diperkirakan akan terjadi {', '.join(tempo_items)}.")
            else:
                narrative.append(f"Tren: {trend_val}.")
        else:
            narrative.append(f"Tren: {trend_val}.")
    
    return " ".join(narrative)

# =========================
# HELPER FUNCTIONS FOR CHART DATA
# =========================
def extract_temp(metar):
    """Extract temperature from METAR string"""
    if not metar or not isinstance(metar, str):
        return 0
    try:
        parts = metar.split()
        for part in parts:
            if '/' in part and part != 'NIL':
                try:
                    temp = part.split('/')[0]
                    return int(temp) if temp.lstrip('-').isdigit() else 0
                except:
                    return 0
    except:
        return 0
    return 0

def extract_pressure(metar):
    """Extract pressure (QNH) from METAR string"""
    if not metar or not isinstance(metar, str):
        return 0
    try:
        if 'Q' in metar:
            try:
                idx = metar.find('Q')
                qnh = metar[idx+1:idx+5]  # pyre-ignore
                return int(qnh) if qnh.isdigit() else 0
            except:
                return 0
    except:
        return 0
    return 0

# API endpoints consolidated below (see get_history_api)


@app.route("/api/metar/<station_code>")
def api_metar_single(station_code):

    metar = get_metar(station_code.upper())
    if not metar:
        return jsonify({"error": "No METAR available"})
    
    parsed = parse_metar(metar)

    wind_direction = parsed.get("wind_dir")
    wind_speed = parsed.get("wind_speed_kt")

    if wind_direction == "VRB":
        wind_direction = "VRB"

    # format wind
    wind_text = None
    if parsed.get("wind_dir") and parsed.get("wind_speed_kt"):
        if parsed.get("wind_gust_kt"):
            wind_text = f"{parsed['wind_dir']}°/{parsed['wind_speed_kt']}G{parsed['wind_gust_kt']}KT"
        else:
            wind_text = f"{parsed['wind_dir']}°/{parsed['wind_speed_kt']}KT"

    return jsonify({
        "station": parsed.get("station"),
        "raw": metar,
        "wind": parsed.get("wind"),
        "wind_direction": parsed.get("wind_dir"),
        "wind_speed": parsed.get("wind_speed_kt"),
        "wind_gust": parsed.get("wind_gust_kt"),
        "visibility": format_visibility(parsed.get("visibility_m")),
        "weather": parsed.get("weather") or "NIL",
        "cloud": parsed.get("cloud") or "NIL",
        "qnh": parsed.get("pressure_hpa") or "NIL",
        "temp": parsed.get("temperature_c"),
        "dewpoint": parsed.get("dewpoint_c"),
        "visibility_m": parsed.get("visibility_m"),
        "status": parsed.get("status", "normal"),
        "report_type": parsed.get("report_type", "METAR")   # 🔥 UPDATED
    })

# =========================
# API GET NARRATIVE
# =========================
@app.route("/api/narrative/<station_code>")
def api_narrative(station_code):
    """API endpoint to get narrative text for a station"""
    metar = get_metar(station_code.upper())
    if not metar:
        return jsonify({"error": "No METAR available", "narrative": ""})
    
    parsed = parse_metar(metar)
    narrative = generate_metar_narrative(parsed, metar)
    
    return jsonify({
        "raw": metar,
        "narrative": narrative
    })

# =========================
# API CROSSWIND CALCULATOR
# =========================
@app.route("/api/crosswind")
def api_crosswind():
    """Calculate crosswind components"""
    wind_dir = request.args.get('wind_dir', type=int)
    wind_speed = request.args.get('wind_speed', type=float)
    runway_heading = request.args.get('runway_heading', type=int)
    
    if wind_dir is None or wind_speed is None or runway_heading is None:
        return jsonify({"error": "Missing parameters"}), 400
    
    angle_rad = math.radians(wind_dir - runway_heading)
    headwind = round(wind_speed * math.cos(angle_rad), 1)
    crosswind = round(abs(wind_speed * math.sin(angle_rad)), 1)
    tailwind = round(abs(headwind), 1) if headwind < 0 else 0
    headwind_val = headwind if headwind > 0 else 0
    
    return jsonify({
        "headwind": headwind_val,
        "crosswind": crosswind,
        "tailwind": tailwind,
        "wind_dir": wind_dir,
        "wind_speed": wind_speed,
        "runway_heading": runway_heading
    })

# =========================
# API WIND ROSE - Historical Wind Data
# =========================
# =========================
# API WIND ROSE - Dual Time Range Filter
# =========================
@app.route("/api/windrose/<station>")
def windrose_api(station):
    """API endpoint untuk Wind Rose 24 jam terakhir - FETCH FROM SHEETS for Real-time Sync"""
    global CSV_FILE
    
    # 00.00 WIB Kemarin sampai 00.00 WIB Hari Ini (Yesterday's Full Day)
    now_utc = datetime.utcnow()
    # Manual WIB offset (UTC+7)
    now_wib = now_utc + timedelta(hours=7)
    # Start of today WIB
    start_today_wib = now_wib.replace(hour=0, minute=0, second=0, microsecond=0)
    # Start of yesterday WIB
    start_yesterday_wib = start_today_wib - timedelta(days=1)
    
    # Convert ranges back to UTC for filtering
    cutoff_time = start_yesterday_wib - timedelta(hours=7)
    end_cutoff_time = start_today_wib - timedelta(hours=7)
    
    print(f"[WINDROSE 24H] {station}: Yesterday's range UTC {cutoff_time} to {end_cutoff_time}", file=sys.stderr)
    
    filtered_data = []
    # 🔥 FETCH DIRECTLY FROM GOOGLE SHEETS for consistent sync
    try:
        all_records = sheets_handler.get_all_data()
        if all_records:
            df = pd.DataFrame(all_records)
            df["time"] = pd.to_datetime(df["time"], errors='coerce')
            print(f"[WINDROSE 24H] Total records from Sheets: {len(df)}", file=sys.stderr)
            
            # Filter untuk station dan rentang hari ini (WIB 00.00 - 00.00)
            station_df = df[
                (df["station"].str.strip().str.upper() == station.upper()) &
                (df["time"] >= cutoff_time) &
                (df["time"] < end_cutoff_time)
            ]
            
            print(f"[WINDROSE 24H] Found {len(station_df)} rows for yesterday's range", file=sys.stderr)
            
            # If yesterday has no data, try today's range as fallback
            if len(station_df) == 0:
                print(f"[WINDROSE 24H] No data for yesterday, trying today's range...", file=sys.stderr)
                # Today: start_today_wib (UTC) to now
                today_cutoff = start_today_wib - timedelta(hours=7)
                station_df = df[
                    (df["station"].str.strip().str.upper() == station.upper()) &
                    (df["time"] >= today_cutoff) &
                    (df["time"] <= now_utc)
                ]
                if len(station_df) > 0:
                    # Update range labels to reflect today
                    start_yesterday_wib = start_today_wib  # relabel
                    start_today_wib = now_wib  # relabel
                    print(f"[WINDROSE 24H] Found {len(station_df)} rows for today's range (fallback)", file=sys.stderr)
            
            for _, row in station_df.iterrows():
                metar = str(row["metar"]) if pd.notna(row["metar"]) else ""
                if not metar:
                    continue
                
                # Extract wind data using regex (standardize with monthly API)
                wind_match = re.search(r'\b(\d{3}|VRB)(\d{2,3})(G\d{2,3})?KT\b', metar)
                if wind_match:
                    try:
                        wind_dir = wind_match.group(1)
                        if wind_dir != "VRB":
                            wib_time = row["time"] + timedelta(hours=7)
                            filtered_data.append({
                                "time": wib_time.strftime("%Y-%m-%d %H:%M:%S"),
                                "utc_time": f"{row['time'].strftime('%Y-%m-%d %H:%M UTC')} | {wib_time.strftime('%Y-%m-%d %H:%M WIB')}",
                                "station": station,
                                "dir": int(wind_dir),
                                "speed": float(wind_match.group(2))
                            })
                    except:
                        continue
        else:
            print(f"[WINDROSE 24H] No records returned from Sheets", file=sys.stderr)
    except Exception as e:
        print(f"[WINDROSE 24H] Sheets Error: {e}", file=sys.stderr)
        # Fallback to local CSV if Sheets fails
        if os.path.exists(CSV_FILE):
             try:
                df_local = pd.read_csv(CSV_FILE)
                df_local["time"] = pd.to_datetime(df_local["time"], errors='coerce')
                local_filtered = df_local[
                    (df_local["station"].str.strip().str.upper() == station.upper()) &
                    (df_local["time"] >= cutoff_time) &
                    (df_local["time"] < end_cutoff_time)
                ]
                for _, row in local_filtered.iterrows():
                    metar = str(row["metar"])
                    wind_match = re.search(r'\b(\d{3}|VRB)(\d{2,3})(G\d{2,3})?KT\b', metar)
                    if wind_match and wind_match.group(1) != "VRB":
                        wib_time = row["time"] + timedelta(hours=7)
                        filtered_data.append({
                            "time": wib_time.strftime("%Y-%m-%d %H:%M:%S"),
                            "utc_time": f"{row['time'].strftime('%Y-%m-%d %H:%M UTC')} | {wib_time.strftime('%Y-%m-%d %H:%M WIB')}",
                            "station": station,
                            "dir": int(wind_match.group(1)),
                            "speed": float(wind_match.group(2))
                        })
             except: pass

    # Determine source (logic matches implementation above)
    source_info = "Sheets" if IS_VERCEL else "Local CSV"
    
    # Use fixed WIB boundaries for the range labels
    start_range = start_yesterday_wib.strftime("%Y-%m-%d %H:%M")
    end_range = start_today_wib.strftime("%Y-%m-%d %H:%M")

    print(f"[WINDROSE 24H] Returning {len(filtered_data)} wind data points", file=sys.stderr)

    return jsonify({
        "period": "24h",
        "data": filtered_data,
        "count": len(filtered_data),
        "range": {
            "start": start_range,
            "end": end_range
        },
        "source": source_info
    })

@app.route("/api/windrose-monthly/<station>")
def windrose_monthly_api(station):
    """API endpoint untuk Wind Rose 1 bulan penuh (bulan sebelumnya) - FETCH FROM SHEETS"""
    now = datetime.utcnow()
    
    # Hitung bulan sebelumnya
    if now.month == 1:
        target_year = now.year - 1
        target_month = 12  # Desember
    else:
        target_year = now.year
        target_month = now.month - 1
    
    # Buat rentang waktu: 1 hari target_month sampai akhir target_month
    start_date = datetime(target_year, target_month, 1)
    # Hitung akhir bulan (hari pertama bulan berikutnya dikurangi 1 detik)
    if target_month == 12:
        end_date = datetime(target_year + 1, 1, 1) - timedelta(seconds=1)
    else:
        end_date = datetime(target_year, target_month + 1, 1) - timedelta(seconds=1)
    
    print(f"[WINDROSE MONTHLY] {station}: Fetching from Google Sheets for {target_year}-{target_month:02d}", file=sys.stderr)
    
    monthly_data = []
    used_current_month = False
    # 🔥 FETCH DIRECTLY FROM GOOGLE SHEETS AS REQUESTED
    try:
        all_records = sheets_handler.get_all_data()
        if all_records:
            df = pd.DataFrame(all_records)
            df["time"] = pd.to_datetime(df["time"], errors='coerce')
            print(f"[WINDROSE MONTHLY] Total records from Sheets: {len(df)}", file=sys.stderr)
            
            # Filter untuk station dan rentang bulan target
            station_df = df[
                (df["station"].str.strip().str.upper() == station.upper()) &
                (df["time"] >= start_date) &
                (df["time"] <= end_date)
            ]
            
            print(f"[WINDROSE MONTHLY] Found {len(station_df)} rows for {target_year}-{target_month:02d}", file=sys.stderr)
            
            # Fallback: if previous month has NO data, try CURRENT month
            if len(station_df) == 0:
                print(f"[WINDROSE MONTHLY] No data for previous month, trying current month...", file=sys.stderr)
                current_start = datetime(now.year, now.month, 1)
                station_df = df[
                    (df["station"].str.strip().str.upper() == station.upper()) &
                    (df["time"] >= current_start) &
                    (df["time"] <= now)
                ]
                if len(station_df) > 0:
                    used_current_month = True
                    target_month = now.month
                    target_year = now.year
                    start_date = current_start
                    end_date = now
                    print(f"[WINDROSE MONTHLY] Found {len(station_df)} rows for current month (fallback)", file=sys.stderr)
            
            for _, row in station_df.iterrows():
                metar = str(row["metar"]) if pd.notna(row["metar"]) else ""
                if not metar:
                    continue
                
                # Extract wind data using regex
                wind_match = re.search(r'\b(\d{3}|VRB)(\d{2,3})(G\d{2,3})?KT\b', metar)
                if wind_match:
                    try:
                        wind_dir = wind_match.group(1)
                        if wind_dir != "VRB":
                            wib_time = row["time"] + timedelta(hours=7)
                            monthly_data.append({
                                "time": row["time"].strftime("%Y-%m-%d %H:%M:%S"),
                                "utc_time": f"{row['time'].strftime('%Y-%m-%d %H:%M UTC')} | {wib_time.strftime('%Y-%m-%d %H:%M WIB')}",
                                "station": station,
                                "dir": int(wind_dir),
                                "speed": float(wind_match.group(2))
                            })
                    except:
                        continue
        else:
            print(f"[WINDROSE MONTHLY] No records returned from Sheets", file=sys.stderr)
    except Exception as e:
        print(f"[WINDROSE MONTHLY] Sheets Error: {e}", file=sys.stderr)
    
    # Format nama bulan untuk display
    month_names = {
        1: "Januari", 2: "Februari", 3: "Maret", 4: "April",
        5: "Mei", 6: "Juni", 7: "Juli", 8: "Agustus",
        9: "September", 10: "Oktober", 11: "November", 12: "Desember"
    }
    month_name = month_names.get(target_month, str(target_month))
    
    print(f"[WINDROSE MONTHLY] Returning {len(monthly_data)} wind data points for {month_name} {target_year}", file=sys.stderr)
    
    return jsonify({
        "period": "monthly" if not used_current_month else "current_month",
        "month": target_month,
        "year": target_year,
        "month_name": month_name,
        "data": monthly_data,
        "count": len(monthly_data),
        "range": {
            "start": start_date.strftime("%Y-%m-%d"),
            "end": end_date.strftime("%Y-%m-%d") if isinstance(end_date, datetime) else str(end_date)
        }
    })

# =========================
# API HISTORY - Technical History for Charts
# =========================
@app.route("/api/latest")
@app.route("/api/history")
@app.route("/api/metar/history")
def get_history_api():
    """Returns historical data in JSON format for charts and tables"""
    global last_metar_update, auto_fetch
    
    # 🔥 VERCEL STALE CHECK:
    # Ensure history is relatively fresh from Sheets if on Vercel
    if IS_VERCEL and auto_fetch:
        now = datetime.utcnow()
        should_sync = False
        if not last_metar_update:
            should_sync = True
        else:
            try:
                last_dt = pd.to_datetime(last_metar_update.replace("Z", ""), format='mixed')
                if (now - last_dt).total_seconds() > 60: # History check can be slightly more relaxed
                    should_sync = True
            except:
                should_sync = True
        
        if should_sync:
            print("[HISTORY] Local cache stale, syncing from Sheets...", file=sys.stderr)
            sheets_handler.sync_to_local(CSV_FILE)
            last_metar_update = datetime.utcnow().isoformat() + "Z"

    if not os.path.exists(CSV_FILE):
        # Warmup: if no history, try to fetch current METAR to initialize
        station = "WARR"
        metar = get_metar(station)
        if metar:
            df = pd.DataFrame([{"station": station, "time": datetime.utcnow(), "metar": metar}])
            df.to_csv(CSV_FILE, index=False)
        else:
            return jsonify({"data": [], "labels": [], "temps": [], "pressures": []})

    try:
        df = fetch_history_from_source()
        if not df.empty:
            df = df.tail(30)
        df["metar"] = df.get("metar", pd.Series(dtype='str')).fillna("").astype(str)
        
        # Format for history table (newest first)
        data_list = []
        for _, row in df.iloc[::-1].iterrows():
            parsed = parse_metar(str(row["metar"]))
            # Parse time for better display
            dt = pd.to_datetime(row["time"])
            data_list.append({
                "day_name": dt.strftime('%A'),
                "time": dt.strftime('%H:%M'),
                "full_time": pd.to_datetime(row["time"]).strftime("%Y-%m-%d %H:%M UTC"),
                "station": row["station"],
                "metar": row["metar"],
                "temp": extract_temp(str(row["metar"])),
                "pressure": extract_pressure(str(row["metar"])),
                "wind": parsed.get("wind_speed_kt"),
                "gust": parsed.get("wind_gust_kt")
            })

        # Format for charts (oldest to newest)
        labels = [pd.to_datetime(t).strftime("%d/%m/%y %H:%M UTC") for t in df["time"]]
        temps = [extract_temp(m) for m in df["metar"]]
        pressures = [extract_pressure(m) for m in df["metar"]]
        
        # Calculate range and source
        start_time = pd.to_datetime(df["time"].iloc[0]).strftime("%Y-%m-%d %H:%M UTC") if not df.empty else ""
        end_time = pd.to_datetime(df["time"].iloc[-1]).strftime("%Y-%m-%d %H:%M UTC") if not df.empty else ""
        
        # In Vercel environment, data is synced from Sheets
        source_info = "Sheets" if IS_VERCEL else "Local CSV"

        return jsonify({
            "data": data_list,
            "labels": labels,
            "temps": temps,
            "pressures": pressures,
            "range": {
                "start": start_time,
                "end": end_time
            },
            "count": len(df),
            "source": source_info
        })
    except Exception as e:
        print(f"[API] History error: {e}", file=sys.stderr)
        return jsonify({"error": str(e), "data": []}), 500

@app.route("/api/metar/<station>")
def get_single_metar_api(station):
    """Returns the latest single METAR data for a station"""
    metar = get_metar(station)
    if not metar and os.path.exists(CSV_FILE):
        # Fallback to last known from CSV
        df = pd.read_csv(CSV_FILE)
        station_df = df[df["station"] == station]
        if not station_df.empty:
            metar = station_df.iloc[-1]["metar"]
    
    if metar:
        parsed = parse_metar(metar)
        return jsonify({
            "raw": metar,
            "station": station,
            "wind_direction": parsed.get("wind_dir"),
            "wind_speed": parsed.get("wind_speed_kt"),
            "visibility_m": parsed.get("visibility_m"),
            "status": parsed.get("status", "normal"),
            "report_type": parsed.get("report_type", "METAR")
        })
    return jsonify({"error": "No data available"}), 404

# =========================
# DATA SOURCE HELPER
# =========================
def fetch_history_from_source():
    """
    Unified fetcher for METAR history. 
    Prioritizes Google Sheets, fallbacks to local CSV if Sheets fails.
    """
    try:
        # 1. Try Google Sheets first (Preferred for Vercel/Cloud)
        print("[DATA] Fetching history from Google Sheets...", file=sys.stderr)
        all_data = sheets_handler.get_all_data()
        if all_data:
            df = pd.DataFrame(all_data)
            if not df.empty and "time" in df.columns:
                print(f"[DATA] Successfully fetched {len(df)} records from Sheets", file=sys.stderr)
                return df
                
        # 2. Fallback to local CSV
        if os.path.exists(CSV_FILE):
            print("[DATA] Falling back to local CSV...", file=sys.stderr)
            return pd.read_csv(CSV_FILE)
            
    except Exception as e:
        print(f"[DATA] ❌ Error fetching history: {e}", file=sys.stderr)
        
    return pd.DataFrame(columns=["station", "time", "metar"])

# =========================
# HOME ROUTE
# =========================
@app.route("/", methods=["GET", "POST"])
def home():
    global last_metar_update, auto_fetch
    station = "WARR"
    metar = None
    parsed = {}
    qam = None
    narrative = None
    latest = None
    latest_wind_obj = {}
    safe_recent_winds = []
    temps = []
    pressures = []
    has_history = False

    try:
        print("\n=== HOME ROUTE CALLED ===", file=sys.stderr)
        
        if request.method == "POST":
            station = request.form["icao"].upper()
            print(f"[HOME] POST request with station: {station}", file=sys.stderr)
            fetch_needed = True
        else:
            fetch_needed = auto_fetch # Only fetch on GET if auto_fetch is enabled

        if fetch_needed:
            print(f"[HOME] Update triggered for {station}...", file=sys.stderr)
            success = update_metar_data_and_sync(station)
            if success:
                print("[HOME] Live sync successful", file=sys.stderr)
            else:
                print("[HOME] Live sync failed or no new data", file=sys.stderr)
        
        # Pull global data for display
        metar = str(latest_metar_data.get("raw") or "")
        if metar:
            parsed = parse_metar(metar)
            qam = latest_metar_data.get("qam")
            narrative = latest_metar_data.get("narrative")
            
            # Additional wind storage (robustness)
            store_wind(parsed, station)
            print("[HOME] Display data prepared from cache", file=sys.stderr)
        else:
            print("[HOME] No live data in cache, checking CSV history...", file=sys.stderr)
            # Try to get last known METAR from CSV if live fetch fails
            if os.path.exists(CSV_FILE):
                df = pd.read_csv(CSV_FILE)
                if len(df) > 0:
                    # Get the most recent METAR
                    last_row = df.iloc[-1]
                    metar = last_row['metar']
                    station = last_row['station']
                    parsed = parse_metar(metar)
                    qam = generate_qam(station, parsed, metar)
                    narrative = generate_metar_narrative(parsed, metar)
                    
                    # Fallback successful
                    metar_display = str(metar)
                    # pyre-ignore[ID bbb2bbe8-27af-4647-8926-038ce0fe7de6, ID 474d23c6-9e67-4cb1-ba78-43551e199e15]
                    metar_display = metar_display[:50]
                    print(f"[HOME] Using historical METAR: {metar_display}...", file=sys.stderr)
                    try:
                        last_metar_update = pd.to_datetime(last_row["time"], format='mixed').isoformat() + "Z"
                    except:
                        last_metar_update = datetime.utcnow().isoformat() + "Z"
    except Exception as e:
        print(f"[HOME] CRITICAL ERROR: {e}", file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)
        # Fallback will happen as metrics are pre-initialized to None

    # Read history and prepare chart data
    history_start = ""
    history_end = ""
    history_count = 0
    history_source = "Sheets" if IS_VERCEL else "Local CSV"

    full_history = fetch_history_from_source()
    if not full_history.empty:
        # Calculate TODAY in UTC
        now_utc = datetime.utcnow()
        today_str = now_utc.strftime("%Y-%m-%d")
        current_day = now_utc.strftime("%A")
        
        # Filter for today's records (WIB date)
        # Ensure time column is string for startswith comparison
        full_history['time'] = full_history['time'].astype(str)
        history = full_history[full_history['time'].str.contains(today_str)].copy()
        
        # Update history source if data actually came from Sheets
        # (This is a bit redundant but helps UI accuracy)
        if len(full_history) > 0 and history_source == "Local CSV":
             # We can't easily know if it came from Sheets unless we check sheets_handler status
             pass
        
        # Add day name for the table
        if not history.empty:
            history['day_name'] = pd.to_datetime(history['time']).dt.strftime('%A')
            history['time_short'] = pd.to_datetime(history['time']).dt.strftime('%H:%M')
            
            # Convert metar to string and fillna first to avoid errors
            history["metar"] = history["metar"].fillna("").astype(str)
            
            # Extract minute for METAR/SPECI status detection (0 or 30 = normal, else = SPECI)
            # Use regex to extract from METAR time group (DDHHMMZ) if available, fallback to system time
            def get_metar_minute(row):
                metar = str(row['metar'])
                match = re.search(r'\b\d{6}Z\b', metar)
                if match:
                    try:
                        return int(match.group(0)[4:6])
                    except: pass
                # Fallback to system timestamp
                try:
                    return pd.to_datetime(row['time']).minute
                except:
                    return -1
            
            history['status_minute'] = history.apply(get_metar_minute, axis=1)
        
        has_history = not history.empty
        if has_history:
            labels = [pd.to_datetime(t).strftime("%d/%m/%y %H:%M") for t in history['time'].tolist()]
            temps = [extract_temp(m) for m in history['metar'].tolist()]
            pressures = [extract_pressure(m) for m in history['metar'].tolist()]
            
            # Extract winds and gusts for trend charts
            winds = []
            gusts = []
            for m in history['metar'].tolist():
                wind_match = re.search(r'(\d{3}|VRB)(\d{2,3})(G(\d{2,3}))?KT', m)
                if wind_match:
                    winds.append(int(wind_match.group(2)))
                    gusts.append(int(wind_match.group(4)) if wind_match.group(4) else None)
                else:
                    winds.append(None)
                    gusts.append(None)
            
            history_start = pd.to_datetime(history['time'].iloc[0]).strftime("%Y-%m-%d %H:%M")
            history_end = pd.to_datetime(history['time'].iloc[-1]).strftime("%Y-%m-%d %H:%M")
            history_count = len(history)
        else:
            labels = []
            winds = []
            gusts = []
    else:
        history = pd.DataFrame(columns=["station", "time", "metar"])
        labels = []
        winds = []
        gusts = []
    
    # Convert recent winds for Wind Compass
    safe_recent_winds = []
    for w in wind_history:
        if w.get("station") == station:
            safe_recent_winds.append({
                "dir": w.get("dir"),
                "speed": w.get("speed"),
                "timestamp": w.get("time")
            })

    # Extract latest wind specifically
    latest_wind_obj = {}
    if parsed and parsed.get('wind_dir') is not None and parsed.get('wind_speed_kt') is not None:
        latest_wind_obj = {
            "dir": parsed['wind_dir'],
            "speed": parsed['wind_speed_kt']
        }

    # Create latest dict for the METAR display with status color
    latest = None
    if metar and parsed:
        latest = {
            "station": station,
            "metar": metar,
            "status": parsed.get("status", "normal"),
            "report_type": detect_metar_report_type(metar)
        }

    last_saved = history["time"].iloc[-1] if has_history else "N/A"
    print(f"[HOME] Rendering template with QAM: {qam is not None}")

    # Pre-format last_metar_update for the template (WIB)
    last_update_display = "--:-- WIB"
    if last_metar_update:
        try:
            # last_metar_update is ISO string (UTC)
            dt = pd.to_datetime(last_metar_update)
            # Manual WIB offset (UTC+7)
            wib_dt = dt + timedelta(hours=7)
            last_update_display = wib_dt.strftime("%H:%M") + " WIB"
        except:
            pass

    return render_template(
        "index.html",
        station=station,
        latest=latest,
        qam=qam,
        narrative=narrative,
        history=history,
        current_day=current_day if 'current_day' in locals() else "",
        last_saved=last_saved,
        temps=temps,
        pressures=pressures,
        winds=winds,
        gusts=gusts,
        labels=labels,
        has_history=has_history,
        auto_fetch=auto_fetch,
        last_metar_update=last_update_display,
        history_start=history_start,
        history_end=history_end,
        history_count=history_count,
        history_source=history_source,
        recent_winds=json.dumps(safe_recent_winds),
        latest_wind=json.dumps(latest_wind_obj)
    )

def common_view_context(template_name):
    """Helper to load common metrics for the new specialized pages"""
    global last_metar_update, auto_fetch
    station = "WARR"
    metar = None
    parsed = {}
    qam = None
    narrative = None
    latest = None
    latest_wind_obj = {}
    # Fetch latest data from cache
    metar = str(latest_metar_data.get("raw") or "")
    if metar:
        parsed = parse_metar(metar)
        qam = latest_metar_data.get("qam")
        narrative = latest_metar_data.get("narrative")
    
    # Read history and prepare chart data
    history_start = ""
    history_end = ""
    history_count = 0
    history_source = "Sheets" if IS_VERCEL else "Local CSV"
    
    labels = []
    temps = []
    pressures = []
    winds = []
    gusts = []
    has_history = False
    
    full_history = fetch_history_from_source()
    if not full_history.empty:
        # Calculate TODAY in UTC
        now_utc = datetime.utcnow()
        today_str = now_utc.strftime("%Y-%m-%d")
        current_day = now_utc.strftime("%A")
        
        # Filter for today's records (WIB date)
        full_history['time'] = full_history['time'].astype(str)
        history = full_history[full_history['time'].str.contains(today_str)].copy()
        
        # Add day name and formatted time for the table
        if not history.empty:
            history['day_name'] = pd.to_datetime(history['time']).dt.strftime('%A')
            history['time_short'] = pd.to_datetime(history['time']).dt.strftime('%H:%M')
            
        history["metar"] = history["metar"].fillna("").astype(str)
        has_history = not history.empty
        if has_history:
            for _, row in history.iterrows():
                m = str(row["metar"])
                labels.append(pd.to_datetime(row["time"]).strftime("%d/%m/%y %H:%M"))
                temps.append(extract_temp(m))
                pressures.append(extract_pressure(m))
                
                # Extract wind speed and gust for charts
                wind_match = re.search(r'(\d{3}|VRB)(\d{2,3})(G(\d{2,3}))?KT', m)
                if wind_match:
                    winds.append(int(wind_match.group(2)))
                    gusts.append(int(wind_match.group(4)) if wind_match.group(4) else None)
                else:
                    winds.append(None)
                    gusts.append(None)
            
            history_start = pd.to_datetime(history['time'].iloc[0]).strftime("%Y-%m-%d %H:%M")
            history_end = pd.to_datetime(history['time'].iloc[-1]).strftime("%Y-%m-%d %H:%M")
            history_count = len(history)

    # Convert recent winds strictly to a list of dicts for JSON serialization
    safe_recent_winds = []
    # wind_history is a global deque of dictionaries
    for w in wind_history:
        if w.get("station") == station:
            safe_recent_winds.append({
                "dir": w.get("dir"),
                "speed": w.get("speed"),
                "timestamp": w.get("time")
            })
            
    # Extract latest wind specifically
    latest_wind_obj = {}
    if parsed and parsed.get('wind_dir') is not None and parsed.get('wind_speed_kt') is not None:
        latest_wind_obj = {
            "dir": parsed['wind_dir'],
            "speed": parsed['wind_speed_kt']
        }

    return render_template(
        template_name,
        station=station,
        latest={"station": station, "metar": metar, "status": parsed.get("status", "normal"), "report_type": detect_metar_report_type(metar)} if parsed else None,
        qam=qam,
        narrative=narrative,
        history=history,
        current_day=current_day if 'current_day' in locals() else "",
        has_history=has_history,
        temps=temps,
        pressures=pressures,
        winds=winds,
        gusts=gusts,
        labels=labels,
        history_start=history_start,
        history_end=history_end,
        history_count=history_count,
        history_source=history_source,
        recent_winds=json.dumps(safe_recent_winds),
        latest_wind=json.dumps(latest_wind_obj)
    )

@app.route("/charts")
def charts_view():
    return common_view_context("charts.html")

@app.route("/wind_analysis")
def wind_analysis_view():
    return common_view_context("wind_analysis.html")

@app.route("/operational_tools")
def operational_tools_view():
    return common_view_context("operational_tools.html")

@app.route("/weather_analysis")
def weather_analysis_view():
    return common_view_context("weather_analysis.html")

# =========================
# DOWNLOAD QAM
# =========================
@app.route("/download_qam")
def download_qam():
    station = request.args.get("station")
    qam = request.args.get("qam")
    if not qam:
        return "Tidak ada QAM untuk di-download", 400

    buffer = BytesIO()
    buffer.write(qam.encode())
    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"QAM_{station}.txt",
        mimetype="text/plain"
    )

# =========================
# DOWNLOAD CSV HISTORY
# =========================
@app.route("/download_csv")
def download_csv():
    if not os.path.exists(CSV_FILE):
        return "CSV belum tersedia", 400

    buffer = BytesIO()
    df = pd.read_csv(CSV_FILE)
    df.to_csv(buffer, index=False)
    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name="metar_history.csv",
        mimetype="text/csv"
    )

# =========================
# HISTORY BY DATE RANGE
# =========================
@app.route("/history_by_date", methods=["GET", "POST"])
def history_by_date():

    results = None
    station = "WARR"  # Default station
    labels: list = []
    temps: list = []
    pressures: list = []
    winds: list = []
    gusts: list = []
    thunder_flags: list = []
    wind_dirs: list = []
    start_date = ""
    end_date = ""
    utc_wib_labels: list = []

    if request.method == "POST":
        station = request.form.get("icao", "WARR").upper()
        start_date = request.form.get("start_date", "")
        end_date = request.form.get("end_date", "")
        
        print(f"[HISTORY] Station: {station}, Start: {start_date}, End: {end_date}")

        if start_date and end_date:
            # 🔥 SYNC: Ensure latest data is pulled from Google Sheets before search
            # This is critical on Vercel where /tmp/ storage might be stale
            try:
                print(f"[HISTORY] Syncing latest data from Sheets for {station}...", file=sys.stderr)
                sheets_handler.sync_to_local(CSV_FILE)
            except Exception as sync_err:
                print(f"[HISTORY] Sync warning (proceeding with local data): {sync_err}", file=sys.stderr)

            # Read CSV
            if os.path.exists(CSV_FILE):
                try:
                    df = pd.read_csv(CSV_FILE)
                    print(f"[HISTORY] Loaded {len(df)} rows from CSV", file=sys.stderr)
                    
                    # 1. Cleaning: Drop completely empty rows and handle NaT
                    df = df.dropna(subset=["time", "metar"])
                    df["time"] = pd.to_datetime(df["time"], errors='coerce', format='mixed')
                    df = df.dropna(subset=["time"])
                    
                    # 2. De-duplication: Ensure charts don't show redundant points
                    # Sort by time first to keep the most recent entries if duplicates exist
                    df = df.sort_values("time")
                    df = df.drop_duplicates(subset=["station", "time", "metar"], keep="last")
                    
                    # 3. Timezone Correction (WIB -> UTC)
                    # User inputs WIB (local), DB stores UTC. Shift back 7 hours.
                    start_dt = pd.to_datetime(start_date)
                    end_dt = pd.to_datetime(end_date)
                    
                    # If end date is exactly at midnight (T00:00), the user likely picked a date 
                    # without a time, implying they want the WHOLE day up to 23:59 WIB.
                    if "T00:00" in end_date:
                        end_dt = end_dt.replace(hour=23, minute=59, second=59)
                    
                    # Convert WIB to UTC (-7 hours)
                    start_utc = start_dt - timedelta(hours=7)
                    end_utc = end_dt - timedelta(hours=7)
                    
                    print(f"[HISTORY] Filter (UTC Range): {start_utc} to {end_utc}", file=sys.stderr)
                    
                    # 4. Apply Filter
                    station_clean = station.strip().upper()
                    results = df[
                        (df["station"].str.strip().str.upper() == station_clean) &
                        (df["time"] >= start_utc) &
                        (df["time"] <= end_utc)
                    ]
                    
                    print(f"[HISTORY] Found {len(results)} matching records", file=sys.stderr)
                    
                    # Extract chart data if results exist
                    if results is not None and not results.empty:
                        results = results.sort_values("time")  # Sort by time for chart
                        
                        for _, row in results.iterrows():
                            metar = str(row["metar"]) if pd.notna(row["metar"]) else ""
                            
                            # Format time for label
                            labels.append(pd.to_datetime(row["time"]).strftime("%d/%m/%y %H:%M UTC"))
                            
                            # Extract temperature (format: XX/XX)
                            temp_match = re.search(r'(\d{2})/(\d{2})', metar)
                            temps.append(int(temp_match.group(1)) if temp_match else None)
                            
                            # Extract pressure (QNH format: QXXXX)
                            qnh_match = re.search(r'Q(\d{4})', metar)
                            pressures.append(int(qnh_match.group(1)) if qnh_match else None)
                            
                            # Extract wind speed, gust, and direction
                            wind_match = re.search(r'(\d{3}|VRB)(\d{2,3})(G(\d{2,3}))?KT', metar)
                            
                            if wind_match:
                                wind_dir = wind_match.group(1)
                                wind_dirs.append(wind_dir if wind_dir != "VRB" else None)
                                winds.append(int(wind_match.group(2)))
                                gusts.append(int(wind_match.group(4)) if wind_match.group(4) else None)
                            else:
                                wind_dirs.append(None)
                                winds.append(None)
                                gusts.append(None)
                            
                            # Detect thunderstorm
                            thunder_codes = ["TS", "TSRA", "VCTS", "+TS", "TSGR", "-TS", "+TSRA", "-TSRA"]
                            thunder_flags.append(any(code in metar for code in thunder_codes))
                            
                            # Add full dual time label for Wind Rose
                            wib_time_row = row["time"] + timedelta(hours=7)
                            utc_wib_labels.append(f"{row['time'].strftime('%Y-%m-%d %H:%M UTC')} | {wib_time_row.strftime('%Y-%m-%d %H:%M WIB')}")
                        
                        print(f"[HISTORY] Chart data extracted: {len(labels)} points")
                        
                        # Reverse results for table display (newest first)
                        results = results.iloc[::-1]
                except Exception as e:
                    print(f"[HISTORY] Error processing CSV: {e}", file=sys.stderr)
                    results = None
            else:
                print("[HISTORY] CSV file does not exist")

    return render_template(
        "history_by_date.html",
        results=results,
        station=station,
        labels=labels,
        temps=temps,
        pressures=pressures,
        winds=winds,
        gusts=gusts,
        thunder_flags=thunder_flags,
        wind_dirs=wind_dirs,
        start_date=start_date,
        end_date=end_date,
        utc_wib_labels=utc_wib_labels,
        auto_fetch=auto_fetch,
        last_metar_update=last_metar_update
    )


# ============ SYSTEM CONTROL ENDPOINTS ============

@app.route("/ping")
def ping():
    """Keep-alive endpoint untuk frontend"""
    return jsonify({"status": "alive", "timestamp": datetime.utcnow().isoformat()})

@app.route("/health")
def health():
    """Health check untuk monitoring eksternal"""
    global last_metar_update
    
    # Fallback: if last_metar_update is None, try to get from CSV
    if last_metar_update is None and os.path.exists(CSV_FILE):
        try:
            df = pd.read_csv(CSV_FILE)
            if not df.empty:
                last_metar_update = pd.to_datetime(df.iloc[-1]["time"]).isoformat() + "Z"
        except:
            pass

    return jsonify({
        "status": "ok",
        "server": "online",
        "auto_fetch": auto_fetch,
        "last_update": last_metar_update
    })

@app.route("/api/toggle_fetch", methods=["POST"])
def toggle_fetch():
    """System Control ON/OFF"""
    global auto_fetch
    auto_fetch = not auto_fetch

    return jsonify({
        "auto_fetch": auto_fetch,
        "last_update": last_metar_update,
        "message": f"Auto fetch {'ENABLED' if auto_fetch else 'DISABLED'}"
    })

@app.route("/api/set_fetch", methods=["POST"])
def set_fetch():
    """Explicitly set system status from client"""
    global auto_fetch
    data = request.json
    if data and "enabled" in data:
        auto_fetch = bool(data["enabled"])
        print(f"[SYSTEM] Fetch status set to: {auto_fetch} (client sync)", file=sys.stderr)
    
    return jsonify({
        "auto_fetch": auto_fetch,
        "last_update": last_metar_update
    })

# =========================
@app.route('/favicon.ico')
def favicon():
    return '', 204

# =========================
# POLLING ENDPOINT (replaces WebSocket)
# =========================
@app.route("/api/latest-data")
def latest_data():
    """Endpoint for frontend polling — returns latest METAR + system status"""
    global last_metar_update, auto_fetch, latest_metar_data
    
    # 🔥 VERCEL SYNC TRIGGER:
    # If auto_fetch is on, check if we need to fetch fresh data
    if auto_fetch:
        now = datetime.utcnow()
        should_update = False
        
        if not last_metar_update:
            should_update = True
        else:
            try:
                last_dt = pd.to_datetime(last_metar_update.replace("Z", ""), format='mixed')
                # If more than 20 seconds old, trigger an update
                if (now - last_dt).total_seconds() > 20: 
                    should_update = True
            except:
                should_update = True
        
        if should_update:
            print(f"[POLL] Data stale or missing, triggering sync for WARR...", file=sys.stderr)
            update_metar_data_and_sync("WARR")

    # Ensure latest_metar_data is populated if available in CSV but missing in cache
    # (Happens on first load or after server restart in serverless environments)
    if not latest_metar_data and os.path.exists(CSV_FILE):
        try:
            df = pd.read_csv(CSV_FILE)
            if not df.empty:
                last_row = df.iloc[-1]
                metar = str(last_row["metar"])
                parsed = parse_metar(metar)
                station = str(last_row["station"])
                qam = generate_qam(station, parsed, metar)
                narrative = generate_metar_narrative(parsed, metar)
                last_metar_update = pd.to_datetime(last_row["time"]).isoformat() + "Z"
                
                latest_metar_data = {
                    "status": "cached",
                    "raw": metar,
                    "qam": qam,
                    "narrative": narrative,
                    "wind_dir": parsed.get("wind_dir"),
                    "wind_speed": parsed.get("wind_speed_kt"),
                    "last_update": last_metar_update, # Include timestamp for change detection
                    "auto_fetch": auto_fetch,
                    "wind_gust": parsed.get("wind_gust_kt"),
                    "temp": parsed.get("temperature_c"),
                    "dewpoint": parsed.get("dewpoint_c"),
                    "visibility_m": parsed.get("visibility_m"),
                    "cloud": parsed.get("cloud"),
                    "qnh": parsed.get("pressure_hpa"),
                    "weather": parsed.get("weather"),
                    "metar_status": parsed.get("status", "normal"),
                    "report_type": parsed.get("report_type", "METAR")
                }
        except Exception as e:
            print(f"[POLL] Error building fallback data: {e}", file=sys.stderr)

    # Return cached data with current system status
    data = {}
    if latest_metar_data:
        data = latest_metar_data.copy()
    
    data.update({
        "auto_fetch": auto_fetch,
        "last_update": last_metar_update,
        "server": "online"
    })
    # Return with Edge Cache headers for Vercel optimization
    response = make_response(jsonify(data))
    response.headers['Cache-Control'] = 's-maxage=20, stale-while-revalidate=40'
    return response

# =========================
# CRON JOB ENDPOINT (For 24/7 background sync)
# =========================
@app.route("/api/cron/sync")
def cron_sync():
    """
    Automated endpoint triggered by Vercel Cron or External Cron service.
    """
    # 1. Check for Vercel Native Cron header
    is_vercel_cron = request.headers.get('x-vercel-cron') == '1'
    
    # 2. Check for Token-based auth
    expected_token = os.environ.get('CRON_TOKEN', 'bmkg-juanda-secret-123')
    provided_token = request.args.get('auth')
    is_external_cron = provided_token == expected_token

    if not is_vercel_cron and not is_external_cron:
        print(f"[CRON] ❌ Unauthorized attempt from {request.remote_addr}. Token provided: {'YES' if provided_token else 'NO'}", file=sys.stderr)
        return jsonify({
            "error": "Unauthorized", 
            "hint": "Provide valid ?auth= token",
            "received_token": provided_token[:3] + "..." if provided_token else None
        }), 401

    auth_type = 'Vercel' if is_vercel_cron else 'External'
    print(f"[CRON] ⏰ Background sync triggered (Type: {auth_type})", file=sys.stderr)
    success = update_metar_data_and_sync("WARR")
    
    if success:
        return jsonify({"status": "success", "message": f"Sync complete via {auth_type}"}), 200
    else:
        return jsonify({"status": "failed", "message": "Sync failed or no new data"}), 500

# =========================
# HELPER: Normalize METAR for accurate comparison
# =========================
def normalize_metar(metar: str) -> str:
    """
    Normalisasi string METAR untuk comparison yang akurat.
    Menghilangkan perbedaan formatting yang tidak signifikan.
    """
    if not metar:
        return ""
    # Remove trailing = (marker akhir METAR)
    metar = metar.replace("=", "")
    # Normalize whitespace (multiple spaces -> single space, strip ends)
    metar = " ".join(metar.split())
    # Uppercase untuk consistency
    metar = metar.upper().strip()
    return metar

# =========================
# CENTRALIZED METAR UPDATE DATA & SYNC (ANTI-DUPLIKASI)
# =========================
def update_metar_data_and_sync(station="WARR"):
    """
    Central function to fetch METAR, save locally, sync to Google Sheets, 
    and update global cache. 
    
    LOGIKA ANTI-DUPLIKASI:
    - Hanya menyimpan jika METAR benar-benar berbeda dari yang sudah ada
      dalam 24 jam terakhir, ATAU jika sudah lewat 30 menit dari update terakhir
      dengan data yang sama (untuk tracking continuity).
    """
    global last_metar_update, latest_metar_data, auto_fetch
    
    try:
        print(f"[SYNC] Starting update for {station}...", file=sys.stderr)
        metar = get_metar(station)
        
        if not metar:
            print("[SYNC] ❌ No METAR received", file=sys.stderr)
            return False
        
        # Normalisasi untuk comparison
        metar_clean = normalize_metar(metar)
        print(f"[SYNC] Raw METAR: {str(metar)[:80]}...", file=sys.stderr)  # type: ignore
        print(f"[SYNC] Normalized: {str(metar_clean)[:80]}...", file=sys.stderr)  # type: ignore

        # =====================================================
        # GLOBAL CONTEXT: Fetch recent history directly from Sheets
        # ensures deduplication works across isolated Vercel containers.
        # =====================================================
        print("[SYNC] Fetching global context from Google Sheets...", file=sys.stderr)
        recent_data = sheets_handler.get_recent_data(limit=10)
        
        if recent_data:
            df = pd.DataFrame(recent_data)
        else:
            # Fallback to local CSV if Sheets fetch fails or is empty
            if not os.path.exists(CSV_FILE):
                pd.DataFrame(columns=["station", "time", "metar"]).to_csv(CSV_FILE, index=False)
            df = pd.read_csv(CSV_FILE)
        
        # =====================================================
        # LOGIKA ANTI-DUPLIKASI: Strict String Match
        # =====================================================
        should_save = True
        skip_reason = ""
        
        if len(df) > 0:
            # Check only the MOST RECENT entry
            last_row = df.iloc[-1]
            last_metar_clean = normalize_metar(str(last_row["metar"]))
            
            if metar_clean == last_metar_clean:
                # METAR identik dengan data terakhir -> No need to save a new row
                should_save = False
                skip_reason = "Duplikat: METAR identik dengan data terakhir"
            else:
                # NEW DATA or SPECI (which has different timestamp) -> Save
                should_save = True
                skip_reason = ""
        
        # =====================================================
        # EXECUTE SAVE OR SKIP
        # =====================================================
        if not should_save:
            print(f"[SYNC] ⏭️ SKIP: {skip_reason}", file=sys.stderr)
            # Update timestamp and cache even if skip saving
            last_metar_update = datetime.utcnow().isoformat() + "Z"
            
            # 🔥 CRITICAL: Update global cache with freshly fetched data 
            # even if we didn't save a new row to the database.
            # This prevents "flip-flop" in serverless environments.
            parsed = parse_metar(metar)
            qam = generate_qam(station, parsed, metar)
            narrative = generate_metar_narrative(parsed, metar)
            
            latest_metar_data = {
                "status": "duplicate_skipped",
                "qam": qam,
                "raw": metar,
                "narrative": narrative,
                "time": datetime.utcnow().strftime("%d-%m-%Y %H:%M:%S"),
                "wind_dir": parsed.get("wind_dir"),
                "wind_speed": parsed.get("wind_speed_kt"),
                "wind_gust": parsed.get("wind_gust_kt"),
                "temp": parsed.get("temperature_c"),
                "dewpoint": parsed.get("dewpoint_c"),
                "visibility_m": parsed.get("visibility_m"),
                "cloud": parsed.get("cloud"),
                "qnh": parsed.get("pressure_hpa"),
                "weather": parsed.get("weather"),
                "metar_status": parsed.get("status", "normal"),
                "report_type": parsed.get("report_type", "METAR"),
                "auto_fetch": auto_fetch,
                "last_update": last_metar_update
            }
            return True  # Return True karena bukan error
        
        # =====================================================
        # SAVE NEW DATA
        # =====================================================
        print("[SYNC] ✨ NEW/REFRESHED METAR detected! Saving...", file=sys.stderr)
        
        new_row = {
            "station": station,
            "time": datetime.utcnow(),
            "metar": metar  # Simpan original, tidak normalized
        }
        
        # Format waktu tanpa milliseconds untuk consistency
        new_row_df = pd.DataFrame([new_row])
        new_row_df["time"] = pd.to_datetime(new_row_df["time"]).dt.strftime("%Y-%m-%d %H:%M:%S")
        
        df = pd.concat([df, new_row_df], ignore_index=True)
        df.to_csv(CSV_FILE, index=False)
        
        # 🔥 SYNC TO GOOGLE SHEETS
        try:
            print(f"[SYNC] Pushing to Google Sheets...", file=sys.stderr)
            sheets_handler.save_metar(station, new_row["time"], metar)
            print(f"[SYNC] ✅ Google Sheets synced", file=sys.stderr)
        except Exception as e:
            print(f"[SYNC] ❌ Google Sheets Error: {e}", file=sys.stderr)
            # Tetap lanjut meski Sheets gagal, data sudah di CSV
        
        # Update cache dengan data baru
        parsed = parse_metar(metar)
        qam = generate_qam(station, parsed, metar)
        narrative = generate_metar_narrative(parsed, metar)
        
        # Simpan ke wind history
        store_wind(parsed, station)
        
        latest_metar_data = {
            "status": "new",
            "qam": qam,
            "raw": metar,
            "narrative": narrative,
            "time": datetime.utcnow().strftime("%d-%m-%Y %H:%M:%S"),
            "wind_dir": parsed.get("wind_dir"),
            "wind_speed": parsed.get("wind_speed_kt"),
            "wind_gust": parsed.get("wind_gust_kt"),
            "temp": parsed.get("temperature_c"),
            "dewpoint": parsed.get("dewpoint_c"),
            "visibility_m": parsed.get("visibility_m"),
            "cloud": parsed.get("cloud"),
            "qnh": parsed.get("pressure_hpa"),
            "weather": parsed.get("weather"),
            "metar_status": parsed.get("status", "normal"),
            "report_type": parsed.get("report_type", "METAR"),
            "auto_fetch": auto_fetch,
            "last_update": datetime.utcnow().isoformat() + "Z"
        }
        last_metar_update = latest_metar_data["last_update"]
        
        print(f"[SYNC] ✅ Data saved successfully at {last_metar_update}", file=sys.stderr)
        return True
        
    except Exception as e:
        print(f"[SYNC] ❌ Critical Update Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return False

# =========================

def background_metar_loop():
    print("✅ Background loop started")
    global last_metar_update, auto_fetch, latest_metar_data

    while True:
        try:
            if not auto_fetch:
                print("[SYSTEM] Auto fetch is DISABLED - skipping METAR update")
                time.sleep(80)
                continue

            station = "WARR"
            print(f"\n=== Background loop iteration ===")
            print(f"[LOOP] Triggering update for station: {station}")
            
            success = update_metar_data_and_sync(station)
            if success:
                print("[LOOP] Background sync successful", file=sys.stderr)
            else:
                print("[LOOP] Background sync failed or no new data", file=sys.stderr)

        except Exception as e:
            print(f"[LOOP] ERROR: {e}")
            import traceback
            traceback.print_exc()

        print(f"[LOOP] Sleeping for 60 seconds...")
        time.sleep(60)

@app.route("/download_history", methods=["POST"])
def download_history():
    station = request.form["icao"].upper()
    start_date = request.form["start_date"]
    end_date = request.form["end_date"]

    # Use unified fetcher to support Vercel/Sheets
    df = fetch_history_from_source()
    if df.empty:
        return "No data available in history", 404
        
    df["time"] = pd.to_datetime(df["time"])

    start = pd.to_datetime(start_date)
    end = pd.to_datetime(end_date)

    results = df[
        (df["station"] == station) &
        (df["time"] >= start) &
        (df["time"] <= end)
    ]

    if results.empty:
        return f"No records found for {station} between {start_date} and {end_date}", 404

    output = io.StringIO()
    results.to_csv(output, index=False)

    return send_file(
        io.BytesIO(output.getvalue().encode()),
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"{station}_history.csv"
    )

# =========================
# METAR VALIDATOR
# =========================
def validate_metar(metar: str) -> list[str]:
    """
    Comprehensive METAR Validator for 10 groups
    Returns a list of error strings or ["✅ METAR Valid"]
    """
    if not metar:
        return ["❌ Data METAR kosong"]

    errors: list[str] = []
    
    # Pre-clean: remove = and handle multiple spaces
    clean_metar = metar.replace("=", "").strip()
    tokens = clean_metar.split()

    if len(tokens) < 5:
        return ["❌ Format METAR terlalu pendek atau tidak lengkap"]

    # Skip first token if it's "METAR" or "SPECI"
    idx = 0
    if tokens[idx] in ["METAR", "SPECI"]:
        idx += 1

    # 1. ICAO Station (4 letters)
    if idx < len(tokens):
        if not re.match(r'^[A-Z]{4}$', tokens[idx]):
            errors.append(f"❌ ICAO station salah: {tokens[idx]} (harus 4 huruf Kapital)")
        idx += 1

    # 2. Time Group (6 digits + Z)
    if idx < len(tokens):
        if not re.match(r'^\d{6}Z$', tokens[idx]):
            errors.append(f"❌ Format waktu salah: {tokens[idx]} (harus 6 digit + Z)")
        idx += 1

    # 3. Wind Group (may be absent in some reports)
    if idx < len(tokens):
        # Supports: 05006KT, 18012G20KT, VRB03KT, 00000KT
        wind_pattern = r'^(\d{3}|VRB)\d{2}(G\d{2,3})?KT$'
        if re.match(wind_pattern, tokens[idx]):
            idx += 1  # Valid wind, advance
        elif tokens[idx].endswith('KT'):
            # Looks like wind but malformed
            errors.append(str(f"❌ Format angin salah: {tokens[idx]}"))
            idx += 1
        else:
            # No wind group found (missing), report error but don't skip the token
            errors.append(str("❌ Angin tidak ditemukan"))

    # From here on, groups can be more dynamic. We'll search for them.
    remaining_tokens: list[str] = tokens[idx:] if idx < len(tokens) else []  # pyre-ignore
    
    # 4. Visibility Group (4 digits, CAVOK, or M1/4SM etc - sticking to metric for now)
    vis_found = False
    for i, t in enumerate(remaining_tokens):
        if re.match(r'^\d{4}$', t) or t == "CAVOK":
            vis_found = True
            # Check for 9999 or specific digits
            break
    if not vis_found:
        errors.append("❌ Cek Visibility (harus 4 digit atau CAVOK)")

    # 5. Weather Group (Optional)
    # 6. Cloud Group
    cloud_prefixes = ["FEW", "SCT", "BKN", "OVC", "SKC", "NSC", "NCD", "VV"]
    cloud_found = False
    for t in remaining_tokens:
        if any(t.startswith(p) for p in cloud_prefixes):
            cloud_found = True
            if t in ["SKC", "NSC", "NCD"]: continue
            
            # Extract height (3 digits)
            height_part = re.search(r'\d{3}', t)
            if not height_part:
                errors.append(f"❌ Tinggi awan salah: {t} (harus 3 digit)")
            
            # Check prefix format specifically if it matches but has wrong height
            prefix = t[:3]  # pyre-ignore
            if prefix in ["FEW", "SCT", "BKN", "OVC"]:
                height = t[3:6]  # pyre-ignore
                if not height.isdigit() or len(height) != 3:
                     errors.append(f"❌ Format kelompok awan salah: {t} (tinggi harus 3 digit)")
        
        # Catch errors like TL103 mentioned by user
        #elif re.match(r'^[A-Z]{2}\d+', t) and "/" not in t and "KT" not in t and not t.startswith("Q"):
        #     errors.append(f"❌ Format kelompok awan salah: {t} (prefix harus FEW/SCT/BKN/OVC)")

    # 7. Temperature/Dewpoint (M?dd/M?dd)
    temp_pattern = r'^M?\d{2}/M?\d{2}$'
    if not any(re.match(temp_pattern, t) for t in remaining_tokens):
        # Using any() for CAVOK check to avoid Pyre __contains__ bug
        has_cavok = any(t == "CAVOK" for t in remaining_tokens)
        if not has_cavok or not any("/" in t for t in remaining_tokens):
            errors.append("❌ Format suhu TT/TdTd salah (contoh: 31/24)")

    # 8. Pressure Group (Q + 4 digits)
    if not any(re.match(r'^Q\d{4}$', t) for t in remaining_tokens):
        errors.append(str("❌ Tekanan (QNH) salah (contoh: Q1010)"))

    # 9. Trend Group (Optional check)
    trend_keywords = ["NOSIG", "TEMPO", "BECMG"]
    # No hard error if missing, but can check format if present

    if len(errors) == 0:
        return ["✅ METAR Valid"]

    return errors

@app.route("/api/validate", methods=["POST"])
def api_validate():
    data = request.get_json()
    if not data or "metar" not in data:
        return jsonify({"results": ["❌ Input tidak ditemukan"]}), 400
    
    metar = data["metar"].strip()
    results = validate_metar(metar)
    return jsonify({"results": results})

# =========================
# MANUAL METAR PARSER
# =========================
@app.route("/manual_parser", methods=["GET", "POST"])
def manual_parser():

    station = "WARR"
    raw_metar = None
    parsed_qam = None
    validation_results = None

    if request.method == "POST":
        raw_metar = request.form["raw_metar"].strip()
        station = raw_metar.split()[1] if len(raw_metar.split()) > 1 else "WARR"
        parsed = parse_metar(raw_metar)
        parsed_qam = generate_qam(station, parsed, raw_metar)
        validation_results = validate_metar(raw_metar)

    return render_template(
        "manual_parser.html",
        station=station,
        raw_metar=raw_metar,
        parsed_qam=parsed_qam,
        validation_results=validation_results,
        auto_fetch=auto_fetch,
        last_metar_update=last_metar_update
    )

# ============================================
# DAILY METAR RECORD MANAGER - BACKEND
# ============================================
@app.route("/api/records/today")
def get_today_records():
    """Mengambil data METAR khusus hari ini (Reset otomatis 00:00 UTC)"""
    now_utc = datetime.utcnow()
    today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    
    try:
        # Mengambil data langsung dari Google Sheets
        all_records = sheets_handler.get_all_data()
        if not all_records:
            return jsonify({"records": [], "date": format_indonesian_date(now_utc)})
        
        df = pd.DataFrame(all_records)
        df["time"] = pd.to_datetime(df["time"], errors='coerce')
        
        # Filter: Hanya data dari 00:00 UTC hari ini
        today_df = df[df["time"] >= today_start].sort_values("time", ascending=False)
        
        records = []
        for _, row in today_df.iterrows():
            metar = str(row["metar"])
            parsed = parse_metar(metar)
            
            record_status = "normal"
            if ',' in metar:
                record_status = "invalid"
            elif ' COR ' in metar or 'CCA' in metar:
                record_status = "cor"
            elif ' AMD ' in metar:
                record_status = "amd"
            elif 'SPECI' in metar:
                record_status = "speci"
            else:
                try:
                    minute = int(parsed.get("minute", "-1"))
                    if minute != 0 and minute != 30 and minute != -1:
                        record_status = "speci"
                except:
                    pass
                    
            records.append({
                "time": pd.to_datetime(row["time"]).strftime("%Y-%m-%d %H:%M UTC"),
                "station": row["station"],
                "metar": metar,
                "record_status": record_status
            })
            
        # Chart Data extraction (Ascending order)
        chart_df = today_df.sort_values("time", ascending=True)
        chart_labels = []
        chart_temps = []
        chart_pressures = []
        chart_winds = []
        chart_gusts = []
        
        for _, row in chart_df.iterrows():
            p = parse_metar(str(row["metar"]))
            chart_labels.append(row["time"].strftime("%H:%M"))
            chart_temps.append(float(p.get("temperature_c")) if p.get("temperature_c") else None)
            chart_pressures.append(float(p.get("pressure_hpa")) if p.get("pressure_hpa") else None)
            chart_winds.append(float(p.get("wind_speed_kt")) if p.get("wind_speed_kt") else None)
            chart_gusts.append(float(p.get("wind_gust_kt")) if p.get("wind_gust_kt") else None)

        return jsonify({
            "date": format_indonesian_date(now_utc),
            "records": records,
            "count": len(records),
            "chart_data": {
                "labels": chart_labels,
                "temps": chart_temps,
                "pressures": chart_pressures,
                "winds": chart_winds,
                "gusts": chart_gusts
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/records/yesterday")
def get_yesterday_records():
    """Mengambil data METAR lengkap dari hari kemarin"""
    now_utc = datetime.utcnow()
    yesterday = now_utc - timedelta(days=1)
    y_start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
    y_end = yesterday.replace(hour=23, minute=59, second=59)
    
    try:
        all_records = sheets_handler.get_all_data()
        df = pd.DataFrame(all_records)
        df["time"] = pd.to_datetime(df["time"], errors='coerce')
        
        # Filter: Rentang waktu penuh hari kemarin (UTC)
        yesterday_df = df[(df["time"] >= y_start) & (df["time"] <= y_end)].sort_values("time", ascending=False)
        
        records = []
        for _, row in yesterday_df.iterrows():
            metar = str(row["metar"])
            parsed = parse_metar(metar)
            
            record_status = "normal"
            if ',' in metar:
                record_status = "invalid"
            elif ' COR ' in metar or 'CCA' in metar:
                record_status = "cor"
            elif ' AMD ' in metar:
                record_status = "amd"
            elif 'SPECI' in metar:
                record_status = "speci"
            else:
                try:
                    minute = int(parsed.get("minute", "-1"))
                    if minute != 0 and minute != 30 and minute != -1:
                        record_status = "speci"
                except:
                    pass

            records.append({
                "time": pd.to_datetime(row["time"]).strftime("%Y-%m-%d %H:%M UTC"),
                "station": row["station"],
                "metar": metar,
                "record_status": record_status
            })
            
        # Chart Data extraction (Ascending order)
        chart_df = yesterday_df.sort_values("time", ascending=True)
        chart_labels = []
        chart_temps = []
        chart_pressures = []
        chart_winds = []
        chart_gusts = []
        
        for _, row in chart_df.iterrows():
            p = parse_metar(str(row["metar"]))
            chart_labels.append(row["time"].strftime("%H:%M"))
            chart_temps.append(float(p.get("temperature_c")) if p.get("temperature_c") else None)
            chart_pressures.append(float(p.get("pressure_hpa")) if p.get("pressure_hpa") else None)
            chart_winds.append(float(p.get("wind_speed_kt")) if p.get("wind_speed_kt") else None)
            chart_gusts.append(float(p.get("wind_gust_kt")) if p.get("wind_gust_kt") else None)

        return jsonify({
            "date": format_indonesian_date(yesterday),
            "records": records,
            "count": len(records),
            "chart_data": {
                "labels": chart_labels,
                "temps": chart_temps,
                "pressures": chart_pressures,
                "winds": chart_winds,
                "gusts": chart_gusts
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ============ VERCEL HANDLER ============
# Vercel akan otomatis mencari objek bernama 'app'
# Tidak perlu custom handler wrapper yang kompleks

# Untuk local development
if __name__ == "__main__":
    # Pre-populate wind history for Wind Rose
    load_wind_history()
    
    # Initialize last_metar_update from CSV if available
    if os.path.exists(CSV_FILE):
        try:
            df = pd.read_csv(CSV_FILE)
            if not df.empty:
                last_time = df.iloc[-1]["time"]
                # Convert to ISO format (UTC)
                last_metar_update = pd.to_datetime(last_time).isoformat() + "Z"
                print(f"[INIT] last_metar_update initialized: {last_metar_update}", file=sys.stderr)
        except Exception as e:
            print(f"[INIT] Failed to initialize last_metar_update: {e}", file=sys.stderr)

    # Use environment variables for production binding and debug mode
    debug_mode = os.environ.get("FLASK_DEBUG", "False") == "True"
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=debug_mode, use_reloader=False)
