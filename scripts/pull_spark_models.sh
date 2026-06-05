#!/usr/bin/env bash
# Pull a list of models onto the Spark via the Ollama API, sequentially.
set -u
SPARK="http://192.168.3.30:11434"
for m in deepseek-r1:32b qwq:32b gemma3:27b deepseek-r1:70b; do
  echo "=== pull $m  $(date '+%H:%M:%S') ==="
  curl -s "$SPARK/api/pull" -d "{\"name\":\"$m\"}" | \
    python3 -c "import sys,json
last=''
for line in sys.stdin:
    try: d=json.loads(line)
    except: continue
    s=d.get('status','')
    if s!=last and ('pulling manifest' in s or 'success' in s or 'error' in d): print('  ',s or d.get('error')); last=s
print('  done:',m)"
done
echo "=== ALL PULLS DONE $(date '+%H:%M:%S') ==="
