#!/usr/bin/env python3
"""
Train the ML package name resolver model.

Takes collected training data (JSON) and produces a trained model (joblib)
that can be used by MLPackageResolver to predict RPM package names from
dependency strings. Data from vibebuild/data/alias_training.json (e.g. python3
-> python3.12) is merged automatically so that vibebuild --download-only python3
works with the trained model.

Usage:
    python scripts/train_model.py --input training_data.json
    python scripts/train_model.py --input training_data.json --output vibebuild/data/model.joblib
    python scripts/train_model.py --input training_data.json --test-split 0.1
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Add project root to path so we can import vibebuild
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def load_training_data(path: str) -> list[dict]:
    """
    Load training data from a JSON file.

    Args:
        path: Path to the JSON file.

    Returns:
        List of training data entries.

    Raises:
        FileNotFoundError: If file does not exist.
        json.JSONDecodeError: If file is not valid JSON.
    """
    data_path = Path(path)
    if not data_path.exists():
        raise FileNotFoundError(f"Training data file not found: {path}")

    with open(data_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError("Training data must be a JSON array")

    # Merge alias training data (e.g. python3 -> python3.12) if present
    alias_path = PROJECT_ROOT / "vibebuild" / "data" / "alias_training.json"
    if alias_path.exists():
        with open(alias_path, "r", encoding="utf-8") as f:
            aliases = json.load(f)
        if isinstance(aliases, list):
            data = data + aliases
            logger.info("Merged %d alias entries from %s", len(aliases), alias_path.name)

    # Validate entries
    required_keys = {"provide", "rpm_name", "srpm_name"}
    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise ValueError(f"Entry {i} is not a dict")
        missing = required_keys - entry.keys()
        if missing:
            raise ValueError(f"Entry {i} missing keys: {missing}")

    return data


def evaluate_model(resolver, test_data: list[dict]) -> dict:
    """
    Evaluate the trained model on test data.

    Args:
        resolver: Trained MLPackageResolver instance.
        test_data: List of test data entries.

    Returns:
        Dict with evaluation metrics.
    """
    total = len(test_data)
    correct_rpm = 0
    correct_srpm = 0
    no_prediction = 0

    for entry in test_data:
        result = resolver.predict(entry["provide"])
        if result is None:
            no_prediction += 1
            continue
        if result["rpm_name"] == entry["rpm_name"]:
            correct_rpm += 1
        if result["srpm_name"] == entry["srpm_name"]:
            correct_srpm += 1

    predicted = total - no_prediction
    return {
        "total": total,
        "predicted": predicted,
        "no_prediction": no_prediction,
        "coverage": predicted / total if total > 0 else 0.0,
        "rpm_accuracy": correct_rpm / predicted if predicted > 0 else 0.0,
        "srpm_accuracy": correct_srpm / predicted if predicted > 0 else 0.0,
        "correct_rpm": correct_rpm,
        "correct_srpm": correct_srpm,
    }


def main() -> None:
    """Main entry point for model training."""
    parser = argparse.ArgumentParser(
        description="Train the ML package name resolver model"
    )
    parser.add_argument(
        "--input",
        type=str,
        required=True,
        help="Path to training data JSON file",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=str(PROJECT_ROOT / "vibebuild" / "data" / "model.joblib"),
        help="Path to save trained model (default: vibebuild/data/model.joblib)",
    )
    parser.add_argument(
        "--test-split",
        type=float,
        default=0.1,
        help="Fraction of data to hold out for testing (default: 0.1)",
    )
    parser.add_argument(
        "--skip-eval",
        action="store_true",
        help="Skip evaluation on test set (faster; model is still saved)",
    )
    args = parser.parse_args()

    # Load data
    logger.info("Loading training data from %s...", args.input)
    data = load_training_data(args.input)
    logger.info("Loaded %d entries", len(data))

    # Split data
    if args.test_split > 0 and len(data) > 10:
        split_idx = max(1, int(len(data) * (1 - args.test_split)))
        train_data = data[:split_idx]
        test_data = data[split_idx:]
        logger.info("Train/test split: %d / %d", len(train_data), len(test_data))
    else:
        train_data = data
        test_data = []
        logger.info("Using all %d entries for training (no test split)", len(data))

    # Import and train
    try:
        from vibebuild.ml_resolver import MLPackageResolver
    except ImportError as e:
        logger.error("Failed to import MLPackageResolver: %s", e)
        logger.error("Make sure scikit-learn is installed: pip install scikit-learn")
        sys.exit(1)

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

    logger.info("Training model...")
    start_time = time.time()
    resolver.train(train_data)
    elapsed = time.time() - start_time
    logger.info("Training completed in %.2f seconds", elapsed)

    # Print training stats
    logger.info("--- Training Stats ---")
    logger.info("Samples:         %d", len(train_data))
    if hasattr(resolver._vectorizer, "vocabulary_"):
        logger.info("Vocabulary size: %d", len(resolver._vectorizer.vocabulary_))
    logger.info("Unique RPMs:     %d", len(set(resolver._rpm_names)))
    logger.info("Unique SRPMs:    %d", len(set(resolver._srpm_names)))

    # Evaluate on test set
    if test_data and not args.skip_eval:
        logger.info("--- Evaluation on test set ---")
        metrics = evaluate_model(resolver, test_data)
        logger.info("Test samples:    %d", metrics["total"])
        logger.info("Predictions:     %d (coverage: %.1f%%)", metrics["predicted"],
                     metrics["coverage"] * 100)
        logger.info("RPM accuracy:    %.1f%% (%d/%d)", metrics["rpm_accuracy"] * 100,
                     metrics["correct_rpm"], metrics["predicted"])
        logger.info("SRPM accuracy:   %.1f%% (%d/%d)", metrics["srpm_accuracy"] * 100,
                     metrics["correct_srpm"], metrics["predicted"])

    # Save model
    logger.info("Saving model to %s...", args.output)
    resolver.save(args.output)
    logger.info("Done!")


if __name__ == "__main__":
    main()
