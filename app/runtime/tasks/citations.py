"""Propose source links from distinctive anchors; the verifier still proves claims."""
import re
from collections import Counter


class SourceCitationResolver:
    policy_version = 'distinctive-source-anchors-v1'

    @staticmethod
    def tokens(text):
        return set(re.findall(r'[a-z][a-z0-9_]{2,}|\b\d{4}\b', text.casefold()))

    def resolve(self, task, draft, allowed):
        sources = {key: self.tokens(value.content) for key, value in task.evidence.items() if key in allowed}
        frequency = Counter(token for tokens in sources.values() for token in tokens)
        threshold = max(1, len(sources) // 6)
        for section in draft.sections:
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
                missing -= sources[key]
