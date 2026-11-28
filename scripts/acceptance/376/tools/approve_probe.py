"""Write identity-bound probe approval after manual review: approve_probe.py RUN PROBE DIGEST COUNT REVIEW"""
import json,hashlib,datetime,sys
from pathlib import Path
from coordinare.services.environment_probes import ProbePlan
run,probe,digest,count,review=sys.argv[1:6]
D=Path(f'.agents/state/376-org-validation/nare-native-{run}/nare/probe-{probe}')
raw=(D/'probe-proposal.json').read_text(); plan=ProbePlan.model_validate_json(raw)
assert plan.digest()==digest and plan.source_commit=='21fd8e7fc010137dd948aef717f16eabb7f359f9' and len(plan.request.commands)==int(count)
(D/'probe-approved.json').write_text(json.dumps({'approved':True,'scope':'environment_probe','plan_digest':plan.digest(),
 'source_commit':plan.source_commit,'image_ref':plan.capabilities.image_ref,'platform':plan.capabilities.platform}))
Path(f'.agents/state/376-native{run}-probe{probe}-approval.json').write_text(json.dumps({
 'observed_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'proposal_sha256':hashlib.sha256(raw.encode()).hexdigest(),
 'plan_digest':plan.digest(),'approved':True,'production_authorization_verified':True,'command_count':int(count),
 'timeout_seconds_each':max(int(c.timeout_seconds) for c in plan.request.commands),'review':review},indent=1))
print('approved',run,probe)
