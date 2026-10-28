import asyncio
import logging
import re
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.services.accusation_triggers import (
    ACCUSATION_VERBS,
    COMMIT_VERBS,
    HARM_NOUNS,
    NEGATIVE_ACTION_VERBS,
    NEGATIVE_ADVERBS,
    NEGATIVE_DESCRIPTOR_ADJECTIVES,
    NEGATIVE_DESCRIPTOR_NOUNS,
    PERSON_ATTRIBUTE_NOUNS,
    TARGETED_INSULT_ADJECTIVES,
    TARGETED_INSULT_NOUNS,
)
from app.services.profanity_words import (
    contains_explicit_sexual,
    contains_profanity,
    is_spam_text,
)

logger = logging.getLogger(__name__)

_LABEL_VERBS = {
    "считать",
    "называть",
    "назвать",
    "признавать",
    "признать",
    "объявлять",
    "объявить",
    "выставлять",
    "выставить",
}

_AUXILIARY_LEMMAS = {
    "быть",
    "мочь",
    "хотеть",
    "начинать",
    "начать",
    "продолжать",
    "стать",
    "собираться",
    "уметь",
}

_LINK_LEMMAS = _AUXILIARY_LEMMAS | {
    "это",
    "такой",
    "самый",
    "очень",
    "просто",
    "настоящий",
    "оказаться",
    "являться",
    "казаться",
    "выглядеть",
}

_POS_MAP = {
    "NOUN": {"NOUN"},
    "PROPN": {"NOUN"},
    "VERB": {"VERB", "INFN", "PRTF", "PRTS", "GRND"},
    "AUX": {"VERB", "INFN"},
    "ADJ": {"ADJF", "ADJS", "PRTF", "PRTS", "COMP"},
    "ADV": {"ADVB", "COMP"},
    "PRON": {"NPRO"},
    "DET": {"ADJF", "NPRO"},
    "NUM": {"NUMR"},
}


class Verdict(str, Enum):
    APPROVE = "approve"
    REJECT_PROFANITY = "reject_profanity"
    REJECT_SPAM = "reject_spam"
    REJECT_TARGETED_INSULT = "reject_targeted_insult"
    MANUAL_PERSON_NEGATIVE = "manual_person_negative"


@dataclass(frozen=True)
class ModerationResult:
    verdict: Verdict

    @property
    def approved(self) -> bool:
        return self.verdict is Verdict.APPROVE

    @property
    def manual_review(self) -> bool:
        return self.verdict is Verdict.MANUAL_PERSON_NEGATIVE


@dataclass
class _SentenceInfo:
    tokens: list[Any]
    lexical: list[Any]
    by_id: dict[str, Any]
    children: dict[str, list[Any]]
    lemmas: dict[str, set[str]]
    proper: dict[str, bool]
    proper_primary: dict[str, bool]
    order: dict[str, int] = field(default_factory=dict)


class ModerationEngine:
    def __init__(self) -> None:
        self._doc_type: Any = None
        self._segmenter: Any = None
        self._morph_tagger: Any = None
        self._syntax_parser: Any = None
        self._ner_tagger: Any = None
        self._morph: Any = None
        self._load_lock = threading.Lock()
        self._check_lock = threading.Lock()

    def _ensure_loaded(self) -> None:
        if self._segmenter is not None:
            return
        with self._load_lock:
            if self._segmenter is not None:
                return
            import pymorphy3
            from natasha import (
                Doc,
                NewsEmbedding,
                NewsMorphTagger,
                NewsNERTagger,
                NewsSyntaxParser,
                Segmenter,
            )

            segmenter = Segmenter()
            embedding = NewsEmbedding()
            morph_tagger = NewsMorphTagger(embedding)
            syntax_parser = NewsSyntaxParser(embedding)
            ner_tagger = NewsNERTagger(embedding)
            morph = pymorphy3.MorphAnalyzer()
            self._doc_type = Doc
            self._segmenter = segmenter
            self._morph_tagger = morph_tagger
            self._syntax_parser = syntax_parser
            self._ner_tagger = ner_tagger
            self._morph = morph
            logger.info("ModerationEngine: модели natasha/pymorphy3 загружены")

    @staticmethod
    def _normalize_lemma(value: str) -> str:
        return value.casefold().replace("ё", "е")

    def _token_lemmas(self, token: Any) -> tuple[set[str], bool, bool]:
        text = str(getattr(token, "text", ""))
        if not any(ch.isalpha() for ch in text):
            return {self._normalize_lemma(text)}, False, False
        parses = self._morph.parse(text)
        collapsed = re.sub(
            r"([^\W\d_])\1+", r"\1", text, flags=re.UNICODE
        )
        collapsed_parses = []
        if collapsed != text:
            collapsed_parses = self._morph.parse(collapsed)
            parses = [*parses, *collapsed_parses]
        proper = any(
            parse.score >= 0.1
            and bool({"Name", "Surn", "Patr"} & set(parse.tag.grammemes))
            for parse in parses
        )
        proper_primary = bool(
            parses
            and {"Name", "Surn", "Patr"} & set(parses[0].tag.grammemes)
        )
        expected = _POS_MAP.get(str(getattr(token, "pos", "")), set())
        lemmas = {
            self._normalize_lemma(parse.normal_form)
            for parse in parses
            if not expected or parse.tag.POS in expected
        }
        if collapsed_parses:
            lemmas.update(
                self._normalize_lemma(parse.normal_form)
                for parse in collapsed_parses
            )
        if not lemmas and parses:
            lemmas = {self._normalize_lemma(parses[0].normal_form)}
        if not lemmas:
            lemmas = {self._normalize_lemma(text)}
        return lemmas, proper, proper_primary

    def _build_sentence(self, sent: Any) -> _SentenceInfo:
        tokens = list(sent.tokens or [])
        lexical = [token for token in tokens if getattr(token, "pos", None) != "PUNCT"]
        by_id = {str(token.id): token for token in tokens}
        children: dict[str, list[Any]] = {str(token.id): [] for token in tokens}
        for token in tokens:
            head_id = str(getattr(token, "head_id", ""))
            if head_id in children and head_id != str(token.id):
                children[head_id].append(token)
        lemmas: dict[str, set[str]] = {}
        proper: dict[str, bool] = {}
        proper_primary: dict[str, bool] = {}
        cache: dict[tuple[str, str], tuple[set[str], bool, bool]] = {}
        for token in tokens:
            cache_key = (str(token.text), str(getattr(token, "pos", "")))
            if cache_key not in cache:
                cache[cache_key] = self._token_lemmas(token)
            token_lemmas, token_proper, token_proper_primary = cache[cache_key]
            lemmas[str(token.id)] = set(token_lemmas)
            proper[str(token.id)] = token_proper
            proper_primary[str(token.id)] = token_proper_primary
        info = _SentenceInfo(
            tokens=tokens,
            lexical=lexical,
            by_id=by_id,
            children=children,
            lemmas=lemmas,
            proper=proper,
            proper_primary=proper_primary,
        )
        info.order = {str(token.id): index for index, token in enumerate(lexical)}
        return info

    @staticmethod
    def _has_lemma(info: _SentenceInfo, token: Any, lexicon: set[str]) -> bool:
        return bool(info.lemmas.get(str(token.id), set()) & lexicon)

    @staticmethod
    def _is_negated(info: _SentenceInfo, token: Any) -> bool:
        for child in info.children.get(str(token.id), []):
            if str(child.text).casefold() == "не":
                return True
        index = info.order.get(str(token.id))
        if index is None or index == 0:
            return False
        previous = info.lexical[index - 1]
        return str(previous.text).casefold() == "не"

    @staticmethod
    def _descendants(
        info: _SentenceInfo,
        root: Any,
        rels: set[str] | None = None,
        include_self: bool = False,
    ) -> list[Any]:
        result = [root] if include_self else []
        seen = {str(root.id)}
        queue = [root]
        while queue:
            current = queue.pop(0)
            for child in info.children.get(str(current.id), []):
                child_id = str(child.id)
                if child_id in seen:
                    continue
                if rels is not None and str(getattr(child, "rel", "")) not in rels:
                    continue
                seen.add(child_id)
                result.append(child)
                queue.append(child)
        return result

    @classmethod
    def _descriptor_phrase_has_negative(
        cls, info: _SentenceInfo, root: Any
    ) -> bool:
        candidates = [root]
        candidates.extend(
            cls._descendants(info, root, rels={"amod", "conj", "advmod"})
        )
        return any(
            not cls._is_negated(info, token)
            and (
                cls._has_lemma(info, token, NEGATIVE_DESCRIPTOR_NOUNS)
                or cls._has_lemma(info, token, NEGATIVE_DESCRIPTOR_ADJECTIVES)
            )
            for token in candidates
        )

    @classmethod
    def _predicate_tokens(
        cls, info: _SentenceInfo, root: Any, person_number: str | None
    ) -> list[Any]:
        result = [root]
        seen = {str(root.id)}
        queue = [root]
        allowed = {"conj", "xcomp", "advcl", "advmod"}
        while queue:
            current = queue.pop(0)
            for child in info.children.get(str(current.id), []):
                child_id = str(child.id)
                rel = str(getattr(child, "rel", ""))
                if child_id in seen or rel not in allowed:
                    continue
                if rel == "conj" and str(getattr(child, "pos", "")) in {"VERB", "AUX"}:
                    child_number = (getattr(child, "feats", {}) or {}).get("Number")
                    if (
                        person_number is not None
                        and child_number is not None
                        and child_number != person_number
                    ):
                        continue
                seen.add(child_id)
                result.append(child)
                queue.append(child)
        return result

    @classmethod
    def _predicate_has_negative(
        cls,
        info: _SentenceInfo,
        predicate: Any,
        person_number: str | None,
        passive_subject: bool = False,
    ) -> bool:
        predicate_tokens = cls._predicate_tokens(info, predicate, person_number)
        negated_ids = {
            str(token.id)
            for token in predicate_tokens
            if cls._is_negated(info, token)
        }
        for token in predicate_tokens:
            if str(token.id) not in negated_ids:
                continue
            for descendant in cls._descendants(info, token, rels={"xcomp"}):
                negated_ids.add(str(descendant.id))
        for token in predicate_tokens:
            if str(token.id) in negated_ids:
                continue
            pos = str(getattr(token, "pos", ""))
            if pos in {"NOUN", "ADJ", "PROPN"} and cls._descriptor_phrase_has_negative(
                info, token
            ):
                return True
            if pos not in {"VERB", "AUX"}:
                continue
            if cls._has_lemma(info, token, NEGATIVE_ACTION_VERBS):
                return True
            if passive_subject and cls._has_lemma(info, token, ACCUSATION_VERBS):
                return True
            for child in info.children.get(str(token.id), []):
                rel = str(getattr(child, "rel", ""))
                if cls._is_negated(info, child):
                    continue
                if rel == "advmod" and cls._has_lemma(info, child, NEGATIVE_ADVERBS):
                    return True
                if rel == "xcomp" and cls._descriptor_phrase_has_negative(info, child):
                    return True
            if cls._has_lemma(info, token, COMMIT_VERBS):
                for child in info.children.get(str(token.id), []):
                    if str(getattr(child, "rel", "")) not in {"obj", "obl", "xcomp"}:
                        continue
                    subtree = cls._descendants(info, child, include_self=True)
                    if any(cls._has_lemma(info, item, HARM_NOUNS) for item in subtree):
                        return True
            if passive_subject:
                for child in info.children.get(str(token.id), []):
                    if str(getattr(child, "rel", "")) not in {"obj", "obl", "xcomp"}:
                        continue
                    subtree = cls._descendants(info, child, include_self=True)
                    if any(cls._has_lemma(info, item, HARM_NOUNS) for item in subtree):
                        return True
        return False

    @classmethod
    def _subject_has_negative(
        cls, info: _SentenceInfo, person_head: Any
    ) -> bool:
        rel = str(getattr(person_head, "rel", ""))
        if not rel.startswith("nsubj"):
            return False
        predicate = info.by_id.get(str(getattr(person_head, "head_id", "")))
        if predicate is None:
            return False
        person_number = (getattr(person_head, "feats", {}) or {}).get("Number")
        return cls._predicate_has_negative(
            info,
            predicate,
            str(person_number) if person_number is not None else None,
            passive_subject=rel.startswith("nsubj:pass"),
        )

    @classmethod
    def _object_has_negative(
        cls, info: _SentenceInfo, person_head: Any
    ) -> bool:
        rel = str(getattr(person_head, "rel", ""))
        if rel not in {"obj", "iobj", "obl", "obl:agent"}:
            return False
        predicate = info.by_id.get(str(getattr(person_head, "head_id", "")))
        if predicate is None or cls._is_negated(info, predicate):
            return False
        if rel == "obl:agent" and cls._has_lemma(
            info, predicate, NEGATIVE_ACTION_VERBS
        ):
            return True
        if cls._has_lemma(info, predicate, ACCUSATION_VERBS):
            return True
        if cls._has_lemma(info, predicate, _LABEL_VERBS):
            for child in info.children.get(str(predicate.id), []):
                if str(getattr(child, "rel", "")) == "xcomp" and (
                    cls._descriptor_phrase_has_negative(info, child)
                ):
                    return True
        prepositions = {
            str(child.text).casefold()
            for child in info.children.get(str(person_head.id), [])
            if str(getattr(child, "rel", "")) == "case"
        }
        if "против" in prepositions:
            subtree = cls._descendants(info, predicate, include_self=True)
            return any(cls._has_lemma(info, item, HARM_NOUNS) for item in subtree)
        return False

    @classmethod
    def _attribute_has_negative(
        cls, info: _SentenceInfo, person_head: Any
    ) -> bool:
        if str(getattr(person_head, "rel", "")) != "nmod":
            return False
        attribute = info.by_id.get(str(getattr(person_head, "head_id", "")))
        if attribute is None:
            return False
        if cls._has_lemma(info, attribute, HARM_NOUNS):
            return True
        if not cls._has_lemma(info, attribute, PERSON_ATTRIBUTE_NOUNS):
            return False
        if cls._descriptor_phrase_has_negative(info, attribute):
            return True
        if str(getattr(attribute, "rel", "")).startswith("nsubj"):
            predicate = info.by_id.get(str(getattr(attribute, "head_id", "")))
            return predicate is not None and cls._descriptor_phrase_has_negative(
                info, predicate
            )
        return False

    @classmethod
    def _relative_clause_has_negative(
        cls, info: _SentenceInfo, person_tokens: list[Any]
    ) -> bool:
        for token in person_tokens:
            person_number = (getattr(token, "feats", {}) or {}).get("Number")
            for child in info.children.get(str(token.id), []):
                if not str(getattr(child, "rel", "")).startswith("acl"):
                    continue
                if cls._predicate_has_negative(
                    info,
                    child,
                    str(person_number) if person_number is not None else None,
                ):
                    return True
        return False

    @classmethod
    def _direct_modifier_has_negative(
        cls, info: _SentenceInfo, person_head: Any, person_tokens: list[Any]
    ) -> bool:
        for token in person_tokens:
            for child in info.children.get(str(token.id), []):
                rel = str(getattr(child, "rel", ""))
                if rel in {"amod", "appos"} and cls._descriptor_phrase_has_negative(
                    info, child
                ):
                    return True
        if str(getattr(person_head, "rel", "")) in {"amod", "appos"}:
            head = info.by_id.get(str(getattr(person_head, "head_id", "")))
            return head is not None and cls._descriptor_phrase_has_negative(info, head)
        return False

    @classmethod
    def _fallback_has_negative(
        cls, info: _SentenceInfo, person_tokens: list[Any]
    ) -> bool:
        orders = [info.order[str(token.id)] for token in person_tokens if str(token.id) in info.order]
        if not orders:
            return False
        start = min(orders)
        end = max(orders)
        person_head = person_tokens[0]
        person_case = (getattr(person_head, "feats", {}) or {}).get("Case")
        for signal in info.lexical:
            signal_order = info.order[str(signal.id)]
            if start <= signal_order <= end or cls._is_negated(info, signal):
                continue
            lower = min(end, signal_order)
            upper = max(start, signal_order)
            negated_between = any(
                str(token.text).casefold() == "не"
                for token in info.lexical[lower + 1 : upper]
            )
            if not negated_between and (
                cls._has_lemma(info, signal, NEGATIVE_DESCRIPTOR_NOUNS)
                or cls._has_lemma(
                    info, signal, NEGATIVE_DESCRIPTOR_ADJECTIVES
                )
            ):
                if signal_order == end + 1 or signal_order == start - 1:
                    return True
                if (
                    str(getattr(signal, "pos", "")) == "ADJ"
                    and signal_order == len(info.lexical) - 1
                    and end < signal_order
                ):
                    return True
                if end < signal_order <= end + 4:
                    between = info.lexical[end + 1 : signal_order]
                    if all(
                        str(getattr(token, "pos", "")) in {"ADV", "DET", "AUX", "PART"}
                        or bool(info.lemmas.get(str(token.id), set()) & _LINK_LEMMAS)
                        for token in between
                    ):
                        return True
                if start - 3 <= signal_order < start:
                    between = info.lexical[signal_order + 1 : start]
                    if all(
                        str(getattr(token, "pos", "")) in {"ADV", "DET", "AUX", "PART"}
                        or bool(info.lemmas.get(str(token.id), set()) & _LINK_LEMMAS)
                        for token in between
                    ):
                        return True
            if (
                signal_order > end
                and signal_order - end <= 6
                and person_case in {None, "Nom"}
                and cls._has_lemma(info, signal, NEGATIVE_ACTION_VERBS)
            ):
                between = info.lexical[end + 1 : signal_order]
                if any(str(token.text).casefold() == "не" for token in between):
                    continue
                if not any(
                    str(getattr(token, "pos", "")) in {"VERB", "AUX"}
                    and not bool(info.lemmas.get(str(token.id), set()) & _AUXILIARY_LEMMAS)
                    for token in between
                ):
                    return True
            if (
                abs(signal_order - start) <= 5
                and abs(signal_order - end) <= 5
                and person_case not in {None, "Nom"}
                and cls._has_lemma(info, signal, ACCUSATION_VERBS)
            ):
                return True
            if end < signal_order <= end + 3 and cls._has_lemma(
                info, signal, PERSON_ATTRIBUTE_NOUNS
            ):
                for child in info.children.get(str(signal.id), []):
                    if str(getattr(child, "rel", "")) == "amod" and (
                        cls._descriptor_phrase_has_negative(info, child)
                    ):
                        return True
                signal_index = signal_order
                if signal_index > 0:
                    previous = info.lexical[signal_index - 1]
                    if cls._descriptor_phrase_has_negative(info, previous):
                        return True
        return False

    @classmethod
    def _person_has_negative(
        cls, info: _SentenceInfo, person_tokens: list[Any]
    ) -> bool:
        if not person_tokens:
            return False
        person_ids = {str(token.id) for token in person_tokens}
        person_head = next(
            (
                token
                for token in person_tokens
                if str(getattr(token, "head_id", "")) not in person_ids
            ),
            person_tokens[0],
        )
        return (
            cls._direct_modifier_has_negative(info, person_head, person_tokens)
            or cls._subject_has_negative(info, person_head)
            or cls._object_has_negative(info, person_head)
            or cls._attribute_has_negative(info, person_head)
            or cls._relative_clause_has_negative(info, person_tokens)
            or cls._fallback_has_negative(info, person_tokens)
        )

    @classmethod
    def _descriptor_phrase_has_targeted_insult(
        cls, info: _SentenceInfo, root: Any
    ) -> bool:
        candidates = [root]
        candidates.extend(
            cls._descendants(info, root, rels={"amod", "conj", "advmod"})
        )
        return any(
            not cls._is_negated(info, token)
            and (
                cls._has_lemma(info, token, TARGETED_INSULT_NOUNS)
                or cls._has_lemma(info, token, TARGETED_INSULT_ADJECTIVES)
            )
            for token in candidates
        )

    @staticmethod
    def _features_agree(first: Any, second: Any) -> bool:
        for feature in ("Gender", "Number", "Case"):
            first_value = (getattr(first, "feats", {}) or {}).get(feature)
            second_value = (getattr(second, "feats", {}) or {}).get(feature)
            if first_value and second_value and first_value != second_value:
                return False
        return True

    @classmethod
    def _person_has_targeted_insult(
        cls, info: _SentenceInfo, person_tokens: list[Any]
    ) -> bool:
        if not person_tokens:
            return False
        person_ids = {str(token.id) for token in person_tokens}
        person_head = next(
            (
                token
                for token in person_tokens
                if str(getattr(token, "head_id", "")) not in person_ids
            ),
            person_tokens[0],
        )
        for token in person_tokens:
            for child in info.children.get(str(token.id), []):
                if str(getattr(child, "rel", "")) in {"amod", "appos"} and (
                    cls._descriptor_phrase_has_targeted_insult(info, child)
                ):
                    return True
        if str(getattr(person_head, "rel", "")) in {"amod", "appos"}:
            head = info.by_id.get(str(getattr(person_head, "head_id", "")))
            if head is not None and cls._descriptor_phrase_has_targeted_insult(
                info, head
            ):
                return True
        rel = str(getattr(person_head, "rel", ""))
        predicate = info.by_id.get(str(getattr(person_head, "head_id", "")))
        if predicate is not None and rel.startswith("nsubj"):
            person_number = (getattr(person_head, "feats", {}) or {}).get(
                "Number"
            )
            predicate_tokens = cls._predicate_tokens(
                info,
                predicate,
                str(person_number) if person_number is not None else None,
            )
            negated_ids = {
                str(token.id)
                for token in predicate_tokens
                if cls._is_negated(info, token)
            }
            for token in list(predicate_tokens):
                if str(token.id) not in negated_ids:
                    continue
                for descendant in cls._descendants(
                    info, token, rels={"xcomp"}
                ):
                    negated_ids.add(str(descendant.id))
            for token in predicate_tokens:
                if str(token.id) in negated_ids:
                    continue
                if str(getattr(token, "pos", "")) in {
                    "NOUN",
                    "ADJ",
                    "PROPN",
                } and cls._descriptor_phrase_has_targeted_insult(info, token):
                    return True
                for child in info.children.get(str(token.id), []):
                    if str(getattr(child, "rel", "")) == "xcomp" and (
                        cls._descriptor_phrase_has_targeted_insult(info, child)
                    ):
                        return True
        if (
            predicate is not None
            and rel in {"obj", "iobj", "obl"}
            and not cls._is_negated(info, predicate)
            and cls._has_lemma(info, predicate, _LABEL_VERBS)
        ):
            for child in info.children.get(str(predicate.id), []):
                if str(getattr(child, "rel", "")) == "xcomp" and (
                    cls._descriptor_phrase_has_targeted_insult(info, child)
                ):
                    return True
        orders = [
            info.order[str(token.id)]
            for token in person_tokens
            if str(token.id) in info.order
        ]
        if not orders:
            return False
        start = min(orders)
        end = max(orders)
        for signal in info.lexical:
            signal_order = info.order[str(signal.id)]
            if start <= signal_order <= end or cls._is_negated(info, signal):
                continue
            if not (
                cls._has_lemma(info, signal, TARGETED_INSULT_NOUNS)
                or cls._has_lemma(info, signal, TARGETED_INSULT_ADJECTIVES)
            ):
                continue
            lower = min(end, signal_order)
            upper = max(start, signal_order)
            if any(
                str(token.text).casefold() == "не"
                for token in info.lexical[lower + 1 : upper]
            ):
                continue
            signal_head = info.by_id.get(str(getattr(signal, "head_id", "")))
            if (
                str(getattr(signal, "pos", "")) == "ADJ"
                and signal_head is not None
                and str(signal_head.id) not in person_ids
                and info.order.get(str(signal_head.id), -1) > signal_order
            ):
                continue
            if signal_order == end + 1 or signal_order == start - 1:
                return True
            if (
                str(getattr(signal, "pos", "")) == "ADJ"
                and signal_order == len(info.lexical) - 1
                and end < signal_order
                and cls._features_agree(person_head, signal)
            ):
                return True
            if end < signal_order <= end + 4:
                between = info.lexical[end + 1 : signal_order]
                if all(
                    str(getattr(token, "pos", "")) in {"ADV", "DET", "AUX", "PART"}
                    or bool(info.lemmas.get(str(token.id), set()) & _LINK_LEMMAS)
                    for token in between
                ):
                    return True
            if start - 3 <= signal_order < start:
                between = info.lexical[signal_order + 1 : start]
                if all(
                    str(getattr(token, "pos", "")) in {"ADV", "DET", "AUX", "PART"}
                    or bool(info.lemmas.get(str(token.id), set()) & _LINK_LEMMAS)
                    for token in between
                ):
                    return True
        return False

    @classmethod
    def _sentence_has_lone_targeted_insult(
        cls, info: _SentenceInfo, person_feats: dict[str, Any]
    ) -> bool:
        if not info.lexical or len(info.lexical) > 3:
            return False
        if any(
            str(token.text).casefold() == "не" for token in info.lexical
        ):
            return False
        targets = [
            token
            for token in info.lexical
            if cls._has_lemma(info, token, TARGETED_INSULT_NOUNS)
            or cls._has_lemma(info, token, TARGETED_INSULT_ADJECTIVES)
        ]
        if len(targets) != 1:
            return False
        target = targets[0]
        others = [token for token in info.lexical if token is not target]
        if not all(
            str(getattr(token, "pos", "")) in {"ADV", "DET", "PART"}
            or bool(info.lemmas.get(str(token.id), set()) & _LINK_LEMMAS)
            for token in others
        ):
            return False
        if str(getattr(target, "pos", "")) == "ADJ":
            target_feats = getattr(target, "feats", {}) or {}
            for feature in ("Gender", "Number"):
                expected = person_feats.get(feature)
                actual = target_feats.get(feature)
                if expected and actual and expected != actual:
                    return False
        return True

    @staticmethod
    def _proper_name_groups(info: _SentenceInfo) -> list[list[Any]]:
        groups: list[list[Any]] = []
        current: list[Any] = []
        for token in info.lexical:
            if info.proper.get(str(token.id), False):
                current.append(token)
                continue
            if current:
                groups.append(current[:3])
                current = []
        if current:
            groups.append(current[:3])
        return [
            group
            for group in groups
            if len(group) >= 2
            or str(group[0].text)[:1].isupper()
            or info.proper_primary.get(str(group[0].id), False)
        ]

    def _check_sync(self, text: str) -> ModerationResult:
        if not text.strip():
            return ModerationResult(Verdict.APPROVE)
        if contains_profanity(text) or contains_explicit_sexual(text):
            return ModerationResult(Verdict.REJECT_PROFANITY)
        if is_spam_text(text):
            return ModerationResult(Verdict.REJECT_SPAM)

        with self._check_lock:
            self._ensure_loaded()
            doc = self._doc_type(text)
            doc.segment(self._segmenter)
            doc.tag_morph(self._morph_tagger)
            doc.parse_syntax(self._syntax_parser)
            doc.tag_ner(self._ner_tagger)
            person_spans = [
                span for span in (doc.spans or []) if getattr(span, "type", None) == "PER"
            ]
            previous_person_groups = 0
            previous_person_features: dict[str, Any] | None = None
            for sent in doc.sents or []:
                info = self._build_sentence(sent)
                groups: list[list[Any]] = []
                used_token_ids: set[str] = set()
                sent_start = getattr(sent, "start", None)
                sent_stop = getattr(sent, "stop", None)
                for span in person_spans:
                    if sent_start is not None and not (
                        sent_start <= span.start and span.stop <= sent_stop
                    ):
                        continue
                    group = [
                        token
                        for token in info.lexical
                        if span.start <= token.start and token.stop <= span.stop
                    ]
                    if group:
                        groups.append(group)
                        used_token_ids.update(str(token.id) for token in group)
                for group in self._proper_name_groups(info):
                    if any(str(token.id) in used_token_ids for token in group):
                        continue
                    groups.append(group)
                    used_token_ids.update(str(token.id) for token in group)
                if any(
                    self._person_has_targeted_insult(info, group) for group in groups
                ):
                    return ModerationResult(Verdict.REJECT_TARGETED_INSULT)
                if any(self._person_has_negative(info, group) for group in groups):
                    return ModerationResult(Verdict.MANUAL_PERSON_NEGATIVE)
                if previous_person_groups == 1:
                    if previous_person_features is not None and (
                        self._sentence_has_lone_targeted_insult(
                            info, previous_person_features
                        )
                    ):
                        return ModerationResult(
                            Verdict.REJECT_TARGETED_INSULT
                        )
                    for token in info.lexical:
                        if str(getattr(token, "pos", "")) != "PRON":
                            continue
                        if not (info.lemmas.get(str(token.id), set()) & {"он", "она", "они"}):
                            continue
                        if self._person_has_targeted_insult(info, [token]):
                            return ModerationResult(
                                Verdict.REJECT_TARGETED_INSULT
                            )
                        if self._person_has_negative(info, [token]):
                            return ModerationResult(Verdict.MANUAL_PERSON_NEGATIVE)
                if previous_person_groups == 0 and len(groups) == 1:
                    previous_person_features = dict(
                        getattr(groups[0][0], "feats", {}) or {}
                    )
                elif groups:
                    previous_person_features = None
                previous_person_groups += len(groups)

        return ModerationResult(Verdict.APPROVE)

    async def check(self, text: str) -> ModerationResult:
        return await asyncio.to_thread(self._check_sync, text)
