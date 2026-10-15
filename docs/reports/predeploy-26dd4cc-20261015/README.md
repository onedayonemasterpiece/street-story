# Predeploy evidence for 26dd4cc — 15 October 2026

Exact copies of the DevCoveer artifact files requested for dot's independent review of
[street-story-predeploy-6083-review-20261015.md](../street-story-predeploy-6083-review-20261015.md).
Production was not changed by these checks. No secrets: credential-file digests stay only in the local
`paired-deployment-review-6083cd3/rollback-plan.json`; the rollback script reads them from there.

| File | Local artifact | sha256 |
|---|---|---|
| `delivery-agent-qualification-current-native-giga-daf9b4a.json` | `/home/dev/artifacts/street-story/20261014T104149Z-research-unblocking-20261014/delivery-agent-qualification-current-native-giga-daf9b4a.json` | `47ce6efd0938989c87ec4ac7a5ef3f1b70320926a5751cbfb6d97585d2b42fb4` |
| `predeploy-checks.json` | `/home/dev/artifacts/street-story/20261014T104149Z-research-unblocking-20261014/paired-deployment-review-26dd4cc/predeploy-checks.json` | `1acff4cb389747733daa58fc405ce7bfca64fcc328bedd81cbda3bf13ecd8ebf` |
| `predeploy-runtime-baseline.json` | `/home/dev/artifacts/street-story/20261014T104149Z-research-unblocking-20261014/paired-deployment-review-26dd4cc/predeploy-runtime-baseline.json` | `e9896702fda41cf4a605fbbadf2912e458443333396b7914c29dae2fb0fabc22` |
| `rollback-to-9613.sh` | `/home/dev/artifacts/street-story/20261014T104149Z-research-unblocking-20261014/paired-deployment-review-26dd4cc/rollback-to-9613.sh` | `84770823cc42492ee2f29e2b2b50ba442ed3e7ecfcb3a2525332de1a14923b4d` |
| `tools/check_qualification.py` | `/home/dev/artifacts/street-story/20261014T104149Z-research-unblocking-20261014/paired-deployment-review-26dd4cc/tools/check_qualification.py` | `4eb9433f5ac0c956418d907576680c237444735da6ae9fefc43c36591e981c4d` |
| `tools/index_replay.py` | `/home/dev/artifacts/street-story/20261014T104149Z-research-unblocking-20261014/paired-deployment-review-26dd4cc/tools/index_replay.py` | `f188072840aab1dba97527dca59f47350332884b224cf29f08ab592754b53d45` |
| `tools/prune_dryrun.py` | `/home/dev/artifacts/street-story/20261014T104149Z-research-unblocking-20261014/paired-deployment-review-26dd4cc/tools/prune_dryrun.py` | `93bd0bf2a296a03d9fbc641275628d221e0fb0800b8c4fd6966b7690b09fb71b` |

Qualification receipts referenced by the proof were integrity-checked by `load_agent_qualification`
(see `predeploy-checks.json` → `qualification`); their contents are not published here.
