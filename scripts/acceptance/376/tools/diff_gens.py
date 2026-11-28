"""diff_gens.py RUN A B: what changed between two accepted manifests of a run."""
import json, sys
from pathlib import Path
from coordinare_service_inference.environment.models import EnvironmentManifest
run, a, b = sys.argv[1:4]
D = Path(f'.agents/state/376-org-validation/nare-native-{run}/nare')
x = EnvironmentManifest.model_validate_json((D / f'attempt-{a}/accepted-manifest.json').read_text())
y = EnvironmentManifest.model_validate_json((D / f'attempt-{b}/accepted-manifest.json').read_text())
print('digest', y.digest(), y.source_commit, 'activation same', x.activation == y.activation,
      'mappings same', x.criterion_checks == y.criterion_checks)
if x.activation != y.activation:
    print('ACT', json.dumps(y.activation.model_dump(mode='json')))
for grp in ('install', 'checks', 'services'):
    old = {c.id: c for c in getattr(x, grp)}; new = {c.id: c for c in getattr(y, grp)}
    for i in new:
        if i not in old:
            print('ADD', grp, i, json.dumps(new[i].model_dump(mode='json', exclude={'scope'}))[:700])
        elif old[i] != new[i]:
            print('CHG', grp, i, json.dumps(new[i].model_dump(mode='json', exclude={'scope'}))[:700])
    for i in old:
        if i not in new:
            print('DEL', grp, i)
