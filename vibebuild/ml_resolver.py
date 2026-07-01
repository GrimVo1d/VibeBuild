"""
ML-based package name resolver using TF-IDF and K-Nearest Neighbors.

Resolves virtual dependency names (e.g. python3dist(requests), pkgconfig(glib-2.0))
to real RPM package names using a trained model.

scikit-learn is an optional dependency - the resolver degrades gracefully if not installed.

The model is a NumPy .npz archive of plain arrays (names, TF-IDF vocabulary and
idf weights), loaded with allow_pickle=False, so a model file cannot run code when
it is loaded. The TF-IDF matrix is not stored: it is recomputed on load (about
10 s for a full Fedora release), which keeps the file at ~16 MB instead of ~280 MB.
"""

import importlib.util
import logging
import os
import re
from pathlib import Path
from typing import Any

from vibebuild.exceptions import VibeBuildError

# scikit-learn (with numpy and scipy) is imported only when a model is used:
# importing it costs about a second on every CLI start.
HAS_SKLEARN = importlib.util.find_spec("sklearn") is not None

logger = logging.getLogger(__name__)

MODEL_FORMAT = 2

# TF-IDF settings; stored in the model and checked on load
_NGRAM_RANGE = (2, 5)
_MAX_FEATURES = 20000
_N_NEIGHBORS = 10

# Regex for names with a version suffix in the first segment (python3.13-, php8-, ruby3.2-)
_VERSIONED_PREFIX_RE = re.compile(r"^[A-Za-z]+\d+(\.\d+)+(-|$)")


def default_model_path() -> Path:
    """$XDG_DATA_HOME/vibebuild/model.npz, ~/.local/share/vibebuild/model.npz by default."""
    data_home = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(data_home) / "vibebuild" / "model.npz"


def _canonical_score(rpm_name: str) -> tuple:
    """
    RPM name canonicalization score. Lower = more canonical.

    Canonical-looking names are ``python3-X``, ``perl-X``, ``ruby-X`` without
    a minor version in the first segment. Names with a minor version
    (``python3.13-X``, ``php8.2-X``) are considered less canonical.
    """
    versioned = 1 if _VERSIONED_PREFIX_RE.match(rpm_name) else 0
    return (versioned, len(rpm_name), rpm_name)


def _require_sklearn() -> None:
    if not HAS_SKLEARN:
        raise VibeBuildError("scikit-learn is required: pip install 'vibebuild[ml]'")


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

    def __init__(self, model_path: str | None = None):
        """
        Args:
            model_path: Path to a saved model; default_model_path() if None.

        The model is loaded lazily on the first `predict()` or `is_available()`,
        so commands that never need ML do not pay for loading it.
        """
        self.confidence_threshold = 0.3
        self._vectorizer: Any = None
        self._nn_model: Any = None
        self._matrix: Any = None
        self._rpm_names: list[str] = []
        self._srpm_names: list[str] = []
        self._provides: list[str] = []
        self._model_loaded = False
        self._cache: dict[str, dict | None] = {}
        self._model_path = Path(model_path) if model_path else default_model_path()
        self._load_attempted = False

    def _ensure_loaded(self) -> bool:
        """Lazy model loading. Returns True if the model is available."""
        if self._model_loaded or self._load_attempted:
            return self._model_loaded
        self._load_attempted = True
        if not HAS_SKLEARN or not self._model_path.exists():
            return False
        try:
            self.load(str(self._model_path))
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to load model from %s: %s", self._model_path, e)
            return False
        return self._model_loaded

    def is_available(self) -> bool:
        """True if scikit-learn is installed and a model is loaded (loads it on first call)."""
        return HAS_SKLEARN and self._ensure_loaded()

    def _fit_neighbors(self) -> None:
        from sklearn.neighbors import NearestNeighbors

        self._nn_model = NearestNeighbors(
            n_neighbors=min(_N_NEIGHBORS, len(self._provides)),
            metric="cosine",
            algorithm="brute",
        )
        self._nn_model.fit(self._matrix)

    def train(self, data: list[dict]) -> None:
        """
        Train the model on provide-to-package mapping data.

        Args:
            data: List of dicts with keys "provide", "rpm_name", "srpm_name".
                  Example: [{"provide": "python3dist(requests)",
                             "rpm_name": "python3-requests",
                             "srpm_name": "python-requests"}, ...]

        Raises:
            VibeBuildError: If scikit-learn is not installed or data is empty.
        """
        _require_sklearn()
        from sklearn.feature_extraction.text import TfidfVectorizer

        if not data:
            raise VibeBuildError("Training data cannot be empty")

        self._provides = [entry["provide"] for entry in data]
        self._rpm_names = [entry["rpm_name"] for entry in data]
        self._srpm_names = [entry["srpm_name"] for entry in data]

        logger.info("Training TF-IDF vectorizer on %d samples...", len(data))
        self._vectorizer = TfidfVectorizer(
            analyzer="char_wb", ngram_range=_NGRAM_RANGE, max_features=_MAX_FEATURES
        )
        self._matrix = self._vectorizer.fit_transform(self._provides)
        self._fit_neighbors()
        self._cache.clear()
        self._model_loaded = True
        logger.info(
            "Model trained: %d samples, vocabulary size %d",
            len(data),
            len(self._vectorizer.vocabulary_),
        )

    def predict(self, dep_name: str) -> dict | None:
        """
        Predict the RPM package name for a dependency string.

        The nearest known provide decides the SRPM if its cosine distance is within
        confidence_threshold. Among the k=10 nearest neighbors from that SRPM the
        most canonical RPM name wins: `python3-X` over `python3.13-X`, then the shorter.

        Returns:
            Dict with "rpm_name" and "srpm_name" keys, or None if all neighbors are
            worse than confidence_threshold / the model is not loaded.
        """
        if not self.is_available():
            return None
        if dep_name in self._cache:
            return self._cache[dep_name]

        query_vec = self._vectorizer.transform([dep_name])
        distances, indices = self._nn_model.kneighbors(query_vec)

        # top-1 is strict: its SRPM is our SRPM answer if it passed the threshold
        best_distance = float(distances[0][0])
        result: dict | None = None
        if best_distance > self.confidence_threshold:
            logger.debug(
                "Prediction for '%s' below confidence threshold (best=%.3f > %.3f)",
                dep_name,
                best_distance,
                self.confidence_threshold,
            )
        else:
            best_srpm = self._srpm_names[int(indices[0][0])]
            # Among the top-K neighbors of the same SRPM pick the canonical RPM
            # (prefer python3- over python3.13-; shorter is better).
            in_srpm = [
                (self._rpm_names[int(i)], float(d))
                for i, d in zip(indices[0], distances[0], strict=True)
                if self._srpm_names[int(i)] == best_srpm and float(d) <= self.confidence_threshold
            ]
            in_srpm.sort(key=lambda t: (_canonical_score(t[0]), t[1]))
            result = {"rpm_name": in_srpm[0][0], "srpm_name": best_srpm}

        self._cache[dep_name] = result
        return result

    def save(self, path: str) -> None:
        """
        Save the trained model as a compressed .npz archive.

        Raises:
            VibeBuildError: If scikit-learn is not installed or no model is trained.
        """
        _require_sklearn()
        import numpy as np

        if not self._model_loaded:
            raise VibeBuildError("No model to save. Train or load a model first.")

        vocabulary = self._vectorizer.vocabulary_
        terms = sorted(vocabulary, key=vocabulary.__getitem__)
        output_path = Path(path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "wb") as f:
            np.savez_compressed(
                f,
                format=np.array(MODEL_FORMAT),
                ngram_range=np.array(_NGRAM_RANGE),
                confidence_threshold=np.array(self.confidence_threshold),
                provides=np.array(self._provides, dtype=str),
                rpm_names=np.array(self._rpm_names, dtype=str),
                srpm_names=np.array(self._srpm_names, dtype=str),
                terms=np.array(terms, dtype=str),
                idf=self._vectorizer.idf_,
            )
        logger.info("Model saved to %s", path)

    def load(self, path: str) -> None:
        """
        Load a model saved by `save`.

        Raises:
            VibeBuildError: If scikit-learn is missing or the file is not a usable model.
        """
        _require_sklearn()
        import numpy as np
        from sklearn.feature_extraction.text import TfidfVectorizer

        model_path = Path(path)
        if not model_path.exists():
            raise VibeBuildError(f"Model file not found: {path}")

        try:
            with np.load(model_path, allow_pickle=False) as model:
                if int(model["format"]) != MODEL_FORMAT:
                    raise VibeBuildError(f"Unsupported model format {int(model['format'])}")
                ngram_range = tuple(int(n) for n in model["ngram_range"])
                threshold = float(model["confidence_threshold"])
                provides = model["provides"].tolist()
                rpm_names = model["rpm_names"].tolist()
                srpm_names = model["srpm_names"].tolist()
                terms = model["terms"].tolist()
                idf = model["idf"]
        except (OSError, KeyError, ValueError) as exc:
            raise VibeBuildError(f"Cannot read model {path}: {exc}") from exc

        if not provides or not (len(provides) == len(rpm_names) == len(srpm_names)):
            raise VibeBuildError(f"Inconsistent model {path}: row counts differ")
        if len(terms) != len(idf):
            raise VibeBuildError(f"Inconsistent model {path}: vocabulary size differs")

        vectorizer = TfidfVectorizer(
            analyzer="char_wb",
            ngram_range=ngram_range,
            vocabulary={term: i for i, term in enumerate(terms)},
        )
        vectorizer.idf_ = idf

        self._vectorizer = vectorizer
        self._matrix = vectorizer.transform(provides)
        self._provides = provides
        self._rpm_names = rpm_names
        self._srpm_names = srpm_names
        self.confidence_threshold = threshold
        self._fit_neighbors()
        self._cache.clear()
        self._model_loaded = True
        logger.info("Model loaded from %s (%d entries)", path, len(self._provides))
