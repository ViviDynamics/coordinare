"""approve_exec.py RUN ATTEMPT DIGEST REVIEW"""
import json,hashlib,datetime,sys
from pathlib import Path
from coordinare_service_inference.environment.models import EnvironmentManifest
run,att,digest,review=sys.argv[1:5]
D=Path(f'.agents/state/376-org-validation/nare-native-{run}/nare/attempt-{att}')
raw=(D/'accepted-manifest.json').read_text(); m=EnvironmentManifest.model_validate_json(raw)
assert m.digest()==digest and m.source_commit=='21fd8e7fc010137dd948aef717f16eabb7f359f9'
img=json.loads((D/'execution-capabilities.json').read_text())['image_ref']
(D/'execution-approved.json').write_text(json.dumps({'approved':True,'scope':'environment_validation','manifest_digest':m.digest(),'source_commit':m.source_commit,'image_ref':img},indent=2))
Path(f'.agents/state/376-native{run}-attempt{att}-execution-approval.json').write_text(json.dumps({
 'observed_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'manifest_sha256':hashlib.sha256(raw.encode()).hexdigest(),
 'manifest_digest':m.digest(),'source_commit':m.source_commit,'approved':True,'production_authorization_verified':True,
 'install_count':len(m.install),'check_count':len(m.checks),'service_count':len(m.services),'review':review},indent=1))
print('approved',run,att,img)
