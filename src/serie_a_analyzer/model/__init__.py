"""Motore statistico: stima delle forze, matrice dei risultati, mercati derivati."""

from .dixon_coles import FittedModel, Match, fit_dixon_coles
from .markets import MatchProbabilities, derive_markets
from .scoreline import scoreline_matrix

__all__ = [
    "Match",
    "FittedModel",
    "fit_dixon_coles",
    "scoreline_matrix",
    "MatchProbabilities",
    "derive_markets",
]
