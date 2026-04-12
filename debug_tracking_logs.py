import os
from dotenv import load_dotenv
from supabase import create_client
import json

load_dotenv()
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

response = supabase.table('tracking_logs') \
    .select('schedule_id, speed_sog, preview_eta_ais, destination') \
    .execute()

with open('debug_tracking_output.json', 'w') as f:
    json.dump(response.data, f, indent=2)
