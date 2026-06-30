"""
ML-based package name resolver using TF-IDF and K-Nearest Neighbors.

Resolves virtual dependency names (e.g. python3dist(requests), pkgconfig(glib-2.0))
to real RPM package names using a trained model.

scikit-learn is an optional dependency - the resolver degrades gracefully if not installed.
"""

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

try:
    import joblib  # pragma: no cover
    from sklearn.feature_extraction.text import TfidfVectorizer  # pragma: no cover
    from sklearn.neighbors import NearestNeighbors  # pragma: no cover

    HAS_SKLEARN = True  # pragma: no cover
except ImportError:  # pragma: no cover
    HAS_SKLEARN = False

logger = logging.getLogger(__name__)

# Default paths
_MODULE_DIR = Path(__file__).parent
_DEFAULT_MODEL_PATH = _MODULE_DIR / "data" / "model.joblib"
_CACHE_DIR = Path.home() / ".cache" / "vibebuild"
_CACHE_FILE = _CACHE_DIR / "ml_name_cache.json"

# Regex for names with a version suffix in the first segment (python3.13-, php8-, ruby3.2-)
_VERSIONED_PREFIX_RE = re.compile(r"^[A-Za-z]+\d+(\.\d+)+(-|$)")


def _canonical_score(rpm_name: str) -> tuple:
    """
    RPM name canonicalization score. Lower = more canonical.

    Canonical-looking names are ``python3-X``, ``perl-X``, ``ruby-X`` without
    a minor version in the first segment. Names with a minor version
    (``python3.13-X``, ``php8.2-X``) are considered less canonical.
    """
    versioned = 1 if _VERSIONED_PREFIX_RE.match(rpm_name) else 0
    return (versioned, len(rpm_name), rpm_name)


class MLPackageResolver:
    """
    ML-based resolver that predicts RPM package names from dependency strings.

    Uses TF-IDF character n-grams and K-Nearest Neighbors with cosine distance
    to find the closest known package provide string and return the corresponding
    RPM and source RPM names.

    Example:
        resolver = MLPackageResolver()
        if resolver.is_available():
            result = resolver.predict("python3dist(requests)")
            # {"rpm_name": "python3-requests", "srpm_name": "python-requests"}
    """

    # Class-level defaults: needed for tests that use __new__(cls)
    # without calling __init__, and for backward compatibility.
    _model_path: Path = _DEFAULT_MODEL_PATH
    _load_attempted: bool = False

    def __init__(self, model_path: str | None = None):
        """
        Initialize the ML package resolver.

        Args:
            model_path: Path to saved model file. If None, looks for the default
                        model at vibebuild/data/model.joblib.

        The model itself is NOT loaded in __init__. It is loaded lazily on the first
        call of `predict()` or `is_available()`, which gives a fast CLI start
        for commands where ML may not be needed at all (`--analyze-only` on a
        package where the rules cover all BR).
        """
        self.confidence_threshold = 0.3
        self._vectorizer: Any = None
        self._nn_model: Any = None
        self._rpm_names: list[str] = []
        self._srpm_names: list[str] = []
        self._provides: list[str] = []
        self._model_loaded = False
        self._cache: dict[str, dict] = {}
        self._cache_dirty = False

        # Remember the path, but do NOT load the model now.
        self._model_path = Path(model_path) if model_path else _DEFAULT_MODEL_PATH
        self._load_attempted = False

        # The light prediction cache is loaded right away because it is small (~KB).
        self._load_cache()

    def _ensure_loaded(self) -> bool:
        """Lazy model loading. Returns True if the model is available."""
        if self._model_loaded or self._load_attempted:
            return self._model_loaded
        self._load_attempted = True
        if not HAS_SKLEARN:
            return False
        if not self._model_path.exists():
            return False
        try:
            self.load(str(self._model_path))
        except Exception as e:
            logger.warning("Failed to load model from %s: %s", self._model_path, e)
            return False
        return self._model_loaded

    def is_available(self) -> bool:
        """
        Check if the resolver is ready to make predictions.

        Returns:
            True if scikit-learn is installed and a model has been loaded
            (triggers lazy load on the first call).
        """
        return HAS_SKLEARN and self._ensure_loaded()

    def train(self, data: list[dict]) -> None:
        """
        Train the model on provide-to-package mapping data.

        Args:
            data: List of dicts with keys "provide", "rpm_name", "srpm_name".
                  Example: [{"provide": "python3dist(requests)",
                             "rpm_name": "python3-requests",
                             "srpm_name": "python-requests"}, ...]

        Raises:
            RuntimeError: If scikit-learn is not installed.
            ValueError: If data is empty or malformed.
        """
        if not HAS_SKLEARN:
            raise RuntimeError(
                "scikit-learn is required for training. Install with: pip install scikit-learn"
            )

        if not data:
            raise ValueError("Training data cannot be empty")

        self._provides = [entry["provide"] for entry in data]
        self._rpm_names = [entry["rpm_name"] for entry in data]
        self._srpm_names = [entry["srpm_name"] for entry in data]

        logger.info("Training TF-IDF vectorizer on %d samples...", len(data))
        self._vectorizer = TfidfVectorizer(
            analyzer="char_wb",
            ngram_range=(2, 5),
            max_features=20000,
        )
        tfidf_matrix = self._vectorizer.fit_transform(self._provides)

        logger.info("Fitting NearestNeighbors model...")
        self._nn_model = NearestNeighbors(
            n_neighbors=min(10, len(data)),
            metric="cosine",
            algorithm="brute",
        )
        self._nn_model.fit(tfidf_matrix)

        self._model_loaded = True
        vocab_size = len(self._vectorizer.vocabulary_)
        logger.info("Model trained: %d samples, vocabulary size %d", len(data), vocab_size)

    def predict(self, dep_name: str) -> dict | None:
        """
        Predict the RPM package name for a dependency string.

        Top-K majority voting:
          1. Take the k=10 nearest neighbors.
          2. Vote for the SRPM, neighbor weight = (1 - distance).
          3. Inside the winning SRPM pick the canonical RPM:
             prefer `python3-X` over `python3.13-X`, all else equal the shorter one.

        Returns:
            Dict with "rpm_name" and "srpm_name" keys, or None if all neighbors are
            worse than confidence_threshold / the model is not loaded.
        """
        if not self.is_available():
            return None

        cache_key = self._cache_key(dep_name)
        if cache_key in self._cache:
            return self._cache[cache_key]

        query_vec = self._vectorizer.transform([dep_name])
        distances, indices = self._nn_model.kneighbors(query_vec)

        # top-1 is strict: its SRPM is our SRPM answer if it passed the threshold
        best_distance = float(distances[0][0])
        if best_distance > self.confidence_threshold:
            logger.debug(
                "Prediction for '%s' below confidence threshold (best=%.3f > %.3f)",
                dep_name,
                best_distance,
                self.confidence_threshold,
            )
            return None
        best_srpm = self._srpm_names[int(indices[0][0])]

        # Among the top-K neighbors of the same SRPM pick the canonical RPM
        # (prefer python3- over python3.13-; shorter is better).
        in_srpm = [
            (self._rpm_names[int(i)], float(d))
            for i, d in zip(indices[0], distances[0], strict=True)
            if self._srpm_names[int(i)] == best_srpm and float(d) <= self.confidence_threshold
        ]
        in_srpm.sort(key=lambda t: (_canonical_score(t[0]), t[1]))
        best_rpm = in_srpm[0][0]

        result = {"rpm_name": best_rpm, "srpm_name": best_srpm}

        self._cache[cache_key] = result
        self._cache_dirty = True
        self._save_cache()

        return result

    def save(self, path: str) -> None:
        """
        Save the trained model to disk.

        Args:
            path: File path to save the model (joblib format).

        Raises:
            RuntimeError: If scikit-learn is not installed or model is not trained.
        """
        if not HAS_SKLEARN:
            raise RuntimeError("scikit-learn is required. Install with: pip install scikit-learn")

        if not self._model_loaded:
            raise RuntimeError("No model to save. Train or load a model first.")

        model_data = {
            "vectorizer": self._vectorizer,
            "nn_model": self._nn_model,
            "rpm_names": self._rpm_names,
            "srpm_names": self._srpm_names,
            "provides": self._provides,
            "confidence_threshold": self.confidence_threshold,
        }

        output_path = Path(path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(model_data, str(output_path), compress=9)
        logger.info("Model saved to %s", path)

    def load(self, path: str) -> None:
        """
        Load a trained model from disk.

        Args:
            path: File path to load the model from (joblib format).

        Raises:
            RuntimeError: If scikit-learn is not installed.
            FileNotFoundError: If model file does not exist.
        """
        if not HAS_SKLEARN:
            raise RuntimeError("scikit-learn is required. Install with: pip install scikit-learn")

        model_path = Path(path)
        if not model_path.exists():
            raise FileNotFoundError(f"Model file not found: {path}")

        model_data = joblib.load(str(model_path))

        self._vectorizer = model_data["vectorizer"]
        self._nn_model = model_data["nn_model"]
        self._rpm_names = model_data["rpm_names"]
        self._srpm_names = model_data["srpm_names"]
        self._provides = model_data["provides"]
        self.confidence_threshold = model_data.get(
            "confidence_threshold", self.confidence_threshold
        )
        self._model_loaded = True
        logger.info("Model loaded from %s (%d entries)", path, len(self._provides))

    def _cache_key(self, dep_name: str) -> str:
        """Generate a stable cache key for a dependency name."""
        return hashlib.sha256(dep_name.encode("utf-8")).hexdigest()[:16]

    def _load_cache(self) -> None:
        """Load prediction cache from disk."""
        try:
            if _CACHE_FILE.exists():
                with open(_CACHE_FILE, encoding="utf-8") as f:
                    self._cache = json.load(f)
                logger.debug("Loaded %d cached predictions", len(self._cache))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load prediction cache: %s", e)
            self._cache = {}

    def _save_cache(self) -> None:
        """Save prediction cache to disk."""
        if not self._cache_dirty:
            return

        try:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            with open(_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, indent=2)
            self._cache_dirty = False
            logger.debug("Saved %d cached predictions", len(self._cache))
        except OSError as e:
            logger.warning("Failed to save prediction cache: %s", e)
