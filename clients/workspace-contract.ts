/**
 * AGCore R2-W — Workspace Shared Contract (client types, contract r2-w/v1)
 *
 * Mirror of `docs/r2/WORKSPACE_CONTRACT.md` and `app/workspace/contracts.py`.
 * Canonical copy lives in AGPilot0005 `clients/workspace-contract.ts`; the frontend copy is
 * AGPilot0011 `hd2ai-shared/workspace-contract.ts` (byte-identical).
 *
 * Canvas Engineer: build against `WorkspaceProvider` + `createMockWorkspaceProvider()`.
 */

export const WORKSPACE_CONTRACT_VERSION = 'r2-w/v1';

export type WorkspaceStatus = 'active' | 'archived' | 'deleted';
export type AccessLevel = 'read' | 'write' | 'admin' | 'none';
export type FileKind = 'file' | 'folder';
export type IndexState = 'PENDING' | 'INDEXING' | 'READY' | 'FAILED' | 'STALE' | 'CANCELLED';
export type SearchMode = 'keyword' | 'vector' | 'hybrid';
export type RevisionStatus = 'current' | 'superseded' | 'deleted';
export type ContextStatus = 'ok' | 'insufficient_documents' | 'empty_workspace';

export type WorkspaceErrorCode =
  | 'workspace_not_found'
  | 'file_not_found'
  | 'file_exists'
  | 'revision_conflict'
  | 'quota_exceeded'
  | 'file_too_large'
  | 'unsupported_media_type'
  | 'invalid_path'
  | 'invalid_request'
  | 'index_not_ready'
  | 'embedding_unavailable'
  | 'forbidden';

export interface WorkspaceError {
  code: WorkspaceErrorCode;
  message?: string;
  retryable?: boolean;
  context?: Record<string, unknown>;
}

export interface WorkspaceIdentity {
  workspace_id: string;
  tenant_id: string;
  owner_id: string;
  service_id: string;
  name: string;
  description: string;
  status: WorkspaceStatus;
  revision: number;
  created_at: string;
  updated_at: string;
}

export interface FileIdentity {
  file_id: string;
  workspace_id: string;
  parent_id: string | null;
  kind: FileKind;
  name: string;
  path: string;
  mime_type: string | null;
  size_bytes: number;
  content_hash: string | null;
  revision: number;
  index_status: IndexState | null;
  created_at: string;
  updated_at: string;
}

export interface AccessResult {
  workspace_id: string;
  level: AccessLevel;
  reason: string;
}

export interface FileContent {
  metadata: FileIdentity;
  revision: number;
  revision_status: RevisionStatus;
  content_hash: string;
  text?: string;
  /** Raw bytes are fetched from `GET /files/{id}/content`; not carried in JSON. */
  download_url?: string;
}

export interface ArtifactReference {
  artifact_ref_id: string;
  workspace_id: string;
  artifact_id: string;
  kind: string;
  file_id: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface IndexJob {
  job_id: string;
  workspace_id: string;
  file_id: string;
  file_revision: number;
  state: IndexState;
  attempts: number;
  max_attempts: number;
  error_code: string | null;
  error_detail: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface EmbedderStatus {
  provider: string;
  model_id: string;
  dimension: number;
  available: boolean;
  degraded_reason: string | null;
}

export interface IndexSummary {
  workspace_id: string;
  files_total: number;
  ready: number;
  indexing: number;
  pending: number;
  failed: number;
  stale: number;
  cancelled: number;
  chunks_total: number;
  embedder: EmbedderStatus;
  retrieval_modes: SearchMode[];
}

export interface SearchResult {
  chunk_id: string;
  file_id: string;
  file_revision: number;
  file_name: string;
  page_number: number | null;
  section: string | null;
  excerpt: string;
  score: number;
  keyword_score: number | null;
  vector_score: number | null;
  matched_by: string[];
}

export interface SearchResponse {
  workspace_id: string;
  query: string;
  mode_requested: SearchMode;
  mode_effective: SearchMode;
  degraded: boolean;
  degradation_reason: string | null;
  provider_status: { keyword: string; embedding: string };
  total_matches: number;
  results: SearchResult[];
}

export interface Citation {
  citation_id: string;
  workspace_id: string;
  file_id: string;
  file_revision: number;
  file_name: string;
  chunk_id: string;
  page_number: number | null;
  section: string | null;
  source_excerpt: string;
  revision_status: RevisionStatus;
}

export interface ContextResponse {
  workspace_id: string;
  query: string;
  status: ContextStatus;
  context_text: string;
  used_tokens: number;
  budget_tokens: number;
  truncated: boolean;
  sources: Citation[];
  retrieved: SearchResult[];
  degraded: boolean;
}

export interface CitationResolveResponse {
  available: boolean;
  citation: Citation | null;
  revision_status: RevisionStatus;
  download_url: string | null;
}

export interface FileEvent {
  event_id: string;
  type:
    | 'file.created'
    | 'file.updated'
    | 'file.renamed'
    | 'file.moved'
    | 'file.deleted'
    | 'index.status_changed';
  workspace_id: string;
  file_id: string;
  revision: number;
  at: string;
}

export interface ArtifactReferenceInput {
  artifact_id: string;
  kind?: string;
  file_id?: string | null;
  metadata?: Record<string, unknown>;
}

/** Structural view of the verified server-side scope; clients never send tenant/owner. */
export interface ScopeView {
  tenant_id: string;
  owner_id: string;
  service_id: string;
}

/** Logical shared provider interface (contract §2). */
export interface WorkspaceProvider {
  getWorkspace(workspaceId: string): Promise<WorkspaceIdentity>;
  checkWorkspaceAccess(workspaceId: string): Promise<AccessResult>;
  listWorkspaceFiles(workspaceId: string, parentId?: string | null, recursive?: boolean): Promise<FileIdentity[]>;
  getFileMetadata(workspaceId: string, fileId: string): Promise<FileIdentity>;
  readFile(workspaceId: string, fileId: string, revision?: number): Promise<FileContent>;
  createFile(
    workspaceId: string,
    name: string,
    content: Blob | string,
    parentId?: string | null,
    expectedRevision?: number,
  ): Promise<FileIdentity>;
  registerArtifactReference(workspaceId: string, ref: ArtifactReferenceInput): Promise<ArtifactReference>;
  listArtifactReferences(workspaceId: string, kind?: string): Promise<ArtifactReference[]>;
}

/** REST paths for the shared API contract (§3). `{…}` are path parameters. */
export const WORKSPACE_API = {
  base: '/api/v1/workspaces',
  list: '/api/v1/workspaces',
  create: '/api/v1/workspaces',
  detail: (workspaceId: string) => `/api/v1/workspaces/${workspaceId}`,
  access: (workspaceId: string) => `/api/v1/workspaces/${workspaceId}/access`,
  files: (workspaceId: string) => `/api/v1/workspaces/${workspaceId}/files`,
  folders: (workspaceId: string) => `/api/v1/workspaces/${workspaceId}/folders`,
  file: (workspaceId: string, fileId: string) => `/api/v1/workspaces/${workspaceId}/files/${fileId}`,
  fileContent: (workspaceId: string, fileId: string) =>
    `/api/v1/workspaces/${workspaceId}/files/${fileId}/content`,
  events: (workspaceId: string) => `/api/v1/workspaces/${workspaceId}/events`,
  indexStatus: (workspaceId: string) => `/api/v1/workspaces/${workspaceId}/index/status`,
  reindex: (workspaceId: string) => `/api/v1/workspaces/${workspaceId}/index/reindex`,
  indexJobs: (workspaceId: string) => `/api/v1/workspaces/${workspaceId}/index/jobs`,
  cancelIndex: (workspaceId: string, fileId: string) =>
    `/api/v1/workspaces/${workspaceId}/files/${fileId}/index/cancel`,
  search: (workspaceId: string) => `/api/v1/workspaces/${workspaceId}/search`,
  context: (workspaceId: string) => `/api/v1/workspaces/${workspaceId}/context`,
  citation: (workspaceId: string, citationId: string) =>
    `/api/v1/workspaces/${workspaceId}/citations/${citationId}`,
  validateCitations: (workspaceId: string) => `/api/v1/workspaces/${workspaceId}/citations/validate`,
  artifactReferences: (workspaceId: string) =>
    `/api/v1/workspaces/${workspaceId}/artifact-references`,
  conversationReferences: (workspaceId: string) =>
    `/api/v1/workspaces/${workspaceId}/conversation-references`,
} as const;

export function isWorkspaceError(value: unknown): value is WorkspaceError {
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof (value as { code?: unknown }).code === 'string' &&
    (value as { code: string }).code in ERROR_HTTP_STATUS
  );
}

export const ERROR_HTTP_STATUS: Record<WorkspaceErrorCode, number> = {
  workspace_not_found: 404,
  file_not_found: 404,
  file_exists: 409,
  revision_conflict: 409,
  quota_exceeded: 413,
  file_too_large: 413,
  unsupported_media_type: 415,
  invalid_path: 400,
  invalid_request: 400,
  index_not_ready: 409,
  embedding_unavailable: 503,
  forbidden: 403,
};

/**
 * In-memory provider for Canvas development and offline UI work. It implements the same
 * shapes as the server engine; nothing here is persisted.
 */
export interface MockWorkspaceProvider extends WorkspaceProvider {
  /** Seed a workspace owned by the mock scope (not part of the provider contract). */
  seedWorkspace(name?: string): WorkspaceIdentity;
  /** Seed a text file inside a mock workspace (not part of the provider contract). */
  seedFile(workspaceId: string, name: string, content: string, parentId?: string | null): FileIdentity;
}

export function createMockWorkspaceProvider(
  now: () => string = () => new Date().toISOString(),
): MockWorkspaceProvider {
  let seq = 0;
  const id = (prefix: string) => `${prefix}_${(++seq).toString(16).padStart(6, '0')}`;
  const workspaces = new Map<string, WorkspaceIdentity>();
  const files = new Map<string, FileIdentity & { content: string }>();
  const artifactRefs = new Map<string, ArtifactReference>();

  const ensure = (workspaceId: string) => {
    const ws = workspaces.get(workspaceId);
    if (!ws || ws.status === 'deleted') {
      throw { code: 'workspace_not_found', message: 'workspace not found' } satisfies WorkspaceError;
    }
    return ws;
  };

  const provider: MockWorkspaceProvider = {
    seedWorkspace(name = 'Mock workspace') {
      const stamp = now();
      const ws: WorkspaceIdentity = {
        workspace_id: id('ws'),
        tenant_id: 'mock_tenant',
        owner_id: 'mock_user',
        service_id: 'hd2ai-admin',
        name,
        description: '',
        status: 'active',
        revision: 1,
        created_at: stamp,
        updated_at: stamp,
      };
      workspaces.set(ws.workspace_id, ws);
      return { ...ws };
    },
    seedFile(workspaceId, name, content, parentId = null) {
      ensure(workspaceId);
      const stamp = now();
      const file = {
        file_id: id('wf'),
        workspace_id: workspaceId,
        parent_id: parentId,
        kind: 'file' as const,
        name,
        path: `/${name}`,
        mime_type: 'text/plain',
        size_bytes: content.length,
        content_hash: `sha256:mock-${seq}`,
        revision: 1,
        index_status: 'READY' as IndexState,
        created_at: stamp,
        updated_at: stamp,
        content,
      };
      files.set(file.file_id, file);
      const { content: _content, ...meta } = file;
      return { ...meta };
    },
    async getWorkspace(workspaceId) {
      return { ...ensure(workspaceId) };
    },
    async checkWorkspaceAccess(workspaceId) {
      const ws = workspaces.get(workspaceId);
      return {
        workspace_id: workspaceId,
        level: ws && ws.status !== 'deleted' ? 'admin' : 'none',
        reason: ws ? 'owner' : 'unknown_workspace',
      };
    },
    async listWorkspaceFiles(workspaceId, parentId = null, recursive = false) {
      ensure(workspaceId);
      return [...files.values()]
        .filter((f) => f.workspace_id === workspaceId)
        .filter((f) => (recursive ? true : f.parent_id === parentId))
        .map(({ content: _content, ...meta }) => ({ ...meta }));
    },
    async getFileMetadata(workspaceId, fileId) {
      ensure(workspaceId);
      const file = files.get(fileId);
      if (!file || file.workspace_id !== workspaceId) {
        throw { code: 'file_not_found', message: 'file not found' } satisfies WorkspaceError;
      }
      const { content: _content, ...meta } = file;
      return { ...meta };
    },
    async readFile(workspaceId, fileId, revision) {
      ensure(workspaceId);
      const file = files.get(fileId);
      if (!file || file.workspace_id !== workspaceId) {
        throw { code: 'file_not_found', message: 'file not found' } satisfies WorkspaceError;
      }
      const { content, ...meta } = file;
      return {
        metadata: { ...meta },
        revision: revision ?? file.revision,
        revision_status: revision && revision !== file.revision ? 'superseded' : 'current',
        content_hash: file.content_hash ?? '',
        text: content,
      };
    },
    async createFile(workspaceId, name, content, parentId = null, expectedRevision) {
      ensure(workspaceId);
      const text = typeof content === 'string' ? content : '';
      const existing = [...files.values()].find(
        (f) => f.workspace_id === workspaceId && f.parent_id === parentId && f.name === name && f.kind === 'file',
      );
      if (existing) {
        if (expectedRevision !== undefined && expectedRevision !== existing.revision) {
          throw {
            code: 'revision_conflict',
            context: { expected_revision: expectedRevision, current_revision: existing.revision },
          } satisfies WorkspaceError;
        }
        existing.revision += 1;
        existing.content = text;
        existing.size_bytes = text.length;
        existing.updated_at = now();
        existing.index_status = 'PENDING';
        return { ...existing };
      }
      const stamp = now();
      const file = {
        file_id: id('wf'),
        workspace_id: workspaceId,
        parent_id: parentId,
        kind: 'file' as const,
        name,
        path: `/${name}`,
        mime_type: 'text/plain',
        size_bytes: text.length,
        content_hash: `sha256:mock-${seq}`,
        revision: 1,
        index_status: 'PENDING' as IndexState,
        created_at: stamp,
        updated_at: stamp,
        content: text,
      };
      files.set(file.file_id, file);
      return { ...file };
    },
    async registerArtifactReference(workspaceId, ref) {
      ensure(workspaceId);
      const record: ArtifactReference = {
        artifact_ref_id: id('wref'),
        workspace_id: workspaceId,
        artifact_id: ref.artifact_id,
        kind: ref.kind ?? 'artifact',
        file_id: ref.file_id ?? null,
        metadata: ref.metadata ?? {},
        created_at: now(),
      };
      artifactRefs.set(record.artifact_ref_id, record);
      return record;
    },
    async listArtifactReferences(workspaceId, kind) {
      ensure(workspaceId);
      return [...artifactRefs.values()].filter(
        (r) => r.workspace_id === workspaceId && (!kind || r.kind === kind),
      );
    },
  };
  return provider;
}
