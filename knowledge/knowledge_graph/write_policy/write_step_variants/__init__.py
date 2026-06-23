"""Concrete write-policy steps."""

from knowledge.knowledge_graph.write_policy.write_step_variants.aspect_tagger import (
    AspectJudge,
    AspectTagger,
)
from knowledge.knowledge_graph.write_policy.write_step_variants.conflict_flagger import (
    ConflictFlagger,
)
from knowledge.knowledge_graph.write_policy.write_step_variants.conflict_judge import (
    ConflictJudge,
)
from knowledge.knowledge_graph.write_policy.write_step_variants.conflict_overwriter import (
    ConflictOverwriter,
)
from knowledge.knowledge_graph.write_policy.write_step_variants.deduper import Deduper
from knowledge.knowledge_graph.write_policy.write_step_variants.merge_judge import MergeJudge
from knowledge.knowledge_graph.write_policy.write_step_variants.redactor import Redactor

__all__ = [
    "Redactor",
    "Deduper",
    "MergeJudge",
    "ConflictFlagger",
    "ConflictJudge",
    "ConflictOverwriter",
    "AspectTagger",
    "AspectJudge",
]
