# Cloud & Multi-User Evaluation for Composez

## Current State

Composez already has a **single-user web architecture**:

- **FastAPI backend** (`composez_core/server/app.py`) with 36+ REST endpoints and a WebSocket for streaming chat
- **Vue 3 frontend** (`novel_ui/`) with CodeMirror editor, file browser, git pane, and console
- **File-based storage** — all project data lives on the local filesystem (novel content, `db/` reference material, `.composez` config)
- **Single global Coder instance** — one `_coder` module-level variable initialized lazily on first WebSocket connect
- **No authentication** — open CORS (`allow_origins=["*"]`), no login, no session management
- **Git for version history** — each project is a git repo; all edits are committed locally
- **Local-only deployment** — binds to `127.0.0.1:8188` by default

The core bottleneck for multi-user is that the entire server is wired to a single project directory and a single Coder instance.

---

## Key Challenges

| Challenge | Why It's Hard |
|-----------|--------------|
| **Isolation** | Aider's Coder holds mutable state (file lists, model config, chat history, git repo ref). Multiple users editing the same project need either separate Coder instances or serialized access. |
| **File storage** | Content lives on a local filesystem. Cloud deployment needs remote storage with per-project isolation. |
| **Git** | Git repos are local directories. Concurrent writers to the same repo cause conflicts. |
| **LLM costs** | Each user drives their own API calls. Need per-user metering or shared billing. |
| **Real-time collab** | The WebSocket currently streams LLM responses to one client. Multi-user editing requires conflict resolution (OT/CRDT). |

---

## Option A: Cloud-Hosted Single-Tenant (One Container per User)

**Concept:** Each user/project gets its own container running the existing server.

### Architecture
```
User → HTTPS → Reverse Proxy (nginx/Caddy)
                    ↓
            Container Orchestrator (ECS / Cloud Run / K8s)
                    ↓
        ┌───────────────────────┐
        │  Container per user   │
        │  uvicorn + FastAPI    │
        │  local git repo       │
        │  mounted volume       │
        └───────────────────────┘
```

### What Changes
- **Auth gateway** — add an auth layer (OAuth2/OIDC) at the proxy or a lightweight middleware
- **Container orchestration** — spin up/down containers per session (Cloud Run is a natural fit: scale-to-zero, per-request billing)
- **Persistent storage** — mount a volume (EFS, GCS FUSE, Persistent Disk) per project so the git repo and files survive container restarts
- **Routing** — map `user-project.composez.app` or `/projects/{id}` to the right container
- **LLM key management** — users bring their own API keys (stored encrypted) or you provide a pooled key with per-user metering

### Pros
- **Minimal code changes** — the existing single-tenant server works as-is inside a container
- **Strong isolation** — each user gets their own filesystem, git repo, and Coder instance
- **Simple concurrency model** — no shared-state problems
- **Incremental path** — can start with a handful of users on dedicated VMs, then move to orchestration

### Cons
- **Cold-start latency** — spinning up a container + initializing a Coder takes seconds (Cloud Run cold starts ~2-5s, Coder init adds more)
- **Cost at scale** — one container per active user; idle containers waste resources unless using scale-to-zero
- **No real-time collaboration** — each project is still single-user; sharing requires passing the project (like a baton)
- **Storage management** — need to handle volume lifecycle, backups, cross-region access

### Estimated Effort
**2-4 weeks** for a working prototype (Dockerfile tuning, auth gateway, volume mounts, deploy scripts).

---

## Option B: Multi-Tenant Server with Per-Session Coder Pools

**Concept:** A single (or small cluster of) server(s) manage multiple Coder instances, one per active session.

### Architecture
```
Users → HTTPS → Load Balancer
                    ↓
            App Server(s) (FastAPI)
            ┌─────────────────────────────┐
            │  Session Manager            │
            │  ┌────────┐ ┌────────┐     │
            │  │Coder A │ │Coder B │ ... │
            │  └────────┘ └────────┘     │
            │  Project Storage (S3/GCS)   │
            └─────────────────────────────┘
```

### What Changes
- **Session manager** — replace the global `_coder` with a session→Coder registry; route requests by session/project ID
- **Auth + user model** — add user accounts, project ownership, API key vault
- **Remote storage backend** — abstract file operations to support S3/GCS (clone project to temp dir on session start, sync back on idle/close)
- **Database** — add PostgreSQL or SQLite for user accounts, project metadata, billing; keep content files on object storage
- **WebSocket multiplexing** — tag WebSocket messages with session IDs; route to the correct Coder
- **Resource limits** — cap concurrent Coders per server, evict idle sessions, queue overflow

### Pros
- **Better resource utilization** — share server resources across users; no per-user container overhead
- **Lower latency** — Coder instances can be kept warm in memory
- **Foundation for collaboration** — the session manager is the natural place to add multi-writer support later

### Cons
- **Significant refactoring** — the biggest change is decoupling the global Coder from request handling; many endpoints call `_get_coder()` which assumes one global instance
- **Isolation risks** — a bug in one session could affect others (memory leak, uncaught exception)
- **Git complexity** — need per-session working directories; concurrent git operations on clones of the same repo need careful locking
- **Coder memory footprint** — each Coder instance holds chat history, file caches, model state; ~50-200MB per session depending on project size

### Estimated Effort
**6-10 weeks** for a production-ready multi-tenant server.

---

## Option C: Git-Centric Collaboration (Multi-User on Shared Projects)

**Concept:** Build on Option A or B but add real-time multi-user editing of the same project.

### Architecture Additions
```
                    ┌─────────────────┐
                    │  Collab Server   │
                    │  (Y.js / CRDT)  │
                    │  ↕ sync ↕       │
        ┌───────────┴───┐   ┌───┴───────────┐
        │ User A browser│   │ User B browser│
        └───────────────┘   └───────────────┘
                    ↓ periodic commit ↓
                    ┌─────────────────┐
                    │    Git repo     │
                    │  (source of     │
                    │   truth)        │
                    └─────────────────┘
```

### What Changes (on top of A or B)
- **CRDT layer** — integrate Y.js (or Automerge) for conflict-free concurrent editing of `.md` files
- **Presence & cursors** — show who's editing where
- **Merge strategy** — CRDT handles real-time edits; git commits are periodic snapshots
- **LLM coordination** — when one user runs `/write`, lock the affected files or queue edits so the LLM output doesn't conflict with a concurrent human edit
- **Permissions** — owner/editor/viewer roles per project

### Pros
- **True collaboration** — Google-Docs-style multi-writer experience for fiction teams (author + editor, writing partners)
- **Differentiated product** — no existing AI writing tool offers real-time collaborative AI-assisted fiction editing

### Cons
- **Very high complexity** — CRDTs are hard; integrating them with git commit history and LLM-generated edits adds significant surface area
- **LLM contention** — two users both running `/write` on overlapping scenes creates hard coordination problems
- **Scope creep risk** — collaboration features tend to expand indefinitely

### Estimated Effort
**3-6 months** on top of either Option A or B.

---

## Option D: Hybrid — GitHub/GitLab as the Collaboration Layer

**Concept:** Use Option A (one container per user) but share projects via git remotes. Collaboration happens through branches and PRs, not real-time co-editing.

### Architecture
```
User A (container) ──push──→ GitHub repo ←──pull── User B (container)
                              ↑
                         PRs / Reviews
                         Branch protection
```

### What Changes
- **Git remote integration** — add push/pull to remote in the UI (the git pane already shows branches/remotes)
- **Branch workflow** — each user works on their own branch; merges happen via PR
- **Project sharing** — invite collaborators by granting access to the GitHub repo
- **Conflict resolution** — leverage git's merge tooling; the CodeMirror merge view already exists in the UI

### Pros
- **Minimal new infrastructure** — git hosting platforms handle auth, storage, collaboration, and backup
- **Familiar model** — writers who use git (or can learn basic branch/merge) get a natural workflow
- **Low risk** — no CRDT, no shared-state servers, no real-time sync complexity
- **Async collaboration fits fiction** — writers rarely need to edit the same sentence simultaneously; chapter-level branching is natural

### Cons
- **Not real-time** — no live co-editing; users see each other's changes after push/pull
- **Git literacy required** — many fiction writers are not git users (though the UI can abstract most of it)
- **Merge conflicts on prose** — git's line-based merge doesn't understand narrative structure; conflicts in `.md` files can be confusing

### Estimated Effort
**2-3 weeks** on top of Option A (mostly UI work for push/pull/branch workflows).

---

## Recommendation

### Phase 1: Cloud-Hosted Single-Tenant (Option A) — start here
This gets you to "cloud-based" with the least risk and code change. The existing server works almost unchanged inside a container. Focus on:
1. **Auth gateway** (OAuth2 via Auth0/Clerk/Supabase Auth — ~1 week)
2. **Container orchestration** (Cloud Run or Fly.io — ~1 week)
3. **Persistent storage** (mounted volume per project — ~1 week)
4. **LLM key management** (encrypted key vault — ~3 days)

### Phase 2: Git-Based Collaboration (Option D) — add sharing
Once cloud hosting works, add git remote workflows for async collaboration. This is the highest-value, lowest-risk path to multi-user.

### Phase 3: Multi-Tenant (Option B) — optimize costs
Only pursue this when the per-container model becomes too expensive at scale (likely >100 concurrent users). The session-manager refactor is significant but well-understood.

### Phase 4: Real-Time Collaboration (Option C) — if demand warrants
Only build CRDT-based co-editing if users explicitly need it. For fiction writing, async collaboration (branches + reviews) is usually sufficient.

---

## Platform Comparison for Phase 1

| Platform | Cold Start | Scale to Zero | Persistent Storage | Cost Model | Fit |
|----------|-----------|---------------|-------------------|------------|-----|
| **Fly.io** | ~1-2s | Yes (Machines) | Fly Volumes | Per-VM-second | Good — simple, volumes built-in |
| **Google Cloud Run** | ~2-5s | Yes | GCS FUSE (limited) | Per-request | OK — needs sidecar for git/volumes |
| **AWS ECS + Fargate** | ~10-30s | No (min 1 task) | EFS | Per-vCPU-hour | Heavy — better for steady load |
| **Railway** | ~2-3s | Yes | Volumes (beta) | Per-usage | Good — simple DX, growing platform |
| **Render** | ~5-10s | Yes (free tier) | Persistent Disk | Per-instance | OK — simple but limited scaling |
| **Self-hosted K8s** | ~1s (warm) | Via KEDA | Any CSI driver | Infrastructure | Best control, most ops burden |

**Recommendation:** Fly.io for initial launch (simple Machines API, built-in volumes, fast cold starts, predictable pricing). Migrate to K8s or Cloud Run if scaling demands it.
