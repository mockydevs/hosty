import { FormField } from "@/components/form-field";
import { type JsonSchema, SchemaForm } from "@/components/schema-form";
import { ErrorState, LoadingState } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogActions,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { api, apiErrorMessage } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { copyToClipboard } from "@/lib/utils";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  Boxes,
  Check,
  Container,
  Copy,
  GitBranch,
  Globe2,
  Layers,
  Package,
  Rocket,
  Search,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router";
import { toast } from "sonner";

type Blueprint = components["schemas"]["BlueprintResponse"];
type BlueprintWithMeta = Blueprint & {
  category?: string;
  icon?: string;
  display_name?: string;
  description?: string;
};
type Accepted = components["schemas"]["StackOperationAccepted"];
type DeployMode = "git" | "image" | "template";

export function blueprintDisplayName(bp: Blueprint): string {
  const meta = bp as BlueprintWithMeta;
  if (meta.display_name) return meta.display_name;
  const schemaTitle = (bp.inputs_schema as JsonSchema).title;
  if (schemaTitle && !schemaTitle.endsWith("Inputs")) return schemaTitle;
  return bp.id
    .split("-")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function inputDefaults(schema: JsonSchema): Record<string, unknown> {
  const defaults: Record<string, unknown> = {};
  for (const [key, field] of Object.entries(schema.properties ?? {})) {
    if (field.default !== undefined && field.default !== null && field.default !== "") {
      defaults[key] = field.default;
    }
  }
  return defaults;
}

function slugFromRepo(repo: string): string {
  const last = repo.trim().replace(/\/$/, "").split(/[/:]/).pop() ?? "";
  const cleaned = last
    .replace(/\.git$/i, "")
    .toLowerCase()
    .replace(/[^a-z0-9-]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return cleaned.slice(0, 32) || "my-app";
}

function validateSlug(value: string): string | null {
  return /^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$/.test(value)
    ? null
    : "1-32 lowercase letters, digits or hyphens; no leading/trailing hyphen";
}

function MethodButton({
  active,
  icon: Icon,
  title,
  description,
  onClick,
}: {
  active: boolean;
  icon: typeof GitBranch;
  title: string;
  description: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      className={[
        "flex w-full cursor-pointer items-start gap-3 rounded-md border p-3 text-left transition-colors",
        active
          ? "border-primary bg-primary/10 text-foreground"
          : "border-border bg-card hover:border-primary/60 hover:bg-accent/50",
      ].join(" ")}
      onClick={onClick}
    >
      <Icon className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
      <span className="min-w-0">
        <span className="block text-sm font-medium">{title}</span>
        <span
          className={active ? "block text-xs text-muted-foreground" : "block text-xs text-muted-foreground"}
        >
          {description}
        </span>
      </span>
    </button>
  );
}

function DeploySteps({ mode }: { mode: DeployMode }) {
  const steps = [
    mode === "git" ? "Repository" : mode === "image" ? "Image" : "Template",
    "Build",
    "Domain",
    "Deploy",
  ];
  return (
    <ol className="grid gap-2 sm:grid-cols-4">
      {steps.map((step, index) => (
        <li
          key={step}
          className="flex items-center gap-2 rounded-md border border-border px-3 py-2"
        >
          <span
            className={[
              "flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-xs font-medium",
              index === 0 ? "bg-primary text-primary-foreground" : "bg-muted text-muted-foreground",
            ].join(" ")}
          >
            {index === 0 ? <Check className="h-3 w-3" aria-hidden /> : index + 1}
          </span>
          <span className="text-xs font-medium">{step}</span>
        </li>
      ))}
    </ol>
  );
}

function SectionTitle({
  icon: Icon,
  title,
  description,
}: {
  icon: typeof GitBranch;
  title: string;
  description?: string;
}) {
  return (
    <div className="flex items-start gap-3">
      <div className="rounded-md border border-border bg-muted p-2">
        <Icon className="h-4 w-4" aria-hidden />
      </div>
      <div>
        <h2 className="text-base font-semibold">{title}</h2>
        {description && <p className="text-sm text-muted-foreground">{description}</p>}
      </div>
    </div>
  );
}

function BuildPackPicker() {
  return (
    <div className="grid gap-3">
      <div className="rounded-md border border-primary bg-primary/10 px-3 py-2 text-foreground">
        <div className="flex items-center gap-2 text-sm font-medium">
          <Package className="h-4 w-4 text-primary" aria-hidden />
          Dockerfile
        </div>
        <p className="mt-1 text-xs text-muted-foreground">Build from the selected repository branch.</p>
      </div>
      <div className="rounded-md border border-dashed border-border px-3 py-2 text-muted-foreground">
        <div className="flex items-center justify-between gap-2 text-sm font-medium">
          <span className="flex items-center gap-2">
            <Layers className="h-4 w-4" aria-hidden />
            Docker Compose
          </span>
          <Badge variant="outline">Next</Badge>
        </div>
        <p className="mt-1 text-xs">Compose analyzer and service review are being wired in.</p>
      </div>
    </div>
  );
}

function GitDeployForm({
  blueprint,
  onSubmit,
  pending,
  serverError,
}: {
  blueprint?: Blueprint;
  onSubmit: (name: string, inputs: Record<string, unknown>) => Promise<void>;
  pending: boolean;
  serverError: string | null;
}) {
  const [sourceId, setSourceId] = useState<string>("public");
  const [repo, setRepo] = useState("");
  const [branch, setBranch] = useState("main");
  const [name, setName] = useState("my-app");
  const [domain, setDomain] = useState("");
  const [port, setPort] = useState("3000");
  const [envVars, setEnvVars] = useState("");
  const [manualName, setManualName] = useState(false);
  const [nameError, setNameError] = useState<string | null>(null);
  
  const sources = useQuery({
    queryKey: ["sources"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/sources", {});
      if (error || !data) return [];
      return data as any[];
    },
  });

  const repos = useQuery({
    queryKey: ["sources", sourceId, "repos"],
    enabled: sourceId !== "public",
    retry: 1,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/sources/{source_id}/repos" as any, {
        params: { path: { source_id: Number(sourceId) } } as any
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load repositories"));
      return data as any[];
    },
  });

  useEffect(() => {
    if (!manualName && repo) setName(slugFromRepo(repo));
  }, [manualName, repo]);

  if (!blueprint) {
    return <ErrorState message="Git repository deployment is not available in this build." />;
  }

  const submit = async () => {
    const slug = name.trim().toLowerCase();
    const slugError = validateSlug(slug);
    if (slugError) {
      setNameError(slugError);
      return;
    }
    setNameError(null);
    
    // Parse env string to Record<string, string>
    const parsedEnv: Record<string, string> = {};
    envVars.split("\n").forEach(line => {
      const match = line.match(/^([^=]+)=(.*)$/);
      if (match && match[1] && match[2] !== undefined) {
        parsedEnv[match[1].trim()] = match[2].trim();
      }
    });

    await onSubmit(slug, {
      ...inputDefaults(blueprint.inputs_schema as JsonSchema),
      repo: repo.trim(),
      branch: branch.trim() || "main",
      internal_port: Number(port),
      domain: domain.trim(),
      env: parsedEnv,
      source_id: sourceId === "public" ? null : Number(sourceId),
    });
  };

  return (
    <div className="space-y-6">
      <SectionTitle
        icon={GitBranch}
        title="Git repository"
        description="Connect a repository, configure environment, and deploy."
      />

      <div className="space-y-6 rounded-md border border-border bg-card p-6">
        <div className="space-y-4">
          <h3 className="text-lg font-medium">1. Select Source</h3>
          <div className="grid gap-4 sm:grid-cols-3">
            <Card 
              className={`cursor-pointer ${sourceId === "public" ? "border-primary bg-primary/10" : "hover:bg-muted/50"}`}
              onClick={() => setSourceId("public")}
            >
              <CardContent className="p-4 text-center">
                <Globe2 className="mx-auto mb-2 h-6 w-6 text-muted-foreground" />
                <div className="font-medium">Public Repository</div>
              </CardContent>
            </Card>
            {sources.data?.map((source) => (
              <Card 
                key={source.id}
                className={`cursor-pointer ${sourceId === String(source.id) ? "border-primary bg-primary/10" : "hover:bg-muted/50"}`}
                onClick={() => setSourceId(String(source.id))}
              >
                <CardContent className="p-4 text-center">
                  <GitBranch className="mx-auto mb-2 h-6 w-6 text-muted-foreground" />
                  <div className="font-medium">{source.name}</div>
                </CardContent>
              </Card>
            ))}
          </div>
        </div>

        <div className="space-y-4">
          <h3 className="text-lg font-medium">2. Repository & Branch</h3>
          {sourceId === "public" ? (
            <FormField label="Repository URL" htmlFor="repo-url">
              <Input
                id="repo-url"
                placeholder="https://github.com/acme/app.git"
                autoComplete="off"
                spellCheck={false}
                value={repo}
                onChange={(event) => setRepo(event.target.value)}
              />
            </FormField>
          ) : (
            <FormField label="Select Repository" htmlFor="repo-select">
              <select
                id="repo-select"
                className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
                value={repo}
                onChange={(e) => {
                  setRepo(e.target.value);
                  const selected = repos.data?.find(
                    (r) => (r.clone_url || r.name) === e.target.value,
                  );
                  if (selected?.default_branch) setBranch(selected.default_branch);
                }}
                disabled={repos.isPending}
              >
                {repos.isPending ? (
                  <option>Loading repositories…</option>
                ) : repos.isError ? (
                  <option>Failed to load — see error below</option>
                ) : (
                  <>
                    <option value="">-- Select --</option>
                    {repos.data?.map((r) => (
                      <option key={r.clone_url || r.name} value={r.clone_url || r.name}>
                        {r.name}
                      </option>
                    ))}
                  </>
                )}
              </select>
              {repos.isError && (
                <p className="mt-1.5 text-xs text-destructive" role="alert">
                  {repos.error instanceof Error ? repos.error.message : "Unknown error"}
                  {" — "}
                  <button
                    type="button"
                    className="underline"
                    onClick={() => repos.refetch()}
                  >
                    retry
                  </button>
                </p>
              )}
            </FormField>
          )}

          <FormField label="Branch" htmlFor="repo-branch">
            <Input
              id="repo-branch"
              autoComplete="off"
              spellCheck={false}
              value={branch}
              onChange={(event) => setBranch(event.target.value)}
            />
          </FormField>
        </div>

        <div className="space-y-4">
          <h3 className="text-lg font-medium">3. Configuration</h3>
          <FormField label="Environment Variables (.env format)" htmlFor="env-vars">
            <textarea
              id="env-vars"
              className="flex min-h-[120px] w-full rounded-md border border-input bg-background px-3 py-2 text-sm font-mono ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
              placeholder="PORT=3000&#10;DATABASE_URL=postgres://..."
              value={envVars}
              onChange={(e) => setEnvVars(e.target.value)}
              spellCheck={false}
            />
          </FormField>

          <BuildPackPicker />

          <div className="grid gap-4 sm:grid-cols-3">
            <FormField label="App port" htmlFor="repo-port">
              <Input
                id="repo-port"
                type="number"
                min={1}
                max={65535}
                value={port}
                onChange={(event) => setPort(event.target.value)}
              />
            </FormField>
            <FormField label="Deployment name" htmlFor="deploy-name" error={nameError ?? undefined}>
              <Input
                id="deploy-name"
                autoComplete="off"
                spellCheck={false}
                value={name}
                onChange={(event) => {
                  setManualName(true);
                  setName(event.target.value);
                }}
              />
            </FormField>
            <FormField label="Domain" htmlFor="deploy-domain">
              <Input
                id="deploy-domain"
                placeholder="app.example.com"
                autoComplete="off"
                spellCheck={false}
                value={domain}
                onChange={(event) => setDomain(event.target.value)}
              />
            </FormField>
          </div>
        </div>

        {serverError && (
          <p className="text-sm text-destructive" role="alert">
            {serverError}
          </p>
        )}

        <div className="flex justify-end pt-4">
          <Button onClick={submit} loading={pending} disabled={!repo.trim() || !port.trim()}>
            <Rocket className="mr-2 h-4 w-4" aria-hidden />
            Deploy Repository
          </Button>
        </div>
      </div>
    </div>
  );
}

function ImageDeployForm({
  blueprint,
  onSubmit,
  pending,
  serverError,
}: {
  blueprint?: Blueprint;
  onSubmit: (name: string, inputs: Record<string, unknown>) => Promise<void>;
  pending: boolean;
  serverError: string | null;
}) {
  const [name, setName] = useState("container-app");
  const [image, setImage] = useState("");
  const [port, setPort] = useState("3000");
  const [domain, setDomain] = useState("");
  const [nameError, setNameError] = useState<string | null>(null);

  if (!blueprint) {
    return <ErrorState message="Container image deployment is not available in this build." />;
  }

  const submit = async () => {
    const slug = name.trim().toLowerCase();
    const slugError = validateSlug(slug);
    if (slugError) {
      setNameError(slugError);
      return;
    }
    setNameError(null);
    await onSubmit(slug, {
      ...inputDefaults(blueprint.inputs_schema as JsonSchema),
      image: image.trim(),
      internal_port: Number(port),
      domain: domain.trim(),
    });
  };

  return (
    <div className="space-y-6">
      <SectionTitle
        icon={Container}
        title="Container image"
        description="Deploy an existing image from Docker Hub, GHCR, or another registry."
      />
      <div className="space-y-4 rounded-md border border-border bg-card p-4">
        <FormField label="Image" htmlFor="image-ref">
          <Input
            id="image-ref"
            placeholder="ghcr.io/acme/app:latest"
            autoComplete="off"
            spellCheck={false}
            value={image}
            onChange={(event) => setImage(event.target.value)}
          />
        </FormField>
        <div className="grid gap-4 sm:grid-cols-3">
          <FormField label="Deployment name" htmlFor="image-name" error={nameError ?? undefined}>
            <Input
              id="image-name"
              autoComplete="off"
              spellCheck={false}
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
          </FormField>
          <FormField label="App port" htmlFor="image-port">
            <Input
              id="image-port"
              type="number"
              min={1}
              max={65535}
              value={port}
              onChange={(event) => setPort(event.target.value)}
            />
          </FormField>
          <FormField label="Domain" htmlFor="image-domain">
            <Input
              id="image-domain"
              placeholder="app.example.com"
              autoComplete="off"
              spellCheck={false}
              value={domain}
              onChange={(event) => setDomain(event.target.value)}
            />
          </FormField>
        </div>
        {serverError && (
          <p className="text-sm text-destructive" role="alert">
            {serverError}
          </p>
        )}
        <Button onClick={submit} loading={pending} disabled={!image.trim() || !port.trim()}>
          <Rocket className="h-4 w-4" aria-hidden />
          Deploy
        </Button>
      </div>
    </div>
  );
}

function TemplateDeployForm({
  blueprints,
  onSubmit,
  pending,
  serverError,
}: {
  blueprints: Blueprint[];
  onSubmit: (blueprint: Blueprint, name: string, inputs: Record<string, unknown>) => Promise<void>;
  pending: boolean;
  serverError: string | null;
}) {
  const templates = blueprints.filter((bp) => !["git", "raw-image"].includes(bp.id));
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<Blueprint | null>(templates[0] ?? null);
  const [name, setName] = useState("");
  const [nameError, setNameError] = useState<string | null>(null);

  useEffect(() => {
    if (selected) {
      setName(`${selected.id}-${Math.random().toString(36).slice(2, 6)}`);
      setNameError(null);
    }
  }, [selected]);

  const filtered = templates.filter((bp) => {
    const term = query.trim().toLowerCase();
    return !term || bp.id.includes(term) || blueprintDisplayName(bp).toLowerCase().includes(term);
  });

  if (!selected) return <ErrorState message="No one-click templates are available." />;

  return (
    <div className="grid gap-5 lg:grid-cols-[320px_1fr]">
      <div className="space-y-3">
        <SectionTitle icon={Boxes} title="Templates" />
        <div className="relative">
          <Search
            className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
            aria-hidden
          />
          <Input
            aria-label="Search templates"
            placeholder="Search templates"
            className="pl-9"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </div>
        <div className="max-h-[520px] space-y-2 overflow-y-auto pr-1">
          {filtered.map((bp) => {
            const active = selected.id === bp.id;
            const meta = bp as BlueprintWithMeta;
            return (
              <button
                type="button"
                key={bp.id}
                className={[
                  "flex w-full cursor-pointer items-center gap-3 rounded-md border p-3 text-left transition-colors",
                  active
                    ? "border-primary bg-primary text-primary-foreground"
                    : "border-border bg-card hover:border-primary/60 hover:bg-accent/50",
                ].join(" ")}
                onClick={() => setSelected(bp)}
              >
                <div className={active ? "text-primary-foreground" : "text-muted-foreground"}>
                  {meta.icon ? (
                    <img src={`/icons/${meta.icon}.svg`} className="h-6 w-6" alt="" />
                  ) : (
                    <Boxes className="h-5 w-5" aria-hidden />
                  )}
                </div>
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm font-medium">
                    {blueprintDisplayName(bp)}
                  </span>
                  <span
                    className={
                      active ? "block text-xs opacity-80" : "block text-xs text-muted-foreground"
                    }
                  >
                    {(bp as BlueprintWithMeta).category || "Template"}
                  </span>
                </span>
              </button>
            );
          })}
        </div>
      </div>

      <div className="rounded-md border border-border bg-card p-4">
        <SchemaForm
          key={selected.id}
          schema={selected.inputs_schema as JsonSchema}
          onSubmit={async (inputs) => {
            const slug = name.trim().toLowerCase();
            const slugError = validateSlug(slug);
            if (slugError) {
              setNameError(slugError);
              return;
            }
            setNameError(null);
            await onSubmit(selected, slug, inputs);
          }}
          submitLabel={`Deploy ${blueprintDisplayName(selected)}`}
          pending={pending}
          serverError={serverError}
        >
          <FormField label="Deployment name" htmlFor="template-name" error={nameError ?? undefined}>
            <Input
              id="template-name"
              autoComplete="off"
              spellCheck={false}
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
          </FormField>
        </SchemaForm>
      </div>
    </div>
  );
}

export function ShowOnceDialog({
  secrets,
  onClose,
}: {
  secrets: Record<string, string> | null;
  onClose: () => void;
}) {
  const copy = async (value: string) => {
    try {
      await copyToClipboard(value);
      toast.success("Copied");
    } catch {
      toast.error("Copy failed. Select the text manually.");
    }
  };

  return (
    <Dialog open={secrets !== null} onClose={onClose}>
      <DialogContent>
        <DialogTitle>Generated secrets</DialogTitle>
        <DialogDescription>These values are shown once and stored encrypted.</DialogDescription>
        {secrets && (
          <dl className="space-y-2 text-sm">
            {Object.entries(secrets).map(([label, value]) => (
              <div key={label} className="flex items-center justify-between gap-2">
                <dt className="text-muted-foreground">{label}</dt>
                <dd className="flex items-center gap-1 font-mono text-xs">
                  <span className="break-all">{value}</span>
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label={`Copy ${label}`}
                    onClick={() => copy(value)}
                  >
                    <Copy className="h-3.5 w-3.5" />
                  </Button>
                </dd>
              </div>
            ))}
          </dl>
        )}
        <DialogActions>
          <Button onClick={onClose}>Continue</Button>
        </DialogActions>
      </DialogContent>
    </Dialog>
  );
}

export function StackCreatePage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [mode, setMode] = useState<DeployMode>("git");
  const [serverError, setServerError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [accepted, setAccepted] = useState<Accepted | null>(null);

  const blueprints = useQuery({
    queryKey: ["stack-blueprints"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/stacks/blueprints");
      if (error || !data)
        throw new Error(apiErrorMessage(error, "Failed to load deployment options"));
      return data;
    },
  });

  const byId = useMemo(() => {
    const map = new Map<string, Blueprint>();
    for (const bp of blueprints.data ?? []) map.set(bp.id, bp);
    return map;
  }, [blueprints.data]);

  const submitBlueprint = async (
    blueprint: Blueprint,
    name: string,
    inputs: Record<string, unknown>,
  ) => {
    setServerError(null);
    setPending(true);
    try {
      const { data, error, response } = await api.POST("/api/stacks", {
        body: { name, blueprint_id: blueprint.id, inputs },
      });
      if (error || !data) {
        setServerError(apiErrorMessage(error, `Deploy failed (${response.status})`));
        return;
      }
      await queryClient.invalidateQueries({ queryKey: ["stacks"] });
      toast.success(`Deploying ${data.stack.name}`);
      if (Object.keys(data.show_once ?? {}).length > 0) {
        setAccepted(data);
      } else {
        navigate(`/stacks/${data.stack.id}`, { state: { operationId: data.operation_id } });
      }
    } finally {
      setPending(false);
    }
  };

  const gitBlueprint = byId.get("git");
  const imageBlueprint = byId.get("raw-image");

  return (
    <div className="mx-auto max-w-7xl space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <Button variant="ghost" size="icon" aria-label="Back" onClick={() => navigate("/stacks")}>
            <ArrowLeft className="h-4 w-4" />
          </Button>
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">New deployment</h1>
            <p className="text-sm text-muted-foreground">
              Deploy from Git, an image, or a template.
            </p>
          </div>
        </div>
      </div>

      {blueprints.isPending ? (
        <LoadingState label="Loading deployment options" />
      ) : blueprints.isError ? (
        <ErrorState message={blueprints.error.message} onRetry={() => blueprints.refetch()} />
      ) : (
        <div className="grid gap-5 xl:grid-cols-[300px_1fr]">
          <aside className="space-y-3">
            <div className="rounded-md border border-border bg-card p-3">
              <p className="mb-2 text-xs font-medium uppercase text-muted-foreground">Source</p>
              <div className="space-y-2">
                <MethodButton
                  active={mode === "git"}
                  icon={GitBranch}
                  title="Git repository"
                  description="Dockerfile from GitHub or any Git URL"
                  onClick={() => {
                    setMode("git");
                    setServerError(null);
                  }}
                />
                <MethodButton
                  active={mode === "image"}
                  icon={Container}
                  title="Container image"
                  description="Docker Hub, GHCR, private registry"
                  onClick={() => {
                    setMode("image");
                    setServerError(null);
                  }}
                />
                <MethodButton
                  active={mode === "template"}
                  icon={Boxes}
                  title="Templates"
                  description="Databases, WordPress, tools"
                  onClick={() => {
                    setMode("template");
                    setServerError(null);
                  }}
                />
              </div>
            </div>
          </aside>

          <section className="space-y-5">
            <DeploySteps mode={mode} />
            {mode === "git" && (
              <GitDeployForm
                blueprint={gitBlueprint}
                pending={pending}
                serverError={serverError}
                onSubmit={(name, inputs) => {
                  if (!gitBlueprint) return Promise.resolve();
                  return submitBlueprint(gitBlueprint, name, inputs);
                }}
              />
            )}
            {mode === "image" && (
              <ImageDeployForm
                blueprint={imageBlueprint}
                pending={pending}
                serverError={serverError}
                onSubmit={(name, inputs) => {
                  if (!imageBlueprint) return Promise.resolve();
                  return submitBlueprint(imageBlueprint, name, inputs);
                }}
              />
            )}
            {mode === "template" && (
              <TemplateDeployForm
                blueprints={blueprints.data}
                pending={pending}
                serverError={serverError}
                onSubmit={submitBlueprint}
              />
            )}
          </section>
        </div>
      )}

      <ShowOnceDialog
        secrets={accepted?.show_once ?? null}
        onClose={() => {
          if (accepted) {
            navigate(`/stacks/${accepted.stack.id}`, {
              state: { operationId: accepted.operation_id },
            });
          }
        }}
      />
    </div>
  );
}
