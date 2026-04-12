import re

with open('debug_output_2.txt', encoding='utf-8') as f:
    markdown_text = f.read()

speed_match = re.search(r'Course / Speed\s*\|\s*[\d\.]+°\s*/\s*([\d\.]+)\s*kn', markdown_text)
scraped_speed = float(speed_match.group(1)) if speed_match else 0.0

eta_match = re.search(r'ETA:\s*(.*?)\s*\(', markdown_text)
scraped_eta_raw = eta_match.group(1) if eta_match else None

dest_match = re.search(r'en route to the port of ([^,]+)', markdown_text, re.IGNORECASE)
if not dest_match:
    dest_match = re.search(r'Destination\s*[:|]\s*([^\n\r]+)', markdown_text, re.IGNORECASE)

scraped_destination = dest_match.group(1).replace('*', '').strip() if dest_match else 'Unknown'

print(f"Speed: {scraped_speed}")
print(f"ETA raw: {scraped_eta_raw}")
print(f"Destination: {scraped_destination}")
