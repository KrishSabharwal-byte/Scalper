import requests
import json
import re
import datetime
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("InstrumentBuilder")

SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

def build():
    logger.info("Starting chunked download of OpenAPI Scrip Master...")
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(max_retries=5)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "*/*",
    }
    
    response = session.get(SCRIP_MASTER_URL, headers=headers, stream=True, timeout=120)
    response.raise_for_status()

    raw_path = "raw_master.json"
    logger.info("Writing raw master...")
    with open(raw_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=65536):
            if chunk:
                f.write(chunk)

    logger.info("Raw master downloaded successfully. Parsing JSON...")
    with open(raw_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    logger.info(f"Total entries parsed: {len(data)}")
    pattern = re.compile(r"^NIFTY(\d{2}[A-Z]{3}\d{2})(\d+)(CE|PE)$")
    nifty_opts = []

    for x in data:
        if x.get("exch_seg") == "NFO" and x.get("name") == "NIFTY" and x.get("instrumenttype") == "OPTIDX":
            sym = x.get("symbol", "")
            m = pattern.match(sym)
            if m:
                exp_str, strike_str, otype = m.groups()
                try:
                    exp_date = datetime.datetime.strptime(exp_str, "%d%b%y").strftime("%Y-%m-%d")
                    nifty_opts.append({
                        "symbol": sym,
                        "token": str(x.get("token")),
                        "expiry_str": exp_str,
                        "expiry_date": exp_date,
                        "strike": int(strike_str),
                        "option_type": otype,
                    })
                except Exception:
                    pass

    logger.info(f"Filtered {len(nifty_opts)} NIFTY option instruments.")
    with open("nifty_options.json", "w", encoding="utf-8") as f:
        json.dump(nifty_opts, f)
    logger.info("nifty_options.json saved successfully!")

if __name__ == "__main__":
    build()
