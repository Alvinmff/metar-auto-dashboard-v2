import sys
import os
import re

# Add project root to sys.path
sys.path.append(os.getcwd())

# Import the function from api.index
from api.index import validate_metar

test_cases = [
    # VRB Wind
    ("METAR WARR 060400Z VRB02KT 5000 HZ FEW020 31/24 Q1010", "VRB Valid (02KT)"),
    ("METAR WARR 060400Z VRB05KT 5000 HZ FEW020 31/24 Q1010", "VRB Invalid (05KT)"),
    
    # Gusts
    ("METAR WARR 060400Z 10010G20KT 5000 HZ FEW020 31/24 Q1010", "Gust Valid (10KT diff)"),
    ("METAR WARR 060400Z 10012G20KT 5000 HZ FEW020 31/24 Q1010", "Gust Invalid (8KT diff)"),
    
    # Visibility HZ/BR
    ("METAR WARR 060400Z 10010KT 5000 HZ FEW020 31/24 Q1010", "HZ Valid (5000m)"),
    ("METAR WARR 060400Z 10010KT 6000 HZ FEW020 31/24 Q1010", "HZ Invalid (6000m)"),
    ("METAR WARR 060400Z 10010KT 5000 BR FEW020 31/24 Q1010", "BR Valid (5000m)"),
    ("METAR WARR 060400Z 10010KT 6000 BR FEW020 31/24 Q1010", "BR Invalid (6000m)"),
]

print("--- METAR Validation Tests ---")
for metar, desc in test_cases:
    results = validate_metar(metar)
    status = "PASS" if (("✅" in results[0]) if "Valid" in desc else ("❌" in results[0] or any("❌" in r for r in results))) else "FAIL"
    print(f"[{status}] {desc}")
    print(f"  METAR: {metar}")
    print(f"  Result: {results}")
    print("-" * 30)
