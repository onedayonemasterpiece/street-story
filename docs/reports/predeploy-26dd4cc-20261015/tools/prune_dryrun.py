"""Dry run of devcoveer_install.prune_release_environments: computes, never removes."""
import importlib.util, json, subprocess, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('inst', sys.argv[1]); inst = importlib.util.module_from_spec(spec); spec.loader.exec_module(inst)
current, previous, preserve = sys.argv[2], sys.argv[3], sys.argv[4:]
refs = inst.release_process_references()
protected = inst.protect_environment_owners({current, previous, *preserve} | refs)
would_remove, kept_symlink = [], []
for release in sorted(inst.RELEASES_ROOT.iterdir()):
    if release.name in protected or not inst.SHA_RE.fullmatch(release.name) or release.is_symlink() or not release.is_dir():
        continue
    env = release / 'venv'
    if env.is_symlink():
        kept_symlink.append(release.name[:8]); continue
    if env.is_dir():
        size = int(subprocess.run(['du', '-sb', str(env)], capture_output=True, text=True).stdout.split()[0])
        would_remove.append({'release': release.name, 'venv_bytes': size})
print(json.dumps({'process_refs': sorted(refs), 'protected': sorted(protected), 'would_remove_venv': would_remove,
                  'symlink_env_untouched': kept_symlink}, indent=1))
