"""
ML training for name_resolver: high-level collect/train/save functions.

This module is a facade over `scripts/collect_training_data.py` and
`scripts/train_model.py` so that they can be called from the CLI
(`vibebuild train`) without spawning a subprocess.

All these functions are slow (network, training). Tests do not call them; for
unit tests the smoke scenario is `vibebuild train --help`.
"""

from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _import_scripts_module(name: str):
    """Import a module from scripts/ so as not to duplicate the logic."""
    scripts_dir = _PROJECT_ROOT / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    return importlib.import_module(name)


def collect(release: int, arch: str, output: str) -> int:
    """Collect training data from Fedora repodata. Returns the number of records."""
    mod = _import_scripts_module("collect_training_data")

    base_url = mod.discover_mirror(release, arch)
    mappings = []
    if base_url:
        primary_url = mod.find_primary_xml_url(base_url)
        if primary_url:
            mappings = mod.download_and_parse_primary(primary_url)
    if not mappings:
        logger.info("primary.xml.gz gave no data, falling back to dnf repoquery")
        mappings = mod.collect_via_dnf(release, arch)
    if not mappings:
        raise RuntimeError("Failed to collect any training data records")
    mappings = mod.deduplicate(mappings)

    out_path = Path(output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    import json

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(mappings, f, indent=2)
    logger.info("Saved %d records to %s", len(mappings), output)
    return len(mappings)


def train(
    input_path: str,
    output: str,
    test_split: float = 0.05,
    eval_sample: int = 500,
    seed: int = 42,
) -> dict:
    """Train the model from training data. Returns metrics."""
    mod = _import_scripts_module("train_model")
    data = mod.load_training_data(input_path)

    import random

    rng = random.Random(seed)
    data = list(data)
    rng.shuffle(data)
    if test_split > 0 and len(data) > 10:
        split_idx = max(1, int(len(data) * (1 - test_split)))
        train_data = data[:split_idx]
        test_data = data[split_idx:]
    else:
        train_data, test_data = data, []

    from vibebuild.ml_resolver import MLPackageResolver

    resolver = MLPackageResolver.__new__(MLPackageResolver)
    resolver.confidence_threshold = 0.3
    resolver._vectorizer = None
    resolver._nn_model = None
    resolver._rpm_names = []
    resolver._srpm_names = []
    resolver._provides = []
    resolver._model_loaded = False
    resolver._cache = {}
    resolver._cache_dirty = False

    resolver.train(train_data)
    metrics: dict = {"train_size": len(train_data), "test_size": len(test_data)}
    if test_data:
        eval_data = test_data
        if eval_sample and len(test_data) > eval_sample:
            eval_data = rng.sample(test_data, eval_sample)
        metrics.update(mod.evaluate_model(resolver, eval_data))
    resolver.save(output)
    return metrics


def collect_and_train(
    release: int,
    arch: str,
    output: str,
    raw_path: str | None = None,
    keep_raw: bool = False,
    test_split: float = 0.05,
    eval_sample: int = 500,
    seed: int = 42,
) -> dict:
    """Full pipeline: collect → train → save → (optionally) delete raw."""
    import tempfile

    if raw_path is None:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", prefix="vb-training-", delete=False
        )
        tmp.close()
        raw_path = tmp.name

    try:
        n = collect(release, arch, raw_path)
        logger.info("Raw records: %d", n)
        metrics = train(raw_path, output, test_split=test_split, eval_sample=eval_sample, seed=seed)
        return metrics
    finally:
        if not keep_raw and raw_path:
            try:
                Path(raw_path).unlink(missing_ok=True)
            except OSError:
                pass
