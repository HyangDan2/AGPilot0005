"""Human source labels; verification keeps the original source keys."""
import re


def legacy_citations(job):
    answer = job.get('answer')
    if not answer:
        return answer, []
    entries = {e['doc_id'] + '/' + e['chunk_id']: e for e in job.get('entries', [])}
    citations = []
    def replace(match):
        labels = {}
        for key in re.findall(r'\[(doc_[A-Za-z0-9_-]+/(?:chk|sec)_[A-Za-z0-9_-]+)\]', match.group()):
            e = entries.get(key)
            if not e:
                continue
            title = job.get('document_titles', {}).get(e['doc_id'], '첨부 문서')
            page = (e.get('source') or {}).get('page')
            labels.setdefault(title, set()).add(page)
            if not any(c['key'] == key for c in citations):
                citations.append({'key': key, 'doc_id': e['doc_id'], 'chunk_id': e['chunk_id'], 'title': title, 'page': page})
        if not labels:
            return match.group()
        return '; '.join(title + (' · p. ' + ', '.join(map(str, sorted(p for p in pages if isinstance(p,int))))
                                   if any(isinstance(p,int) for p in pages) else '') for title,pages in labels.items())
    return re.sub(r'(?:\[doc_[A-Za-z0-9_-]+/(?:chk|sec)_[A-Za-z0-9_-]+\](?:,\s*)?)+', replace, answer), citations


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
