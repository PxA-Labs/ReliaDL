import os
import subprocess

def get_conflicted_files():
    result = subprocess.run(['git', 'diff', '--name-only', '--diff-filter=U'], capture_output=True, text=True)
    return [f for f in result.stdout.split('\n') if f.strip()]

def resolve_file(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except Exception as e:
        print(f"Could not read {filepath}: {e}")
        return False
        
    resolved_lines = []
    in_conflict = False
    current_chunk_ours = []
    current_chunk_theirs = []
    reading_ours = False
    reading_theirs = False
    
    for line in lines:
        if line.startswith('<<<<<<<'):
            in_conflict = True
            reading_ours = True
            reading_theirs = False
            current_chunk_ours = []
            current_chunk_theirs = []
            continue
        elif line.startswith('======='):
            reading_ours = False
            reading_theirs = True
            continue
        elif line.startswith('>>>>>>>'):
            in_conflict = False
            reading_theirs = False
            
            # keep ours (HEAD)
            resolved_lines.extend(current_chunk_ours)
            
            # for README.md, if theirs has "Security & OpenSSF Compliance", append it too, but maybe deduplicate?
            text_theirs = "".join(current_chunk_theirs)
            if "OpenSSF" in text_theirs or "securityscorecards" in text_theirs:
                resolved_lines.extend(current_chunk_theirs)
            
            continue
            
        if in_conflict:
            if reading_ours:
                current_chunk_ours.append(line)
            elif reading_theirs:
                current_chunk_theirs.append(line)
        else:
            resolved_lines.append(line)
            
    with open(filepath, 'w', encoding='utf-8') as f:
        f.writelines(resolved_lines)
    print(f"Resolved {filepath}")
    return True

files = get_conflicted_files()
for f in files:
    resolve_file(f)

