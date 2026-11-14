"""Owner-scoped task progress. No document bodies or planner text in events."""
class TaskProgress:
    def __init__(self, repository, clock):
        self.repository, self.clock = repository, clock
        self.flushed = {}

    def event(self, task, kind, label, status='running', item_id=None, **data):
        if kind == 'run' and status != 'running':
            self.close_phase(task)
        seq = task.events[-1]['seq'] + 1 if task.events else 1
        safe = {k: v for k, v in data.items() if k in ('duration_ms', 'model_call', 'reasoning_effort', 'cached', 'page')}
        task.events.append({'seq': seq, 'id': item_id or f'event-{seq}', 'kind': kind, 'label': label,
                            'status': status, 'at': self.clock(), **safe})
        if len(task.events) > 1000:
            task.events = task.events[-1000:]
        self.repository.save(task)

    def phase(self, task, phase, label):
        self.close_phase(task)
        task.phase = phase
        self.event(task, 'phase', label, item_id='phase-' + str(len(task.events) + 1))

    def close_phase(self, task):
        last = next((e for e in reversed(task.events) if e['kind'] == 'phase'), None)
        if last and last['status'] == 'running':
            self.event(task, 'phase', last['label'], status='done', item_id=last['id'],
                       duration_ms=round((self.clock()-last['at'])*1000))

    def reasoning(self, task, chunk):
        task.reasoning_text += chunk
        if len(task.reasoning_text) > 200000:
            task.reasoning_text = task.reasoning_text[:200000] + '\n[표시 한도에 도달했습니다]'
        now = self.clock()
        if now - self.flushed.get(task.job_id, 0) >= .5:
            self.repository.save(task)
            self.flushed[task.job_id] = now
