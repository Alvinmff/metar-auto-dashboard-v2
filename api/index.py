from flask import Flask, render_template, request, send_file, jsonify  # pyre-ignore
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
    # Ini memastikan history yang sudah masuk kodingan tidak hilang di dashboard
    if not os.path.exists(CSV_FILE) and os.path.exists(ROOT_CSV):
        try:
            import shutil
            shutil.copy2(ROOT_CSV, CSV_FILE)
            print("[INIT] Base history copied from project root to /tmp/", file=sys.stderr)
        except Exception as e:
            print(f"[INIT] Failed to copy base history: {e}", file=sys.stderr)
else:
    CSV_FILE = ROOT_CSV
    print("[INIT] Running locally - Using local storage", file=sys.stderr)

# ============ ERROR HANDLER ============
@app.errorhandler(Exception)
def handle_exception(e):
    """Global error handler untuk catch semua exception"""
    error_msg = f"ERROR: {str(e)}\n{traceback.format_exc()}"
    print(error_msg, file=sys.stderr)
    return jsonify({
        "error": str(e),
        "traceback": traceback.format_exc()
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
    "+TSRA", "-TSRA", "-TS", "+TS"
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

        if part in ["HZ","RA","+RA","-RA","TSRA","+TSRA","TS","+TS","-TS","SH","DS","SS","-TSRA"]:
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
# GENERATE QAM FORMAT
# =========================
def generate_qam(station, parsed, raw_metar):
    # Convert parsed data to display format
    display = format_parsed_for_display(parsed)
    
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
DATE    : {date_str}
TIME    : {time_str} UTC
========================
WIND    : {display['wind']}
VIS     : {display['visibility']}
WEATHER : {display['weather']}
CLOUD   : {display['cloud']}
TT/TD   : {display['temp_td']}
QNH     : {display['qnh']} MB
QFE     : {display['qfe']} MB
TREND   : {display['trend']}
"""
    return qam

# =========================
# GENERATE NARRATIVE TEXT
# =========================
def generate_metar_narrative(parsed, raw_metar=None):
    """Generate Indonesian narrative text from METAR data (without emojis)"""
    if not parsed:
        return "Data METAR tidak valid."
    
    # Use format_parsed_for_display to convert new structure to display format
    display = format_parsed_for_display(parsed)
    
    text: list[str] = []
    
    # Get station info
    station = display.get('station', 'Unknown')
    if raw_metar and station == "-":
        station_match = re.match(r'([A-Z]{4})', raw_metar)
        if station_match:
            station = station_match.group(1)
    if station == "-":
        station = "Unknown"
    
    # Get observation time from METAR or parsed data
    day, hour, minute = "??", "??", "??"
    month_name = ""
    year = datetime.utcnow().year
    
    if raw_metar:
        time_match = re.search(r'(\d{2})(\d{2})(\d{2})Z', raw_metar)
        if time_match:
            day, hour, minute = time_match.groups()
            # Get month from current date
            month_name = datetime.utcnow().strftime("%B")
    elif display.get('day') != "-":
        day = display.get('day', '??')
        hour = display.get('hour', '??')
        minute = display.get('minute', '??')
        month_name = datetime.utcnow().strftime("%B")
    
    # Convert month name to Indonesian
    month_map = {
        "January": "Januari",
        "February": "Februari",
        "March": "Maret",
        "April": "April",
        "May": "Mei",
        "June": "Juni",
        "July": "Juli",
        "August": "Agustus",
        "September": "September",
        "October": "Oktober",
        "November": "November",
        "December": "Desember"
    }
    month_indonesian = month_map.get(month_name, month_name)
    
    text.append(f"Observasi cuaca di Bandara Juanda ({station}) pada tanggal {day} {month_indonesian} {year} pukul {hour}:{minute} UTC menunjukkan kondisi berikut:")
    
    # Wind information
    wind = display.get('wind', '')
    if wind and wind != 'NIL':
        text.append(f"Angin dari arah {wind}.")
    
    # Visibility information
    vis = display.get('visibility', '')
    if vis and vis != 'NIL':
        if vis == "10 KM":
            text.append("Jarak pandang sekitar 10 kilometer.")
        elif "KM" in vis:
            km_val = vis.replace("KM", "").strip()
            text.append(f"Jarak pandang sekitar {km_val} kilometer.")
        elif "M" in vis:
            m_val = vis.replace("M", "").strip()
            text.append(f"Jarak pandang sekitar {m_val} meter.")
        else:
            text.append(f"Visibilitas {vis}.")
    
    # Weather information
    weather = display.get('weather', '')
    if weather and weather != 'NIL':
        weather_map = {
            "HZ": "kabut asap",
            "RA": "hujan",
            "+RA": "hujan lebat",
            "-RA": "hujan ringan",
            "TS": "badai petir",
            "-TS": "badai petir ringan",
            "+TS": "badai petir kuat",
            "SH": "hujan shower",
            "DS": "debu pasir",
            "SS": "pasir badai",
            "-TSRA": "badai petir ringan disertai hujan",
            "TSRA": "badai petir disertai hujan",
            "+TSRA": "badai petir kuat disertai hujan"
        }
        desc = weather_map.get(weather, weather)
        text.append(f"Terdapat fenomena cuaca berupa {desc}.")
    
    # Cloud information with cloud_map
    cloud = display.get('cloud', '')
    if cloud and cloud != 'NIL':
        cloud_map = {
            "FEW": "awan sedikit",
            "SCT": "awan tersebar",
            "BKN": "awan banyak",
            "OVC": "awan menutup langit"
        }
        
        # Parse cloud format: "BKN 2500FT" or "FEW015CB"
        cloud_match = re.match(r'([A-Z]{3})\s*(\d+)', cloud)
        if cloud_match:
            cloud_type = cloud_match.group(1)
            cloud_height = cloud_match.group(2)
            desc = cloud_map.get(cloud_type, cloud_type)
            extra = ""
            if "CB" in cloud:
                extra = " CB (Cumulonimbus)"
            elif "TCU" in cloud:
                extra = " TCU (Towering Cumulus)"
            text.append(f"Terdapat {desc} pada ketinggian {cloud_height} kaki.{extra}")
        else:
            text.append(f"Awan: {cloud}.")
    
    # Temperature and dewpoint
    temp_td = display.get('temp_td', '')
    if temp_td and temp_td != 'NIL':
        temp_match = re.match(r'(\d{2})/(\d{2})', temp_td)
        if temp_match:
            temp = temp_match.group(1)
            dewpoint = temp_match.group(2)
            text.append(f"Suhu {temp}°C dengan titik embun {dewpoint}°C.")
    
    # Pressure
    qnh = display.get('qnh', '')
    if qnh and qnh != 'NIL':
        text.append(f"Tekanan udara {qnh} hPa.")
    
    # Trend
    trend: str = str(display.get('trend', ''))
    if trend and trend != 'NIL':
        if trend == 'NOSIG':
            text.append("Tidak ada perubahan signifikan dalam waktu dekat.")
        elif trend.startswith('TEMPO'):
            tempo_content = trend.replace('TEMPO ', '', 1).strip()
            time_match = re.search(r'L(\d{4})', tempo_content)
            time_str = ""
            if time_match:
                time_val = time_match.group(1)
                time_str = f"pukul {time_val[:2]}:{time_val[2:]}"  # type: ignore
            vis_match = re.search(r'(\d{4})', tempo_content)
            vis_str = ""
            if vis_match:
                vis_val = int(vis_match.group(1))
                # Use the detailed visibility logic matching format_visibility
                if vis_val >= 10000 or vis_val == 9999:
                    vis_str = "10 km"
                elif vis_val == 8000:
                    vis_str = "8 km"
                elif vis_val == 7000:
                    vis_str = "7 km"
                elif vis_val == 6000:
                    vis_str = "6 km"
                elif vis_val == 5000:
                    vis_str = "5 km"
                elif vis_val == 4000:
                    vis_str = "4 km"
                elif vis_val == 3000:
                    vis_str = "3 km"
                elif vis_val == 2000:
                    vis_str = "2 km"
                elif vis_val == 1500:
                    vis_str = "1.5 km"
                elif vis_val == 1000:
                    vis_str = "1 km"
                elif vis_val >= 1000:
                    vis_str = f"{vis_val // 1000} km"
                else:
                    vis_str = f"{vis_val} m"
            weather_map = {
                "HZ": "kabut asap", "RA": "hujan", "+RA": "hujan lebat",
                "-RA": "hujan ringan","-TSRA": "badai petir ringan disertai hujan",
                "TSRA": "badai petir disertai hujan", "+TSRA": "badai petir kuat disertai hujan", 
                "TS": "badai petir", "-TS": "badai petir ringan", "+TS": "badai petir kuat"
            }

            weather_found = None
            for code, desc in weather_map.items():
                if tempo_content.find(code) != -1:
                    weather_found = desc
                    break
            tempo_parts: list[str] = []
            if time_str:
                tempo_parts.append(time_str)
            if vis_str:
                tempo_parts.append(f"visibilitas {vis_str}")
            if weather_found:
                tempo_parts.append(str(weather_found))
            if tempo_parts:
                text.append(f"Dalam waktu dekat, diperkirakan akan terjadi {', '.join(tempo_parts)}.")
            else:
                text.append(f"Tren: {trend}.")
        else:
            text.append(f"Tren: {trend}.")
    
    return " ".join(text)

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

@app.route("/api/latest")
def api_latest():

    if not os.path.exists(CSV_FILE):
        return jsonify({"labels": [], "temps": [], "pressures": []})

    df = pd.read_csv(CSV_FILE).tail(20)

    # Convert metar column to string and handle NaN values
    df["metar"] = df["metar"].fillna("").astype(str)

    labels = df["time"].tolist()
    temps: list = []
    pressures: list = []

    for metar in df["metar"]:

        # ===== TEMPERATURE =====
        if metar and isinstance(metar, str):
            temp_match = re.search(r'(\d{2})/(\d{2})', metar)
            if temp_match:
                temps.append(int(temp_match.group(1)))
            else:
                temps.append(None)
        else:
            temps.append(None)

        # ===== PRESSURE (QNH) =====
        if metar and isinstance(metar, str):
            qnh_match = re.search(r'Q(\d{4})', metar)
            if qnh_match:
                pressures.append(int(qnh_match.group(1)))
            else:
                pressures.append(None)
        else:
            pressures.append(None)

    return jsonify({
        "labels": labels,
        "temps": temps,
        "pressures": pressures
    })

# =========================
# API GET FULL HISTORY
# =========================
@app.route("/api/history")
def api_history():
    """API endpoint to get full history data including METAR strings"""
    if not os.path.exists(CSV_FILE):
        return jsonify({"data": []})

    df = pd.read_csv(CSV_FILE).tail(20)
    
    # Convert metar column to string and handle NaN values
    df["metar"] = df["metar"].fillna("").astype(str)

    # Reverse to show newest first
    df = df.iloc[::-1]

    history_data = []
    for _, row in df.iterrows():
        history_data.append({
            "time": str(row["time"]),
            "station": row["station"],
            "metar": row["metar"]
        })

    return jsonify({"data": history_data})

# =========================
# API GET METAR HISTORY FOR CHARTS
# =========================
@app.route("/api/metar/history")
def api_metar_history():
    """API endpoint to get history data for charts (temp, pressure, wind, gust)"""
    if not os.path.exists(CSV_FILE):
        return jsonify({"data": []})

    df = pd.read_csv(CSV_FILE).tail(30)

    df["time"] = pd.to_datetime(df["time"])
    df = df.sort_values("time")

    df["metar"] = df["metar"].fillna("").astype(str)

    history_data = []
    for _, row in df.iterrows():
        metar = row["metar"]
        
        # Extract temperature
        temp = 0
        temp_match = re.search(r'(\d{2})/(\d{2})', metar)
        if temp_match:
            temp = int(temp_match.group(1))
        
        # Extract pressure
        pressure = 0
        qnh_match = re.search(r'Q(\d{4})', metar)
        if qnh_match:
            pressure = int(qnh_match.group(1))
        
        # Extract wind speed and gust
        wind = 0
        gust = None
        wind_match = re.search(r'(\d{3}|VRB)(\d{2,3})(G(\d{2,3}))?KT', metar)
        if wind_match:
            wind = int(wind_match.group(2))
            if wind_match.group(4):
                gust = int(wind_match.group(4))
        
        history_data.append({
            "time": str(row["time"]),
            "temp": temp,
            "pressure": pressure,
            "wind": wind,
            "gust": gust
        })

    return jsonify({"data": history_data})

# =========================
# API GET METAR (Single)
# =========================
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
@app.route("/api/windrose/<station>")
def windrose_api(station):
    """API endpoint to get historical wind data for Wind Rose chart"""
    global wind_history
    
    # Fallback: if wind_history is empty, try to populate from CSV
    if not wind_history and os.path.exists(CSV_FILE):
        load_wind_history()
        
    data = list(wind_history)
    return jsonify(data)

# =========================
# API HISTORY - Technical History for Charts
# =========================
@app.route("/api/latest")
@app.route("/api/history")
@app.route("/api/metar/history")
def get_history_api():
    """Returns historical data in JSON format for charts and tables"""
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
        df = pd.read_csv(CSV_FILE).tail(30)
        df["metar"] = df["metar"].fillna("").astype(str)
        
        # Format for history table (newest first)
        data_list = []
        for _, row in df.iloc[::-1].iterrows():
            parsed = parse_metar(str(row["metar"]))
            data_list.append({
                "time": pd.to_datetime(row["time"]).strftime("%Y-%m-%d %H:%M"),
                "station": row["station"],
                "metar": row["metar"],
                "temp": extract_temp(str(row["metar"])),
                "pressure": extract_pressure(str(row["metar"])),
                "wind": parsed.get("wind_speed_kt"),
                "gust": parsed.get("wind_gust_kt")
            })

        # Format for charts (oldest to newest)
        labels = [pd.to_datetime(t).strftime("%H:%M") for t in df["time"]]
        temps = [extract_temp(m) for m in df["metar"]]
        pressures = [extract_pressure(m) for m in df["metar"]]
        
        return jsonify({
            "data": data_list,
            "labels": labels,
            "temps": temps,
            "pressures": pressures
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
# HOME ROUTE
# =========================
@app.route("/", methods=["GET", "POST"])
def home():
    global last_metar_update, auto_fetch
    station = "WARR"
    metar = None
    parsed = None
    qam = None
    narrative = None
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
            print(f"[HOME] Fetching live METAR for {station}...", file=sys.stderr)
            metar = get_metar(station)
        else:
            print(f"[HOME] Auto fetch is DISABLED - skipping home route live fetch", file=sys.stderr)
        
        if metar:
            print(f"[HOME] Live METAR received: {metar[:50]}...", file=sys.stderr)
            
            if not os.path.exists(CSV_FILE):
                df = pd.DataFrame(columns=["station", "time", "metar"])
                df.to_csv(CSV_FILE, index=False)

            df = pd.read_csv(CSV_FILE)

            if len(df) == 0 or df.iloc[-1]["metar"] != metar:
                new_row = {
                    "station": station,
                    "time": datetime.utcnow(),
                    "metar": metar
                }
                df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
                df.to_csv(CSV_FILE, index=False)
                print("[HOME] New METAR saved to CSV", file=sys.stderr)

            parsed = parse_metar(metar)
            qam = generate_qam(station, parsed, metar)
            narrative = generate_metar_narrative(parsed, metar)
            
            # Store wind data for Wind Rose
            store_wind(parsed, station)
            print("[HOME] Live METAR processed successfully", file=sys.stderr)
        else:
            print("[HOME] No live METAR available, checking CSV history...", file=sys.stderr)
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
                        last_metar_update = pd.to_datetime(last_row["time"]).isoformat() + "Z"
                    except:
                        last_metar_update = datetime.utcnow().isoformat() + "Z"
    except Exception as e:
        print(f"[HOME] CRITICAL ERROR: {e}", file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)
        # Fallback will happen as metrics are pre-initialized to None

    # Read history and prepare chart data
    if os.path.exists(CSV_FILE):
        history = pd.read_csv(CSV_FILE).tail(20)
        
        # Convert metar column to string and handle NaN values
        history["metar"] = history["metar"].fillna("").astype(str)
        
        has_history = not history.empty
        if has_history:
            labels = history['time'].tolist()
            temps = [extract_temp(m) for m in history['metar'].tolist()]
            pressures = [extract_pressure(m) for m in history['metar'].tolist()]

            # Reverse data so newest is at the top in table (charts show oldest->newest left to right)
            labels = history['time'].tolist()
            temps = [extract_temp(m) for m in history['metar'].tolist()]
            pressures = [extract_pressure(m) for m in history['metar'].tolist()]
        else:
            labels = []
    else:
        history = pd.DataFrame(columns=["station", "time", "metar"])
        labels = []
    
    # Create latest dict for the METAR display with status color
    latest = None
    if metar and parsed:
        latest = {
            "station": station,
            "metar": metar,
            "status": parsed.get("status", "normal")
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
        last_saved=last_saved,
        temps=temps,
        pressures=pressures,
        labels=labels,
        has_history=has_history,
        auto_fetch=auto_fetch,
        last_metar_update=last_update_display
    )

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

    if request.method == "POST":
        station = request.form.get("icao", "WARR").upper()
        start_date = request.form.get("start_date", "")
        end_date = request.form.get("end_date", "")
        
        print(f"[HISTORY] Station: {station}, Start: {start_date}, End: {end_date}")

        if start_date and end_date:
            # Read CSV
            if os.path.exists(CSV_FILE):
                df = pd.read_csv(CSV_FILE)
                print(f"[HISTORY] Total rows in CSV: {len(df)}")
                
                # Convert time column to datetime
                df["time"] = pd.to_datetime(df["time"], errors='coerce')
                
                # Convert input dates (datetime-local gives format like "2026-02-23T13:29")
                start = pd.to_datetime(start_date)
                # Add one day to end date to include the full day
                end = pd.to_datetime(end_date) + pd.Timedelta(days=1)
                
                print(f"[HISTORY] Filter: station={station}, start={start}, end={end}")
                
                # Filter by station and date range
                results = df[
                    (df["station"] == station) &
                    (df["time"] >= start) &
                    (df["time"] <= end)
                ]
                
                print(f"[HISTORY] Filtered rows: {len(results) if results is not None else 0}")
                
                # Extract chart data if results exist
                if results is not None and not results.empty:
                    results = results.sort_values("time")  # Sort by time for chart
                    
                    for _, row in results.iterrows():
                        metar = str(row["metar"]) if pd.notna(row["metar"]) else ""
                        
                        # Format time for label
                        labels.append(str(row["time"]))
                        
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
                    
                    print(f"[HISTORY] Chart data extracted: {len(labels)} points")
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
        end_date=end_date
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

# =========================
# POLLING ENDPOINT (replaces WebSocket)
# =========================
@app.route("/api/latest-data")
def latest_data():
    """Endpoint for frontend polling — returns latest METAR + system status"""
    global last_metar_update
    
    # Fallback: if no cached data yet, try to build from CSV
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
                
                return jsonify({
                    "status": "cached",
                    "raw": metar,
                    "qam": qam,
                    "narrative": narrative,
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
                    "last_update": last_metar_update,
                    "server": "online"
                })
        except Exception as e:
            print(f"[POLL] Error building fallback data: {e}")

    # Return cached data with current system status
    data = latest_metar_data.copy() if latest_metar_data else {}
    data.update({
        "auto_fetch": auto_fetch,
        "last_update": last_metar_update,
        "server": "online"
    })
    return jsonify(data)

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
            print(f"[LOOP] Fetching METAR for station: {station}")
            
            metar = get_metar(station)
            
            if metar:
                print(f"[LOOP] METAR received: {metar[:50]}...")

                # Simpan ke CSV kalau beda
                if not os.path.exists(CSV_FILE):
                    print("[LOOP] CSV file doesn't exist, creating new one...")
                    df = pd.DataFrame(columns=["station","time","metar"])
                    df.to_csv(CSV_FILE, index=False)

                df = pd.read_csv(CSV_FILE)
                
                print(f"[LOOP] Current CSV rows: {len(df)}")

                if len(df) == 0 or df.iloc[-1]["metar"] != metar:
                    print("[LOOP] NEW METAR detected! Saving to CSV...")
                    new_row = {
                        "station": station,
                        "time": datetime.utcnow(),
                        "metar": metar
                    }

                    df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
                    df.to_csv(CSV_FILE, index=False)

                    print("🔥 NEW METAR SAVED!")

                    parsed = parse_metar(metar)

                    # 🔥 simpan ke wind history
                    store_wind(parsed, station)

                    qam = generate_qam(station, parsed, metar)
                    narrative = generate_metar_narrative(parsed, metar)
                    
                    print(f"[LOOP] QAM generated:\n{qam}")
                    print(f"[LOOP] Narrative generated:\n{narrative}")

                    # Update cached data for polling endpoint
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
                else:
                    print("[LOOP] METAR unchanged, skipping save")
                
                # Update last update timestamp whenever a fetch is successful
                last_metar_update = datetime.utcnow().isoformat() + "Z"
            else:
                print("[LOOP] ❌ No METAR received from NOAA!")

        except Exception as e:
            print(f"[LOOP] ERROR: {e}")
            import traceback
            traceback.print_exc()

        print(f"[LOOP] Sleeping for 80 seconds...")
        time.sleep(80)

@app.route("/download_history", methods=["POST"])
def download_history():

    station = request.form["icao"].upper()
    start_date = request.form["start_date"]
    end_date = request.form["end_date"]

    df = pd.read_csv(CSV_FILE)
    df["time"] = pd.to_datetime(df["time"])

    start = pd.to_datetime(start_date)
    end = pd.to_datetime(end_date)

    results = df[
        (df["station"] == station) &
        (df["time"] >= start) &
        (df["time"] <= end)
    ]

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
            errors.append(f"❌ Format angin salah: {tokens[idx]}")
            idx += 1
        else:
            # No wind group found (missing), report error but don't skip the token
            errors.append("❌ Angin tidak ditemukan")

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
        elif re.match(r'^[A-Z]{2}\d+', t) and "/" not in t and "KT" not in t and not t.startswith("Q"):
             errors.append(f"❌ Format kelompok awan salah: {t} (prefix harus FEW/SCT/BKN/OVC)")

    # 7. Temperature/Dewpoint (M?dd/M?dd)
    temp_pattern = r'^M?\d{2}/M?\d{2}$'
    if not any(re.match(temp_pattern, t) for t in remaining_tokens):
        # Using any() for CAVOK check to avoid Pyre __contains__ bug
        has_cavok = any(t == "CAVOK" for t in remaining_tokens)
        if not has_cavok or not any("/" in t for t in remaining_tokens):
            errors.append("❌ Format suhu TT/TdTd salah (contoh: 31/24)")

    # 8. Pressure Group (Q + 4 digits)
    if not any(re.match(r'^Q\d{4}$', t) for t in remaining_tokens):
        errors.append("❌ Tekanan (QNH) salah (contoh: Q1010)")

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
        raw_metar=raw_metar,
        parsed_qam=parsed_qam,
        validation_results=validation_results
    )

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
