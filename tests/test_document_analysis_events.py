import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.document_analysis import router


def test_analysis_event_stream_and_legacy_snapshot():
    class Analysis:
        def __init__(self):
            self.reads = 0

        def get(self, job_id, owner, service):
            if (job_id, owner, service) != ('ana_test', 'owner', 'hd2ai-public'):
                return None
            self.reads += 1
            if self.reads == 1:
                return {'status': 'running', 'answer': '첫 단락'}
            return {'status': 'completed', 'answer': '확정 답변', 'model_label': 'fallback'}

    app = FastAPI()
    app.state.analysis_services = Analysis()
    app.include_router(router)
    headers = {'X-HD2Lab-User': 'owner', 'X-HD2Lab-Service': 'hd2ai-public'}
    path = '/agent/analyses/ana_test'
    with TestClient(app) as client:
        events = client.get(path, headers={**headers, 'Accept': 'text/event-stream'})
        assert events.headers['content-type'].startswith('text/event-stream')
        assert events.headers['x-accel-buffering'] == 'no'
        assert [json.loads(line[6:]) for line in events.text.splitlines() if line.startswith('data: ')] == [
            {'status': 'running', 'answer': '첫 단락'},
            {'status': 'completed', 'answer': '확정 답변', 'model_label': 'fallback'},
        ]
        assert client.get(path, headers=headers).json()['answer'] == '확정 답변'
        assert client.get(path, headers={**headers, 'X-HD2Lab-User': 'other', 'Accept': 'text/event-stream'}).status_code == 404
