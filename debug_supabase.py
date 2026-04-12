import os
import asyncio
from dotenv import load_dotenv
from supabase import create_client
import json

load_dotenv()
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

response = supabase.table('vessel_schedules') \
    .select('id, vessel_id, actual_atb, master_vessels(imo_number, vessel_name)') \
    .is_('actual_atb', 'null') \
    .execute()

with open('debug_output.json', 'w') as f:
    json.dump(response.data, f, indent=2)
