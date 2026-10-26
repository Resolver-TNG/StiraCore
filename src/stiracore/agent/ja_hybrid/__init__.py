"""Opt-in Japanese hybrid stimulus classifier (ML-Ask + discourse patterns, #164).

Importing this package does not import ``mlask_official``; the emotion-word
layer is loaded on first use. See docs/ja-rules-classifier.md.
"""

from stiracore.agent.ja_hybrid.classifier import (
    JaHybridClassifier,
    JaHybridVerdict,
    mlask_available,
)

__all__ = ["JaHybridClassifier", "JaHybridVerdict", "mlask_available"]
