import os
import glob

def resolve_file(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except Exception as e:
        print(f"Could not read {filepath}: {e}")
        return False
        
    resolved_lines = []
    in_conflict = False
    current_chunk = []
    
    for line in lines:
        if line.startswith('<<<<<<<'):
            in_conflict = True
            current_chunk = []
            continue
        elif line.startswith('======='):
            # keep HEAD (our branch)
            resolved_lines.extend(current_chunk)
            current_chunk = []
            continue
        elif line.startswith('>>>>>>>'):
            # keep incoming (master) - wait, maybe we should keep incoming too?
            # actually if we keep both we might have duplicated lines.
            # let's just keep the incoming chunk if it contains something unique
            in_conflict = False
            
            # for README.md, if the incoming chunk has "Security & OpenSSF Compliance", we definitely want it
            text = "".join(current_chunk)
            if "OpenSSF" in text or "securityscorecards" in text:
                resolved_lines.extend(current_chunk)
            
            current_chunk = []
            continue
            
        if in_conflict:
            current_chunk.append(line)
        else:
            resolved_lines.append(line)
            
    with open(filepath, 'w', encoding='utf-8') as f:
        f.writelines(resolved_lines)
    return True

print("Script loaded")
