/**
 * AGCore R2-W — Workspace Shared Contract (client types, contract r2-w/v1+a02)
 *
 * Mirror of `docs/r2/WORKSPACE_CONTRACT.md` and `app/workspace/contracts.py`.
 * Canonical copy lives in AGPilot0005 `clients/workspace-contract.ts`; the frontend copy is
 * AGPilot0011 `hd2ai-shared/workspace-contract.ts` (byte-identical).
 *
 * Amendment 01 (folder-based workspace) is additive: every v1 field/operation keeps its
 * meaning, and every Amendment 01 member is optional for a v1 consumer.
 *
 * Amendment 02 (agent tools) is additive as well: the workspace exposes 11 server-side agent
 * tools and a metadata-only chat scope. Agent tools are NOT client provider operations; the
 * `WorkspaceProvider` interface below is unchanged.
 *
 * Canvas Engineer: build against `WorkspaceProvider` + `createMockWorkspaceProvider()`.
 * Workspace→Canvas handoff (WO-C18): `POST /artifacts/from-workspace`,
 * `POST /artifacts/{id}/save-to-workspace`, `GET /artifacts/{id}/workspace-link`;
 * the workspace file's `content_hash` is the Canvas `last_saved_content_hash`.
 */

export const WORKSPACE_CONTRACT_VERSION = 'r2-w/v1';
export const WORKSPACE_CONTRACT_AMENDMENT = 'A02';
export const WORKSPACE_CONTRACT_REVISION = 'r2-w/v1+a02';

export type WorkspaceStatus = 'active' | 'archived' | 'deleted';
export type WorkspaceLifecycleState = 'open' | 'closed' | 'deleted';
export type WorkspaceStorageProvider = 'server_managed' | 'server_mounted_folder' | 'local_browser';
export type ContentOrigin = 'blob' | 'external';
export type RootStatus = 'available' | 'missing' | 'not_allowed' | 'metadata_overlap';
export type AccessLevel = 'read' | 'write' | 'admin' | 'none';
export type FileKind = 'file' | 'folder';
export type IndexState = 'PENDING' | 'INDEXING' | 'READY' | 'FAILED' | 'STALE' | 'CANCELLED';
export type SearchMode = 'keyword' | 'vector' | 'hybrid';
export type RevisionStatus = 'current' | 'superseded' | 'deleted';
export type ContextStatus = 'ok' | 'insufficient_documents' | 'empty_workspace';

export type WorkspaceErrorCode =
  | 'workspace_not_found'
  | 'workspace_closed'
  | 'read_only_workspace'
  | 'path_outside_allowed_root'
  | 'symlink_escape'
  | 'root_unavailable'
  | 'provider_mismatch'
  | 'content_conflict'
  | 'source_delete_not_permitted'
  | 'metadata_overlap'
  | 'watch_unavailable'
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
  | 'forbidden'
  // Amendment 02 — agent tools
  | 'approval_required'
  | 'patch_conflict'
  | 'section_not_found'
  | 'capability_denied';

export interface WorkspaceError {
  code: WorkspaceErrorCode;
  message?: string;
  retryable?: boolean;
  context?: Record<string, unknown>;
}

export interface WorkspaceRootInfo {
  storage_provider: WorkspaceStorageProvider;
  root_id: string | null;
  display_path: string;
  allowed_root_id: string | null;
  read_only: boolean;
  zero_copy: boolean;
  /** False for `local_browser`: the server never receives a server-side path. */
  server_side_path_access: boolean;
  client_handle_id: string | null;
  opened_at: string | null;
  status: RootStatus;
}

export interface AllowedRootInfo {
  root_id: string;
  display_path: string;
  storage_provider: WorkspaceStorageProvider;
  writable: boolean;
  scope: string;
  is_default: boolean;
}

export interface IndexStorageInfo {
  separate_from_workspace_root: boolean;
  metadata_store: string;
  metadata_location_ref: string;
  chunk_index_location_ref: string;
  vector_index_location_ref: string;
  inside_workspace_root: boolean;
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
  /** Amendment 01 */
  lifecycle_state?: WorkspaceLifecycleState;
  root?: WorkspaceRootInfo;
  closed_at?: string | null;
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
  /** Amendment 01 */
  storage_provider?: WorkspaceStorageProvider;
  content_origin?: ContentOrigin;
  relative_path?: string | null;
  synced_at?: string | null;
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

// ---------------------------------------------------------------------------------------
// Amendment 01 — folder-based workspace lifecycle, root, write and sync
// ---------------------------------------------------------------------------------------

export interface FolderOpenRequest {
  path: string;
  name?: string;
  read_only?: boolean;
  root_id?: string;
}

export interface NewWorkspaceRequest {
  name: string;
  description?: string;
  parent_path?: string;
  root_id?: string;
}

export interface LocalBrowserEntry {
  path: string;
  kind?: FileKind;
  size_bytes?: number;
  content_hash?: string | null;
  text?: string | null;
  mime_type?: string | null;
}

export interface LocalBrowserWorkspaceRequest {
  name: string;
  description?: string;
  client_handle_id: string;
  display_path?: string;
  read_only?: boolean;
  entries?: LocalBrowserEntry[];
}

export interface WorkspaceCloseRequest {
  reason?: string;
}

export interface WorkspaceDisconnectRequest {
  /** Default false: disconnecting never deletes the real folder. */
  delete_source_files?: boolean;
  confirm?: boolean;
}

export interface WorkspaceWriteRequest {
  path: string;
  content?: string;
  content_base64?: string;
  mime_type?: string;
  expected_content_hash?: string;
  create_parents?: boolean;
  overwrite?: boolean;
}

export interface ClientWriteDirective {
  required: boolean;
  client_handle_id: string | null;
  relative_path: string;
  reason: string;
}

export interface WorkspaceWriteResult {
  workspace_id: string;
  path: string;
  file: FileIdentity | null;
  revision: number;
  content_hash: string | null;
  written_bytes: number;
  storage_provider: WorkspaceStorageProvider;
  content_origin: ContentOrigin;
  /** Set for `local_browser`: the browser must perform the write through its handle. */
  client_write: ClientWriteDirective | null;
}

export interface WorkspaceSyncResult {
  workspace_id: string;
  full: boolean;
  scanned: number;
  created: number;
  modified: number;
  moved: number;
  deleted: number;
  unchanged: number;
  errors: Array<Record<string, unknown>>;
  started_at: string;
  finished_at: string;
}

export interface WorkspaceWatchStatus {
  workspace_id: string;
  watching: boolean;
  storage_provider: WorkspaceStorageProvider;
  interval_seconds: number;
  last_scan_at: string | null;
  last_change_at: string | null;
  changes_total: number;
  last_error: string | null;
}

/**
 * Validate and normalize a workspace-relative path (the same gate the server applies).
 * Rejects absolute paths, drive letters, backslashes, control characters and `.`/`..`.
 */
export function validateRelativePath(path: string): string {
  if (typeof path !== 'string') throw { code: 'invalid_path', message: 'path must be a string' };
  const raw = path;
  if (!raw.trim()) throw { code: 'invalid_path', message: 'empty path' };
  if (/[\u0000-\u001f\u007f]/.test(raw)) throw { code: 'invalid_path', message: 'control characters rejected' };
  if (raw.includes('\\')) throw { code: 'invalid_path', message: 'backslash rejected' };
  if (raw.startsWith('/') || raw.startsWith('~')) throw { code: 'invalid_path', message: 'absolute path rejected' };
  if (raw.length > 1024) throw { code: 'invalid_path', message: 'path too long' };
  if (/^[A-Za-z]:/.test(raw)) throw { code: 'invalid_path', message: 'drive letter rejected' };
  const parts = raw.split('/');
  for (const part of parts) {
    if (!part || part === '.' || part === '..') throw { code: 'invalid_path', message: `invalid segment: ${part}` };
    if (part.length > 255) throw { code: 'invalid_path', message: 'path segment too long' };
  }
  return parts.join('/');
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
  // Amendment 01 (optional for v1 consumers)
  openFolder?(request: FolderOpenRequest): Promise<WorkspaceIdentity>;
  newWorkspace?(request: NewWorkspaceRequest): Promise<WorkspaceIdentity>;
  closeWorkspace?(workspaceId: string, reason?: string): Promise<WorkspaceIdentity>;
  reopenWorkspace?(workspaceId: string): Promise<WorkspaceIdentity>;
  disconnectWorkspace?(workspaceId: string, deleteSourceFiles?: boolean): Promise<Record<string, unknown>>;
  writeFile?(workspaceId: string, request: WorkspaceWriteRequest): Promise<WorkspaceWriteResult>;
  syncWorkspace?(workspaceId: string, full?: boolean): Promise<WorkspaceSyncResult>;
}

/** REST paths for the shared API contract (§3). `{…}` are path parameters. */
export const WORKSPACE_API = {
  base: '/api/v1/workspaces',
  list: '/api/v1/workspaces',
  create: '/api/v1/workspaces',
  openFolder: '/api/v1/workspaces/open-folder',
  newWorkspace: '/api/v1/workspaces/new',
  localBrowser: '/api/v1/workspaces/local-browser',
  allowedRoots: '/api/v1/workspaces/allowed-roots',
  detail: (workspaceId: string) => `/api/v1/workspaces/${workspaceId}`,
  access: (workspaceId: string) => `/api/v1/workspaces/${workspaceId}/access`,
  root: (workspaceId: string) => `/api/v1/workspaces/${workspaceId}/root`,
  close: (workspaceId: string) => `/api/v1/workspaces/${workspaceId}/close`,
  reopen: (workspaceId: string) => `/api/v1/workspaces/${workspaceId}/reopen`,
  disconnect: (workspaceId: string) => `/api/v1/workspaces/${workspaceId}/disconnect`,
  sync: (workspaceId: string) => `/api/v1/workspaces/${workspaceId}/sync`,
  watch: (workspaceId: string) => `/api/v1/workspaces/${workspaceId}/watch`,
  writeFile: (workspaceId: string) => `/api/v1/workspaces/${workspaceId}/file`,
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
  workspace_closed: 409,
  read_only_workspace: 403,
  path_outside_allowed_root: 403,
  symlink_escape: 400,
  root_unavailable: 409,
  provider_mismatch: 400,
  content_conflict: 409,
  source_delete_not_permitted: 403,
  metadata_overlap: 400,
  watch_unavailable: 409,
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
  approval_required: 409,
  patch_conflict: 409,
  section_not_found: 404,
  capability_denied: 403,
};

/**
 * In-memory provider for Canvas development and offline UI work. It implements the same
 * shapes as the server engine; nothing here is persisted.
 */
export interface MockWorkspaceProvider extends WorkspaceProvider {
  /** Seed a workspace owned by the mock scope (not part of the provider contract). */
  seedWorkspace(
    name?: string,
    storageProvider?: WorkspaceStorageProvider,
    displayPath?: string,
    readOnly?: boolean,
  ): WorkspaceIdentity;
  /** Seed a text file inside a mock workspace (not part of the provider contract). */
  seedFile(workspaceId: string, name: string, content: string, parentId?: string | null): FileIdentity;
  // Amendment 01 is required on the mock so Canvas can build the full folder lifecycle.
  openFolder(request: FolderOpenRequest): Promise<WorkspaceIdentity>;
  newWorkspace(request: NewWorkspaceRequest): Promise<WorkspaceIdentity>;
  closeWorkspace(workspaceId: string, reason?: string): Promise<WorkspaceIdentity>;
  reopenWorkspace(workspaceId: string): Promise<WorkspaceIdentity>;
  disconnectWorkspace(workspaceId: string, deleteSourceFiles?: boolean): Promise<Record<string, unknown>>;
  writeFile(workspaceId: string, request: WorkspaceWriteRequest): Promise<WorkspaceWriteResult>;
  syncWorkspace(workspaceId: string, full?: boolean): Promise<WorkspaceSyncResult>;
}

function mockRoot(
  provider: WorkspaceStorageProvider,
  displayPath: string,
  readOnly: boolean,
  stamp: string,
): WorkspaceRootInfo {
  const zeroCopy = provider !== 'server_managed';
  return {
    storage_provider: provider,
    root_id: null,
    display_path: displayPath,
    allowed_root_id: null,
    read_only: readOnly,
    zero_copy: zeroCopy,
    server_side_path_access: provider !== 'local_browser',
    client_handle_id: provider === 'local_browser' ? 'mock-handle' : null,
    opened_at: stamp,
    status: 'available',
  };
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
    seedWorkspace(name = 'Mock workspace', storageProvider = 'server_managed', displayPath = '', readOnly = false) {
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
        lifecycle_state: 'open',
        root: mockRoot(storageProvider, displayPath, readOnly, stamp),
        closed_at: null,
      };
      workspaces.set(ws.workspace_id, ws);
      return { ...ws };
    },
    async openFolder(request) {
      if (!request.path) throw { code: 'invalid_path', message: 'path required' } satisfies WorkspaceError;
      return this.seedWorkspace(
        request.name || 'Mock folder workspace',
        'server_mounted_folder',
        request.path,
        Boolean(request.read_only),
      );
    },
    async newWorkspace(request) {
      const ws = this.seedWorkspace(
        request.name,
        'server_managed',
        `${request.parent_path || '/mock/workspaces'}/${request.name}`,
      );
      const withRoot: WorkspaceIdentity = {
        ...ws,
        description: request.description ?? '',
        root: ws.root ? { ...ws.root, zero_copy: true, root_id: request.root_id ?? null } : ws.root,
      };
      workspaces.set(ws.workspace_id, withRoot);
      return { ...withRoot };
    },
    async closeWorkspace(workspaceId, reason = '') {
      const ws = ensure(workspaceId);
      const stamp = now();
      const updated: WorkspaceIdentity = {
        ...ws,
        lifecycle_state: 'closed',
        revision: ws.revision + 1,
        updated_at: stamp,
        closed_at: stamp,
      };
      workspaces.set(workspaceId, updated);
      void reason;
      return { ...updated };
    },
    async reopenWorkspace(workspaceId) {
      const ws = workspaces.get(workspaceId);
      if (!ws || ws.status === 'deleted') {
        throw { code: 'workspace_not_found', message: 'workspace not found' } satisfies WorkspaceError;
      }
      const stamp = now();
      const updated: WorkspaceIdentity = {
        ...ws,
        lifecycle_state: 'open',
        revision: ws.revision + 1,
        updated_at: stamp,
        closed_at: null,
      };
      workspaces.set(workspaceId, updated);
      return { ...updated };
    },
    async disconnectWorkspace(workspaceId, deleteSourceFiles = false) {
      if (deleteSourceFiles) {
        throw { code: 'source_delete_not_permitted', message: 'source deletion is not permitted' } satisfies WorkspaceError;
      }
      const ws = ensure(workspaceId);
      const purgedFiles = [...files.values()].filter((f) => f.workspace_id === workspaceId).length;
      for (const [fileId, file] of [...files.entries()]) {
        if (file.workspace_id === workspaceId) files.delete(fileId);
      }
      workspaces.set(workspaceId, { ...ws, status: 'deleted', lifecycle_state: 'deleted', updated_at: now() });
      return { workspace_id: workspaceId, lifecycle_state: 'deleted', purged_files: purgedFiles, source_files_deleted: false };
    },
    async writeFile(workspaceId, request) {
      const ws = ensure(workspaceId);
      if (ws.root?.read_only) {
        throw { code: 'read_only_workspace', message: 'workspace is read-only' } satisfies WorkspaceError;
      }
      const relative = validateRelativePath(request.path);
      if (ws.root?.storage_provider === 'local_browser') {
        return {
          workspace_id: workspaceId,
          path: relative,
          file: null,
          revision: 0,
          content_hash: null,
          written_bytes: 0,
          storage_provider: 'local_browser',
          content_origin: 'external',
          client_write: {
            required: true,
            client_handle_id: ws.root.client_handle_id ?? null,
            relative_path: relative,
            reason: 'local_browser_no_server_path_access',
          },
        };
      }
      const identity = await this.createFile(workspaceId, relative, request.content ?? '', null);
      return {
        workspace_id: workspaceId,
        path: relative,
        file: identity,
        revision: identity.revision,
        content_hash: identity.content_hash,
        written_bytes: identity.size_bytes,
        storage_provider: ws.root?.storage_provider ?? 'server_managed',
        content_origin: ws.root?.zero_copy ? 'external' : 'blob',
        client_write: null,
      };
    },
    async syncWorkspace(workspaceId, full = false) {
      ensure(workspaceId);
      const stamp = now();
      const total = [...files.values()].filter((f) => f.workspace_id === workspaceId && f.kind === 'file').length;
      return {
        workspace_id: workspaceId,
        full,
        scanned: total,
        created: 0,
        modified: 0,
        moved: 0,
        deleted: 0,
        unchanged: total,
        errors: [],
        started_at: stamp,
        finished_at: stamp,
      };
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
      const ws = ensure(workspaceId);
      if (ws.root?.read_only) {
        throw { code: 'read_only_workspace', message: 'workspace is read-only' } satisfies WorkspaceError;
      }
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
        storage_provider: ws.root?.storage_provider ?? 'server_managed',
        content_origin: ws.root?.zero_copy ? ('external' as ContentOrigin) : ('blob' as ContentOrigin),
        relative_path: parentId ? null : name,
        synced_at: null,
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

/* ------------------------------------------------------------------------------------------------
 * Amendment 02 — agent tools and the workspace scope of a chat request
 * ---------------------------------------------------------------------------------------------- */

/** The 11 server-side workspace agent tools (Amendment 02 §1). No terminal/OS tool exists. */
export type WorkspaceAgentToolName =
  | 'workspace_list_files'
  | 'workspace_find_files'
  | 'workspace_get_file_info'
  | 'workspace_read_file'
  | 'workspace_search_text'
  | 'workspace_retrieve'
  | 'workspace_create_file'
  | 'workspace_patch_file'
  | 'workspace_move_file'
  | 'workspace_delete_file'
  | 'workspace_create_directory';

export interface WorkspaceAgentFileAction {
  action: 'read' | 'created' | 'patched' | 'moved' | 'deleted' | 'opened';
  file_id: string;
  path: string;
  name: string;
  revision: number;
  previous_revision?: number | null;
  diff?: string | null;
  content_hash?: string | null;
  canvas?: {
    action: 'open';
    artifact_id: string;
    artifact_ref_id: string;
    workspace_id: string;
    file_id: string;
    path: string;
    revision: number;
    content_hash?: string | null;
  } | null;
}

export interface WorkspaceChatScopeContext {
  workspace_id: string;
  workspace_root: {
    root_id?: string | null;
    display_path: string;
    read_only: boolean;
    zero_copy: boolean;
    lifecycle_state: WorkspaceLifecycleState;
  };
  storage_provider: WorkspaceStorageProvider;
  execution_scope: { tenant_id: string; owner_id: string; service_id: string };
  granted_capabilities: {
    allowed_tools: WorkspaceAgentToolName[];
    read_only: boolean;
    write: boolean;
    delete: boolean;
    context_budget_tokens: number;
  };
  /** Counts and top-level names only — the chat scope never carries a file body. */
  file_metadata: {
    files: number;
    folders: number;
    bytes: number;
    indexed: number;
    pending: number;
    failed: number;
    stale: number;
    top_level: Array<{ file_id: string; kind: FileKind; name: string; relative_path: string }>;
  };
  retrieval_availability: {
    keyword: boolean;
    vector: boolean;
    hybrid: boolean;
    degraded: boolean;
    reasons: string[];
  };
}
