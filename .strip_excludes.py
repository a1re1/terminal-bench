"""Emit a copy of a job config with dataset task exclusions removed.

run_sweep.sh uses this for GPU-capable backends: bench_job.yaml excludes the
GPU tasks so a local docker sweep does not abort, but on Modal those tasks are
exactly the ones we want. Keeping one config and stripping here avoids a second
config file drifting out of sync.

    python .strip_excludes.py <in.yaml> <out.yaml>
"""

import sys

import yaml


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    src, dst = sys.argv[1], sys.argv[2]
    with open(src) as fh:
        config = yaml.safe_load(fh)
    for dataset in config.get("datasets") or []:
        dataset.pop("exclude_task_names", None)
    with open(dst, "w") as fh:
        yaml.safe_dump(config, fh, sort_keys=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
