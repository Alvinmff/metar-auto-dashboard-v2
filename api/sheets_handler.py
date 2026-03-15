import gspread  # type: ignore
from google.oauth2.service_account import Credentials  # type: ignore
import os
import json
import pandas as pd  # type: ignore
from datetime import datetime
import sys

# Spreadsheet ID from user
SPREADSHEET_ID = "1wtngMXZTznjJGtR3WcDES71aup4EDWCRocQyiWblAkU"

class GoogleSheetHandler:
    def __init__(self):
        self.scope = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        self.client = None
        self.sheet = None
        self._authenticate()

    def _authenticate(self):
        """Authenticate using Env Var (Vercel) or local credentials.json"""
        try:
            creds_json = os.environ.get("GOOGLE_SHEETS_CREDENTIALS")
            creds = None
            if creds_json:
                try:
                    # Parse JSON string from env var
                    info = json.loads(creds_json)
                    creds = Credentials.from_service_account_info(info, scopes=self.scope)
                    print(f"[SHEETS] Credentials for {info.get('client_email')} parsed successfully", file=sys.stderr)
                except Exception as json_err:
                    print(f"[SHEETS] ❌ JSON Parse Error on Credentials: {json_err}", file=sys.stderr)
                    return
            else:
                # Local fallback
                creds_path = os.path.join(os.path.dirname(__file__), "credentials.json")
                if os.path.exists(creds_path):
                    print(f"[SHEETS] Authenticating via {creds_path}", file=sys.stderr)
                    creds = Credentials.from_service_account_file(creds_path, scopes=self.scope)
                else:
                    print("[SHEETS] ❌ No credentials found: GOOGLE_SHEETS_CREDENTIALS env var is MISSING", file=sys.stderr)
            
            if not creds:
                return

            print("[SHEETS] Authorizing client...", file=sys.stderr)
            client = gspread.authorize(creds)
            self.client = client
            
            # Try to open the spreadsheet
            if client is not None:
                print(f"[SHEETS] Opening spreadsheet by key: {SPREADSHEET_ID}", file=sys.stderr)
                spreadsheet = client.open_by_key(SPREADSHEET_ID)
                if spreadsheet is not None:
                    print("[SHEETS] Spreadsheet opened, fetching worksheet...", file=sys.stderr)
                    worksheet = spreadsheet.get_worksheet(0)
                    if worksheet is not None:
                        self.sheet = worksheet
                        print("[SHEETS] ✅ Connected to spreadsheet successfully", file=sys.stderr)
                        
                        # Initialization Check: Ensure headers exist if sheet is empty
                        try:
                            first_row = worksheet.get_values('A1:C1')
                            if not first_row:
                                worksheet.append_row(["station", "time", "metar"])
                                print("[SHEETS] Initialized headers in new sheet", file=sys.stderr)
                        except Exception as header_err:
                             print(f"[SHEETS] Header check skip: {header_err}", file=sys.stderr)
                    else:
                        print("[SHEETS] ❌ Could not find worksheet in spreadsheet", file=sys.stderr)
                else:
                    print("[SHEETS] ❌ Could not open spreadsheet", file=sys.stderr)
            else:
                print("[SHEETS] ❌ Failed to authorize client", file=sys.stderr)

        except Exception as e:
            import traceback
            print(f"[SHEETS] ❌ Authentication Error: {e}", file=sys.stderr)
            traceback.print_exc()

    def save_metar(self, station, time, metar):
        """Append a new METAR record to Google Sheets"""
        if self.sheet is None:
            # Try to re-authenticate if missing (lazy auth)
            self._authenticate()
            
        sheet = self.sheet
        if sheet is None:
            print("[SHEETS] ❌ Cannot save: Final authentication check failed", file=sys.stderr)
            return False

        try:
            # Format time if it's a datetime object
            if isinstance(time, datetime):
                time_str = time.strftime("%Y-%m-%d %H:%M:%S")
            else:
                time_str = str(time)

            print(f"[SHEETS] Appending row: {station}, {time_str}", file=sys.stderr)
            sheet.append_row([station, time_str, metar])
            print(f"[SHEETS] ✅ Data successfully saved to Google Sheets for {station}", file=sys.stderr)
            return True
        except Exception as e:
            print(f"[SHEETS] ❌ Error saving to Sheets: {e}", file=sys.stderr)
            return False

    def get_recent_data(self, limit=20):
        """Fetch the last N records from Sheets for deduplication context"""
        if self.sheet is None:
            self._authenticate()
            
        sheet = self.sheet
        if sheet is None:
            return []

        try:
            # Get total rows
            all_rows = sheet.get_all_values()
            if len(all_rows) <= 1: # Header only
                return []
            
            # Extract header
            header = all_rows[0]
            # Get last 'limit' rows
            recent_rows = all_rows[-limit:]
            
            # Convert to list of dicts (like get_all_records)
            data = []
            for row in recent_rows:
                if len(row) >= len(header):
                    # Use comprehension with type ignore for persistent linter confusion
                    row_dict = {header[i]: row[i] for i in range(len(header))}  # type: ignore
                    data.append(row_dict)
            return data
        except Exception as e:
            print(f"[SHEETS] ❌ Error fetching recent data: {e}", file=sys.stderr)
            return []

    def get_all_data(self):
        """Fetch all records from Sheets as a list of dicts"""
        if self.sheet is None:
            self._authenticate()
            
        sheet = self.sheet
        if sheet is None:
            return []

        try:
            print("[SHEETS] Fetching all data records...", file=sys.stderr)
            return sheet.get_all_records()
        except Exception as e:
            print(f"[SHEETS] ❌ Error fetching all data: {e}", file=sys.stderr)
            return []

    def sync_to_local(self, local_path):
        """Fetch all data from Sheets and save to local CSV (for Vercel warmup)"""
        if self.sheet is None:
            self._authenticate()
            
        sheet = self.sheet
        if sheet is None:
            print("[SHEETS] ❌ Cannot sync: Authentication failed", file=sys.stderr)
            return False

        try:
            print(f"[SHEETS] Syncing data to {local_path}...", file=sys.stderr)
            all_data = sheet.get_all_records()
            if not all_data:
                print("[SHEETS] Sheet is empty, nothing to sync", file=sys.stderr)
                return False

            df = pd.DataFrame(all_data)
            # Standardize time format during sync
            if "time" in df.columns:
                df["time"] = pd.to_datetime(df["time"], format='mixed').dt.strftime("%Y-%m-%d %H:%M:%S")
            df.to_csv(local_path, index=False)
            print(f"[SHEETS] ✅ Sync complete: {len(df)} rows saved to local", file=sys.stderr)
            return True
        except Exception as e:
            print(f"[SHEETS] ❌ Error syncing from Sheets: {e}", file=sys.stderr)
            return False

# Singleton instance
sheets_handler = GoogleSheetHandler()
