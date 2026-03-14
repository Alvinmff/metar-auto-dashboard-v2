import gspread
from google.oauth2.service_account import Credentials
import os
import json
import pandas as pd
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
            
            if creds_json:
                print("[SHEETS] Authenticating via Environment Variable", file=sys.stderr)
                # Parse JSON string from env var
                info = json.loads(creds_json)
                creds = Credentials.from_service_account_info(info, scopes=self.scope)
            else:
                # Local fallback
                creds_path = os.path.join(os.path.dirname(__file__), "credentials.json")
                if os.path.exists(creds_path):
                    print(f"[SHEETS] Authenticating via {creds_path}", file=sys.stderr)
                    creds = Credentials.from_service_account_file(creds_path, scopes=self.scope)
            if not creds:
                print("[SHEETS] ❌ No credentials obtained!", file=sys.stderr)
                return

            client = gspread.authorize(creds)
            self.client = client
            
            # Try to open the spreadsheet
            if client is not None:
                spreadsheet = client.open_by_key(SPREADSHEET_ID)
                if spreadsheet is not None:
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
            print(f"[SHEETS] ❌ Authentication Error: {e}", file=sys.stderr)

    def save_metar(self, station, time, metar):
        """Append a new METAR record to Google Sheets"""
        sheet = self.sheet
        if sheet is None:
            print("[SHEETS] ❌ Cannot save: Not authenticated", file=sys.stderr)
            return False

        try:
            # Format time if it's a datetime object
            if isinstance(time, datetime):
                time_str = time.strftime("%Y-%m-%d %H:%M:%S")
            else:
                time_str = str(time)

            sheet.append_row([station, time_str, metar])
            print(f"[SHEETS] Data saved: {station} at {time_str}", file=sys.stderr)
            return True
        except Exception as e:
            print(f"[SHEETS] ❌ Error saving to Sheets: {e}", file=sys.stderr)
            return False

    def sync_to_local(self, local_path):
        """Fetch all data from Sheets and save to local CSV (for Vercel warmup)"""
        sheet = self.sheet
        if sheet is None:
            print("[SHEETS] ❌ Cannot sync: Not authenticated", file=sys.stderr)
            return False

        try:
            print(f"[SHEETS] Syncing data to {local_path}...", file=sys.stderr)
            all_data = sheet.get_all_records()
            if not all_data:
                print("[SHEETS] Sheet is empty, nothing to sync", file=sys.stderr)
                return False

            df = pd.DataFrame(all_data)
            df.to_csv(local_path, index=False)
            print(f"[SHEETS] ✅ Sync complete: {len(df)} rows saved to local", file=sys.stderr)
            return True
        except Exception as e:
            print(f"[SHEETS] ❌ Error syncing from Sheets: {e}", file=sys.stderr)
            return False

# Singleton instance
sheets_handler = GoogleSheetHandler()
