# GitHub-Sourced Stack Deployments

This plan turns the current Stacks foundation into the intended "connect
GitHub, pick a repo, deploy the Dockerfile or Compose file" workflow.

## Product Contract

Hosty is not becoming a general Docker control panel. A GitHub deployment is
still a Hosty Stack: one tenant-owned deployable unit, reconciled by the pure
planner, run rootless under the tenant account, published only through Caddy,
and constrained by the existing quota, backup, DNS, audit, and suspension
systems.

The user journey:

1. Connect GitHub once as a source.
2. Select an organization/user and private repository.
3. Select a branch, tag, or commit.
4. Let Hosty discover deployment files in the repo.
5. Choose `compose.yaml` / `docker-compose.yml` or `Dockerfile`.
6. Review detected services, ports, volumes, required env vars, and warnings.
7. Fill env/domain settings.
8. Deploy as a Stack.
9. Redeploy, roll back, view logs, and run one-off commands from the Stack page.

## Implementation Tasks

### 1. Source Connections

- [ ] Add `git_sources` table: owner, provider (`github` first), installation
      id/account login, encrypted access token or GitHub App installation
      reference, created/updated timestamps.
- [ ] Add source routes:
      `GET /api/sources/github/status`,
      `POST /api/sources/github/connect`,
      `DELETE /api/sources/github/{id}`,
      `GET /api/sources/github/{id}/repos`.
- [ ] Prefer a GitHub App installation flow for private repos. PAT support can
      stay as an admin-only fallback for hobby installs.
- [ ] Encrypt every provider token with the existing secrets module. Never log
      repository tokens or clone URLs containing credentials.
- [ ] Owner-scope sources: admins can see all, clients see only their own.

### 2. Repository Discovery

- [ ] Add `system/git.py` as the only git seam: `ls-remote`, shallow fetch,
      archive/download, and file listing through argv-only runner calls.
- [ ] Add repo discovery endpoint:
      `POST /api/sources/github/{id}/repos/{owner}/{repo}/discover`.
- [ ] Detect candidates:
      `compose.yaml`, `compose.yml`, `docker-compose.yaml`,
      `docker-compose.yml`, `Dockerfile`, `.env.example`.
- [ ] Return a normalized report: branch/commit, candidate files, likely app
      type, required env vars, exposed ports, volumes, and unsupported fields.
- [ ] Cache discovery results by source/repo/ref/commit hash to avoid repeated
      GitHub calls, but revalidate on deploy.

### 3. Compose Analyzer

- [ ] Replace the current dynamic `ComposeBlueprint` shortcut with a dedicated
      analyzer that converts repository compose files into a policy-checked
      intermediate model.
- [ ] Supported compose subset: `services`, `image`, `build`, `environment`,
      `env_file`, `ports`, `expose`, `volumes`, `depends_on`, `healthcheck`,
      `command`, `entrypoint`, `profiles`.
- [ ] Rejected fields: `privileged`, `network_mode: host`, host PID/IPC,
      arbitrary host binds, public host ports, external networks, device
      mounts, Docker socket mounts, `cap_add` beyond an allowlist.
- [ ] Normalize public ports into Hosty loopback allocations. The compose file
      never owns public `80`/`443` or arbitrary host binds.
- [ ] Convert named volumes into Hosty bind directories under the tenant stack
      root so backups and usage metering remain one system.
- [ ] Convert migration/profile services into stack actions instead of always
      running services.

### 4. Dockerfile Analyzer

- [ ] Detect Dockerfile path and build context.
- [ ] Infer port from `EXPOSE`, common framework defaults, or ask the user.
- [ ] Infer build args separately from runtime env vars. Secrets must not be
      passed as Docker build args unless explicitly marked non-secret.
- [ ] Render a single-service StackSpec with `build_repo`, `build_ref`, and
      `dockerfile_path`.

### 5. Domain And Env Wizard

- [ ] Add a guided frontend flow:
      Source -> Repository -> Ref -> Deployment file -> Analyze -> Configure
      env/domain/resources -> Deploy.
- [ ] Show warnings before deploy, not after a failed operation.
- [ ] Import `.env.example` keys as empty form rows. Mark required compose
      variables from `${VAR:?message}` as required.
- [ ] Support generated secrets for selected env keys.
- [ ] Keep "advanced raw env map" for expert use.

### 6. Stack Persistence

- [ ] Extend stack rows or add `stack_sources`: source id, repo full name,
      ref, commit sha, deployment file path, deployment kind, analyzer version.
- [ ] Store rendered services/volumes/endpoints as today. The reconciler reads
      desired rows, not GitHub.
- [ ] Keep the original source metadata so redeploys can fetch a new commit and
      re-render intentionally.

### 7. Build And Redeploy

- [ ] Extend `ServiceSpec` and DB rows with build metadata:
      repo URL/source id, commit sha, build context, Dockerfile path, build
      args, target, and generated image tag.
- [ ] Extend Quadlet `.build` rendering so Podman builds from a local checked
      out source directory or trusted Git source without credentials leaking
      into unit text.
- [ ] Add actions: `redeploy_latest`, `deploy_commit`, `rollback`,
      `run_profile`, `run_command`.
- [ ] Record previous successful commit/image digest for rollback.

### 8. Operations And Observability

- [ ] Operation steps: fetch source, analyze, reserve resources, write units,
      build image, start services, healthcheck, sync Caddy.
- [ ] Surface analyzer errors as 422 with actionable messages, not 500.
- [ ] Add stack detail panels for source repo/ref/commit, deployment file,
      latest deployed commit, and pending updates.
- [ ] Add drift notification text that names the failed service and last host
      error.

### 9. Tests And Gates

- [ ] Unit-test GitHub API adapters with mocked HTTP.
- [ ] Unit-test `system/git.py` argv builders and option-injection cases.
- [ ] Snapshot-test compose analyzer output for real Coolify-style samples.
- [ ] API tests: source connect, repo list, discovery, deploy compose, deploy
      Dockerfile, unsupported compose rejection, private repo auth failure.
- [ ] Frontend tests for the wizard and analyzer warning states.
- [ ] VM gate: deploy a private GitHub Next.js repo with Redis from compose,
      verify HTTPS through Caddy, edit env, redeploy a new commit, rollback.

## First Execution Slice

1. Stabilize the current dynamic compose templates so Stacks creation no
   longer returns generic internal server errors.
2. Add the analyzer as pure code with tests, initially fed by local fixture
   files instead of GitHub.
3. Add source metadata tables/routes after the analyzer contract is stable.
4. Build the frontend wizard against mocked discovery responses.
5. Wire GitHub as the first live provider.
