"""Human source labels; verification keeps the original source keys."""
import re


def legacy_citations(job):
    answer = job.get('answer')
    if not answer:
        return answer, []
    entries = {e['doc_id'] + '/' + e['chunk_id']: e for e in job.get('entries', [])}
    by_chunk = {}
    for e in job.get('entries', []):
        by_chunk.setdefault(e['chunk_id'], []).append(e)
    citations = []
    def replace(match):
        labels = {}
        last_doc = (job.get('document_ids') or [None])[0] if len(job.get('document_ids') or []) == 1 else None
        for doc_part, chk_part in re.findall(r'(?:(doc_[A-Za-z0-9_-]+)/)?((?:chk|sec)_[A-Za-z0-9_-]+)(?:#\d+/\d+)?', match.group()):
            doc_id = doc_part or last_doc
            if doc_part:
                last_doc = doc_part
            e = entries.get(f'{doc_id}/{chk_part}') if doc_id else None
            if not e and len(by_chunk.get(chk_part, [])) == 1:
                e = by_chunk[chk_part][0]
            if not e:
                continue
            key = f"{e['doc_id']}/{e['chunk_id']}"
            title = job.get('document_titles', {}).get(e['doc_id'], '첨부 문서')
            page = (e.get('source') or {}).get('page')
            labels.setdefault(title, set()).add(page)
            if not any(c['key'] == key for c in citations):
                citations.append({'key': key, 'doc_id': e['doc_id'], 'chunk_id': e['chunk_id'], 'title': title, 'page': page})
        if not labels:
            return ''
        return '; '.join(title + (' · p. ' + ', '.join(map(str, sorted(p for p in pages if isinstance(p,int))))
                                   if any(isinstance(p,int) for p in pages) else '') for title,pages in labels.items())
    cleaned = re.sub(r'\[(?:(?:doc_[A-Za-z0-9_-]+/)?(?:chk|sec)_[A-Za-z0-9_-]+(?:#\d+/\d+)?(?:\s*,\s*)?)+\]', replace, answer)
    cleaned = re.sub(r'\n*분석 범위:\s*\d+/\d+\s*청크\s*$', '', cleaned).strip()
    return cleaned, citations


def humanize_keys(text, citations):
    # Only replace keys verified against actual evidence; preserve unknown text.
    for c in sorted(citations, key=lambda c: len(c['key']), reverse=True):
        title = re.sub(r'([\\\[\]`*_<>])', r'\\\1', c['title'])
        label = title + (' · p. ' + str(c['page']) if c.get('page') else '')
        text = re.sub(r'\[?' + re.escape(c['key']) + r'\]?(?![A-Za-z0-9_])', lambda _: label, text)
    return text


class AnswerPresenter:
    @staticmethod
    def sections(task):
        if task.final_draft:
            return task.final_draft.sections
        return [s for draft in task.drafts.values() for s in draft.sections]

    def citations(self, task):
        result, seen = [], set()
        for section in self.sections(task):
            for key in section.refs:
                e = task.evidence.get(key)
                if not e or key in seen:
                    continue
                seen.add(key)
                result.append({'key': key, 'doc_id': e.doc_id, 'chunk_id': e.chunk_id,
                               'title': task.document_titles.get(e.doc_id, '첨부 문서'),
                               'page': e.source.get('page') or e.source.get('slide')})
        return result

    def render(self, task):
        labels = {c['key']: c for c in self.citations(task)}
        blocks = []
        for section in self.sections(task):
            refs = {}
            for key in section.refs:
                c = labels.get(key)
                if c:
                    refs.setdefault(c['title'], set()).add(c['page'])
            sources = []
            for title, pages in refs.items():
                title = re.sub(r'([\\\[\]`*_<>])', r'\\\1', title)
                numbers = sorted(p for p in pages if isinstance(p, int))
                sources.append(title + (' · p. ' + ', '.join(map(str, numbers)) if numbers else ''))
            blocks.append('### ' + section.heading + '\n\n' + humanize_keys(section.body, list(labels.values())) +
                          ('\n\n근거: ' + '; '.join(sources) if sources else ''))
        return '\n\n'.join(blocks)
