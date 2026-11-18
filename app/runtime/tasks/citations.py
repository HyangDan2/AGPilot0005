"""Propose source links from distinctive anchors; the verifier still proves claims."""
import json
import re
from collections import Counter


class SourceCitationResolver:
    policy_version = 'distinctive-source-anchors-v1'

    @staticmethod
    def tokens(text):
        return set(re.findall(r'[a-z][a-z0-9_]{2,}|\b\d{4}\b', text.casefold()))

    @staticmethod
    def _broad_tokens(text):
        return set(re.findall(r'[0-9A-Za-z가-힣_]{2,}', text.casefold()))

    def resolve(self, task, draft, allowed, max_json_chars=None):
        sources = {key: self.tokens(value.content) for key, value in task.evidence.items() if key in allowed}
        broad_sources = {key: self._broad_tokens(value.content) for key, value in task.evidence.items() if key in allowed}
        frequency = Counter(token for tokens in sources.values() for token in tokens)
        threshold = max(1, len(sources) // 6)
        allowed_list = sorted(allowed)
        for section in draft.sections:
            if section.refs and allowed:
                normalized = []
                for ref in section.refs:
                    if ref in allowed:
                        normalized.append(ref)
                    elif '/' not in ref:
                        matches = [k for k in allowed_list if k.endswith('/' + ref)]
                        normalized.append(matches[0] if len(matches) == 1 else ref)
                    else:
                        normalized.append(ref)
                section.refs = list(dict.fromkeys(normalized))
            if not section.refs and allowed_list:
                sec_tokens = self._broad_tokens(section.heading + ' ' + section.body)
                scored = sorted(
                    ((len(sec_tokens & broad_sources.get(k, set())), k) for k in allowed_list),
                    key=lambda item: (-item[0], item[1]),
                )
                best = [k for score, k in scored if score > 0][:2]
                if not best:
                    sibling_refs = [r for s in draft.sections for r in s.refs if r in allowed]
                    best = [sibling_refs[0]] if sibling_refs else [allowed_list[0]]
                for candidate in best:
                    section.refs.append(candidate)
                    if max_json_chars is not None and len(json.dumps(draft.model_dump(), ensure_ascii=False)) > max_json_chars:
                        section.refs.pop()
                        break
            if not section.refs or any(ref not in allowed for ref in section.refs):
                continue
            anchors = {token for token in self.tokens(section.body) if 0 < frequency[token] <= threshold}
            covered = set().union(*(sources.get(key, set()) for key in section.refs))
            missing = anchors - covered
            for _ in range(3):
                candidates = [(len(tokens & missing), key, tokens & missing) for key, tokens in sources.items()
                              if key not in section.refs]
                if not candidates:
                    break
                count, key, matches = max(candidates, key=lambda item: (item[0], item[1]))
                if count < 2 and not any(token.isdigit() or len(token) >= 14 for token in matches):
                    break
                section.refs.append(key)
                if max_json_chars is not None and len(json.dumps(draft.model_dump(), ensure_ascii=False)) > max_json_chars:
                    section.refs.pop()
                    break
                missing -= sources[key]
